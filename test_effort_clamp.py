"""The global Codex reasoning effort must not hard-fail a route that cannot take it.

``~/.codex/config.toml`` carries one ``model_reasoning_effort`` for every model the picker
can select. The live gateway answers 400 ``unsupported_reasoning_effort`` when the value is
outside the routed model's list, which turns a supported model into an unusable one.
Measured 2026-10-08: only ``x-ai/grok-4.7`` rejects ``max``.
"""
import unittest

import adapter


class ClampReasoningEffort(unittest.TestCase):
    def body(self, model, effort=None, **extra):
        payload = {'model': model, 'input': []}
        if effort is not None:
            payload['reasoning'] = {'effort': effort, 'summary': 'auto'}
        payload.update(extra)
        return payload

    def test_grok_max_is_pulled_down_to_its_ceiling(self):
        payload = self.body('x-ai/grok-4.7', 'max')
        clamped, was = adapter.clamp_reasoning_effort(payload)
        self.assertEqual(was, 'max')
        self.assertEqual(clamped['reasoning']['effort'], 'xhigh')

    def test_grok_keeps_efforts_it_supports(self):
        for effort in ('low', 'medium', 'high', 'xhigh'):
            with self.subTest(effort=effort):
                payload = self.body('x-ai/grok-4.7', effort)
                clamped, was = adapter.clamp_reasoning_effort(payload)
                self.assertIsNone(was)
                self.assertIs(clamped, payload)

    def test_no_op_leaves_the_original_object_alone(self):
        # A caller that reuses the body must not see an effort it never sent disappear.
        payload = self.body('claude-opus-5-5', 'max')
        clamped, was = adapter.clamp_reasoning_effort(payload)
        self.assertIsNone(was)
        self.assertIs(clamped, payload)
        self.assertEqual(payload['reasoning'], {'effort': 'max', 'summary': 'auto'})

    def test_routes_measured_to_accept_max_are_untouched(self):
        for model in ('gemini-3.8-flash', 'doubao-seed-2.1-pro', 'doubao-seed-evolving',
                      'claude-opus-5-5', 'deepseek-flash', 'gpt-6-astra'):
            with self.subTest(model=model):
                payload = self.body(model, 'max')
                clamped, was = adapter.clamp_reasoning_effort(payload)
                self.assertIsNone(was)
                self.assertEqual(clamped['reasoning']['effort'], 'max')

    def test_only_the_other_key_is_replaced(self):
        payload = self.body('x-ai/grok-4.7', 'max', temperature=0.2)
        clamped, _ = adapter.clamp_reasoning_effort(payload)
        self.assertEqual(clamped['temperature'], 0.2)
        self.assertEqual(clamped['reasoning']['summary'], 'auto')
        self.assertEqual(payload['reasoning']['effort'], 'max')  # original untouched

    def test_shapes_that_carry_no_effort_pass_through(self):
        for payload in ({'model': 'x-ai/grok-4.7'},
                        {'model': 'x-ai/grok-4.7', 'reasoning': None},
                        {'model': 'x-ai/grok-4.7', 'reasoning': {'effort': None}},
                        {'model': 'x-ai/grok-4.7', 'reasoning': {'effort': 'unknown-level'}},
                        {'model': None, 'reasoning': {'effort': 'max'}},
                        'not-a-dict'):
            with self.subTest(payload=payload):
                clamped, was = adapter.clamp_reasoning_effort(payload)
                self.assertIsNone(was)
                self.assertIs(clamped, payload)

    def test_none_means_off_and_is_never_raised(self):
        payload = self.body('x-ai/grok-4.7', 'none')
        clamped, was = adapter.clamp_reasoning_effort(payload)
        self.assertIsNone(was)
        self.assertEqual(clamped['reasoning']['effort'], 'none')

    def test_every_ceiling_is_a_real_level(self):
        for model, ceiling in adapter.REASONING_EFFORT_CEILING.items():
            with self.subTest(model=model):
                self.assertIn(ceiling, adapter.REASONING_EFFORT_ORDER)

    def test_ceiling_only_ever_lowers(self):
        order = adapter.REASONING_EFFORT_ORDER
        for model, ceiling in adapter.REASONING_EFFORT_CEILING.items():
            for effort in order:
                with self.subTest(model=model, effort=effort):
                    payload = self.body(model, effort)
                    clamped, was = adapter.clamp_reasoning_effort(payload)
                    if was is None:
                        continue
                    self.assertLessEqual(order.index(clamped['reasoning']['effort']),
                                         order.index(effort))


if __name__ == '__main__':
    unittest.main()
