"""A third-party thinking chain may only be replayed to the vendor that minted it.

Volcengine Ark seals its thinking chain into its own opaque blob and states that omitting it
degrades multi-round tool use; Anthropic requires every thinking block of a tool-use turn back.
The adapter used to summarise every non-GPT blob away, which silently dropped both. These tests
pin the provenance rule that replaced the old "long blob means OpenAI" assumption.
"""

import copy
import json
import unittest
from unittest import mock

import adapter
from adapter import Adapter

TEST_KEY = bytes(range(32))
TEST_CREDENTIAL = 'synthetic-offline-only-credential'
CFG = {'upstream': 'https://offline.invalid/v1',
       'compactor_model': 'gpt-6-sol', 'compactor_effort': 'medium'}

# Real shapes, truncated. 73/73 doubao samples in one production history start with base64 of b'v1'.
ARK_BLOB = 'djEqGHfuLmtwxZHnMpcVRhuWxG' + 'A' * 32
OPENAI_BLOB = 'gAAAAABqvlZNkUQ0S7-BqU5LgG-DBkqWg8hPdUMfPNk_' + 'A' * 32
ANTHROPIC_BLOB = 'anthropic-thinking-v1:' + 'A' * 64
DEEPSEEK_SURROGATE = '56ef6028-9c69-4176-b58d-713e818c10fb-0'
GARBAGE_BLOB = 'not-a-known-vendor-tag-' + 'B' * 40


def reasoning(blob=ARK_BLOB, text='Public continuation summary.'):
    item = {'type': 'reasoning', 'id': 'rs_synthetic',
            'summary': [{'type': 'summary_text', 'text': text}]}
    if blob is not None:
        item['encrypted_content'] = blob
    return item


class OfflineCase(unittest.TestCase):
    def setUp(self):
        for target in ('adapter.requests.request', 'adapter.requests.post',
                       'adapter.read_api_key', 'adapter.keychain_key'):
            patcher = mock.patch(target, side_effect=AssertionError('external_access_forbidden'))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.adapter = Adapter(copy.deepcopy(CFG), TEST_KEY, object(),
                               lambda: TEST_CREDENTIAL)

    def expand(self, items, model, **kw):
        return self.adapter.expand(copy.deepcopy(items), model, **kw)

    def summarised(self, items, model):
        """The chain is gone and only its public summary survived."""
        out = self.expand(items, model)
        self.assertFalse(any(i.get('type') == 'reasoning' for i in out))
        texts = [p['text'] for i in out if i.get('type') == 'message'
                 for p in i.get('content', []) if isinstance(p, dict)]
        self.assertIn('Public continuation summary.', texts)

    def replayed(self, items, model):
        self.assertTrue(any(i.get('type') == 'reasoning' for i in self.expand(items, model)))


class ProvenanceTests(unittest.TestCase):
    def test_blob_tags_are_read_from_the_blob_itself(self):
        self.assertEqual(adapter.ciphertext_owner(ARK_BLOB), 'ark')
        self.assertEqual(adapter.ciphertext_owner(OPENAI_BLOB), 'openai')
        self.assertEqual(adapter.ciphertext_owner(ANTHROPIC_BLOB), 'anthropic')
        for unknown in (DEEPSEEK_SURROGATE, GARBAGE_BLOB, '', None, 17):
            with self.subTest(blob=unknown):
                self.assertIsNone(adapter.ciphertext_owner(unknown))

    def test_model_slug_maps_to_the_upstream_that_can_decode_its_blob(self):
        for model, owner in (('doubao-seed-2.1-pro', 'ark'),
                             ('doubao-seed-evolving', 'ark'),
                             ('Doubao-Seed-2.1-Pro', 'ark'),
                             ('claude-opus-5-5', 'anthropic'),
                             ('gpt-6-astra', 'openai'),
                             ('deepseek-flash', None),
                             ('gemini-3.8-flash', None),
                             ('grok-4.7', None)):
            with self.subTest(model=model):
                self.assertEqual(adapter.model_ciphertext_owner(model), owner)


class ReplayTests(OfflineCase):
    def test_ark_gets_its_own_chain_back_byte_for_byte(self):
        item = reasoning()
        self.assertEqual(self.expand([item], 'doubao-seed-2.1-pro'), [item])

    def test_ark_keeps_an_unrecognised_blob(self):
        # Ark ignores a blob it cannot restore (live probe: HTTP 200), so an unknown tag is
        # forwarded rather than silently dropped on a future format change.
        self.replayed([reasoning(GARBAGE_BLOB)], 'doubao-seed-2.1-pro')

    def test_ark_still_summarises_another_vendors_chain(self):
        for blob in (OPENAI_BLOB, ANTHROPIC_BLOB):
            with self.subTest(blob=blob[:14]):
                self.summarised([reasoning(blob)], 'doubao-seed-2.1-pro')

    def test_ark_summarises_a_chainless_item(self):
        self.summarised([reasoning(None)], 'doubao-seed-2.1-pro')

    def test_claude_replays_its_own_thinking_block(self):
        # Anthropic requires every thinking block of a tool-use turn back, unchanged - but the
        # block has to sit inside a turn that continues. A chain left as the very last block is
        # refused outright ("The final block in an assistant message cannot be thinking",
        # measured on the live route 2026-10-08); test_trailing_thinking.py pins that case.
        item = reasoning(ANTHROPIC_BLOB)
        continuation = {'type': 'message', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': 'Answer.'}]}
        out = self.adapter.expand([copy.deepcopy(item), copy.deepcopy(continuation)],
                                  'claude-opus-5-5')
        self.assertEqual(out, [item, continuation])

    def test_claude_trailing_chain_loses_its_ciphertext_entirely(self):
        # A lone trailing chain must leave the request with no reasoning item and no raw
        # blob anywhere, which is what the route refuses otherwise.
        out = self.expand([reasoning(ANTHROPIC_BLOB)], 'claude-opus-5-5')
        self.assertFalse(any(i.get('type') == 'reasoning' for i in out))
        self.assertNotIn(ANTHROPIC_BLOB, json.dumps(out))
        self.assertEqual([i['type'] for i in out], ['message'])

    def test_claude_still_summarises_another_vendors_chain(self):
        # Replaying history onto another model is exactly when Anthropic says to drop them.
        for blob in (ARK_BLOB, OPENAI_BLOB):
            with self.subTest(blob=blob[:14]):
                self.summarised([reasoning(blob)], 'claude-opus-5-5')

    def test_strict_routes_keep_an_unrecognised_blob_out(self):
        for model in ('claude-opus-5-5', 'grok-4.7', 'deepseek-flash'):
            with self.subTest(model=model):
                self.summarised([reasoning(GARBAGE_BLOB)], model)

    def test_thinking_text_models_are_still_replayed(self):
        # DeepSeek and the NanoGPT-hosted models carry the readable chain plus a surrogate id
        # or a tagged blob; the whole item has always gone back and must keep doing so.
        deepseek = {'type': 'reasoning', 'id': 'rs_think',
                    'encrypted_content': DEEPSEEK_SURROGATE,
                    'content': [{'type': 'reasoning_text', 'text': 'I should check.'}],
                    'summary': [{'type': 'summary_text', 'text': 'Checked.'}]}
        nano = {'type': 'reasoning', 'id': 'rs_nano',
                'encrypted_content': 'nano-reasoning:v1:' + 'C' * 48,
                'content': [{'type': 'reasoning_text', 'text': 'Simple command.'}],
                'summary': [{'type': 'summary_text', 'text': 'Simple command.'}]}
        self.assertEqual(self.expand([deepseek], 'deepseek-flash'), [deepseek])
        self.assertEqual(self.expand([nano], 'qwen3.8-27b-nsfw'), [nano])

    def test_gpt_route_keeps_its_own_state_and_drops_a_foreign_chain(self):
        # The route is not proof of provenance. This thread ran on Doubao before, so its
        # history still holds an Ark blob; OpenAI refuses the whole request it cannot
        # protect ("response protection is unavailable"), so only its own item may travel.
        # An unrecognised blob is a separate case and still passes (see
        # test_foreign_blob.NativeBehaviourIsUnchangedTests).
        items = [reasoning(ARK_BLOB), reasoning(OPENAI_BLOB)]
        out = self.expand(items, 'gpt-6-astra')
        self.assertEqual([i for i in out if i.get('type') == 'reasoning'], [items[1]])
        self.assertIn('Public continuation summary.',
                      [p['text'] for i in out if i.get('type') == 'message'
                       for p in i.get('content', []) if isinstance(p, dict)])

    def test_compaction_still_hands_the_compactor_a_readable_summary(self):
        # The compactor is a GPT model: it cannot restore a third-party blob, so the compaction
        # path must keep the summary-only conversion even though the forward path replays.
        out = self.expand([reasoning()], 'doubao-seed-2.1-pro', replay_chain=False)
        self.assertFalse(any(i.get('type') == 'reasoning' for i in out))
        self.assertEqual(out[0]['content'][0]['text'], 'Public continuation summary.')

    def test_expand_never_mutates_the_request_it_was_handed(self):
        items = [reasoning(ARK_BLOB), reasoning(OPENAI_BLOB)]
        snapshot = copy.deepcopy(items)
        self.expand(items, 'doubao-seed-2.1-pro')
        self.assertEqual(items, snapshot)


if __name__ == '__main__':
    unittest.main()
