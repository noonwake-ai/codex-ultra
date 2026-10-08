"""A GPT route must never be handed thinking state minted by another upstream.

`model_ciphertext_owner` says which upstream a *route* can decode. That is a different
question from who minted a given blob: a thread switched from Claude to a GPT model keeps
the Claude turns, so the history carries Anthropic state inside a request routed to OpenAI.
OpenAI refuses the whole request it cannot "protect" — live symptom, 2026-10-08:

    event: response.failed
    {"response":{"error":{"code":"upstream_error",
     "message":"response protection is unavailable","type":"internal_error"},
     "status":"failed"}}

The GPT branch used to skip the conversion wholesale (`and not model.startswith('gpt-')`
short-circuited before the provenance check), so that foreign state travelled untouched and
the session failed on every following turn — including the compaction turn, which is the one
that would have cleared it. These tests pin the foreign-blob conversion on native routes and
pin that none of the old native behaviour moved.
"""

import copy
import inspect
import unittest
from unittest import mock

import adapter

TEST_KEY = bytes(range(32))
TEST_CREDENTIAL = 'synthetic-offline-only-credential'
CFG = {'upstream': 'https://offline.invalid/v1',
       'compactor_model': 'gpt-6-sol', 'compactor_effort': 'medium'}

ARK_BLOB = 'djEqGHfuLmtwxZHnMpcVRhuWxG' + 'A' * 32
OPENAI_BLOB = 'gAAAAABqvlZNkUQ0S7-BqU5LgG-DBkqWg8hPdUMfPNk_' + 'A' * 32
ANTHROPIC_BLOB = 'anthropic-thinking-v1:' + 'A' * 64
DEEPSEEK_SURROGATE = '56ef6028-9c69-4176-b58d-713e818c10fb-0'
GARBAGE_BLOB = 'not-a-known-vendor-tag-' + 'B' * 40

SUMMARY = 'Public continuation summary.'


def reasoning(blob=ARK_BLOB, summary=SUMMARY, extra=None):
    item = {'type': 'reasoning', 'id': 'rs_synthetic',
            'summary': [{'type': 'summary_text', 'text': summary}]}
    if blob is not None:
        item['encrypted_content'] = blob
    if extra:
        item.update(extra)
    return item


def reasoning_text_item():
    return {'type': 'reasoning', 'id': 'rs_think',
            'summary': [{'type': 'summary_text', 'text': SUMMARY}],
            'encrypted_content': DEEPSEEK_SURROGATE,
            'content': [{'type': 'reasoning_text', 'text': 'I should check the fixture.'}]}


def user_turn():
    return {'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': '把第二个模块接上。'}]}


def tool_group():
    return [{'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1',
             'name': 'read_file', 'arguments': '{"path":"a.py"}'},
            {'type': 'function_call_output', 'call_id': 'call_1', 'output': 'ok'}]


class Base(unittest.TestCase):
    def setUp(self):
        for target in ('adapter.requests.request', 'adapter.requests.post',
                       'adapter.read_api_key', 'adapter.keychain_key'):
            patcher = mock.patch(target, side_effect=AssertionError('external_access_forbidden'))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.adapter = adapter.Adapter(copy.deepcopy(CFG), TEST_KEY,
                                       transport=object(),
                                       credential=lambda: TEST_CREDENTIAL)

    def expand(self, items, model, **kwargs):
        return self.adapter.expand(copy.deepcopy(items), model, **kwargs)

    def blobs(self, items, model, **kwargs):
        return [i.get('encrypted_content') for i in self.expand(items, model, **kwargs)
                if i.get('type') == 'reasoning']

    def summary_texts(self, items, model, **kwargs):
        return [p['text'] for i in self.expand(items, model, **kwargs)
                if i.get('type') == 'message'
                for p in i.get('content', []) if isinstance(p, dict)]


class ForeignBlobOnNativeRouteTests(Base):
    """The bug: foreign state riding into OpenAI on a gpt-* route."""

    def test_anthropic_blob_never_reaches_openai(self):
        for model in ('gpt-6-astra', 'gpt-6-sol', 'gpt-6.1-sol'):
            with self.subTest(model=model):
                items = [user_turn(), reasoning(ANTHROPIC_BLOB)]
                self.assertEqual(self.blobs(items, model), [])
                self.assertIn(SUMMARY, self.summary_texts(items, model))

    def test_ark_blob_never_reaches_openai(self):
        items = [user_turn(), reasoning(ARK_BLOB)]
        self.assertEqual(self.blobs(items, 'gpt-6-astra'), [])
        self.assertIn(SUMMARY, self.summary_texts(items, 'gpt-6-astra'))

    def test_reasoning_text_chain_never_reaches_openai(self):
        # OpenAI only ever mints opaque encrypted_content; a reasoning_text part is foreign.
        items = [user_turn(), reasoning_text_item()]
        self.assertEqual(self.blobs(items, 'gpt-6-astra'), [])
        self.assertIn(SUMMARY, self.summary_texts(items, 'gpt-6-astra'))

    def test_foreign_blob_is_dropped_on_the_compaction_forward_too(self):
        # The live failure was the native compaction turn, which goes through prepare_forward.
        body = {'model': 'gpt-6-astra', 'stream': True,
                'input': [user_turn(), reasoning(ANTHROPIC_BLOB),
                          {'type': 'compaction_trigger'}]}
        # Builds whose prepare_forward takes a media policy let this test skip the media
        # layer; this case is about the chain, not the pixels.
        kwargs = ({'media': 'off'}
                  if 'media' in inspect.signature(self.adapter.prepare_forward).parameters
                  else {})
        prepared = self.adapter.prepare_forward(copy.deepcopy(body), {}, **kwargs)
        self.assertEqual([i for i in prepared['input'] if i.get('type') == 'reasoning'], [])
        self.assertEqual([i for i in prepared['input']
                          if i.get('type') == 'compaction_trigger'], [{'type': 'compaction_trigger'}])

    def test_agent_message_state_riding_a_foreign_blob_does_not_become_the_route_s_own(self):
        # A foreign blob must not be mistaken for the route's sealed agent state.
        items = [reasoning(ANTHROPIC_BLOB, extra={'id': 'rs_agent'})]
        out = self.expand(items, 'gpt-6-astra')
        self.assertFalse(any(i.get('type') == 'reasoning' for i in out))


class NativeBehaviourIsUnchangedTests(Base):
    """Nothing that already worked may move: the conversion is positive-only."""

    def test_openai_blob_is_replayed_byte_for_byte(self):
        item = reasoning(OPENAI_BLOB)
        out = self.expand([user_turn(), item], 'gpt-6-astra')
        self.assertEqual([i for i in out if i.get('type') == 'reasoning'], [item])

    def test_summary_only_reasoning_is_untouched(self):
        item = reasoning(None)
        out = self.expand([user_turn(), item], 'gpt-6-astra')
        self.assertEqual([i for i in out if i.get('type') == 'reasoning'], [item])

    def test_unknown_blob_keeps_the_previous_behaviour(self):
        item = reasoning(GARBAGE_BLOB)
        out = self.expand([user_turn(), item], 'gpt-6-astra')
        self.assertEqual([i for i in out if i.get('type') == 'reasoning'], [item])

    def test_openai_route_still_keeps_everything_else_in_order(self):
        items = [user_turn()] + tool_group() + [reasoning(OPENAI_BLOB)]
        self.assertEqual(self.expand(items, 'gpt-6-astra'), items)

    def test_messages_groups_and_media_survive_the_drop(self):
        media = {'type': 'message', 'role': 'user',
                 'content': [{'type': 'input_image', 'image_url': 'data:image/png;base64,AA=='}]}
        items = [user_turn(), reasoning(ANTHROPIC_BLOB)] + tool_group() + [media]
        out = self.expand(items, 'gpt-6-astra')
        self.assertEqual([i for i in out if i.get('type') == 'function_call'], [items[2]])
        self.assertEqual([i for i in out if i.get('type') == 'function_call_output'], [items[3]])
        self.assertEqual([i for i in out if i.get('type') == 'message' and i.get('role') == 'user'][-1], media)
        self.assertEqual(len([i for i in out if i.get('type') == 'reasoning']), 0)


class OtherRoutesAreUnchangedTests(Base):
    def test_claude_route_still_replays_its_own_chain(self):
        item = reasoning(ANTHROPIC_BLOB)
        # A complete group: the chain is followed by the answer it produced. A trailing
        # chain is a different, deliberate case (see test_trailing_thinking).
        answer = {'type': 'message', 'role': 'assistant',
                  'content': [{'type': 'output_text', 'text': '接好了。'}]}
        out = self.expand([user_turn(), item, answer], 'claude-opus-5-5')
        self.assertEqual([i for i in out if i.get('type') == 'reasoning'], [item])

    def test_claude_route_still_summarises_a_foreign_blob(self):
        self.assertEqual(self.blobs([user_turn(), reasoning(OPENAI_BLOB)], 'claude-opus-5-5'), [])

    def test_ark_route_still_replays_its_own_chain(self):
        item = reasoning(ARK_BLOB)
        out = self.expand([user_turn(), item], 'doubao-seed-2.1-pro')
        self.assertEqual([i for i in out if i.get('type') == 'reasoning'], [item])

    def test_compaction_path_still_hands_readable_text_to_the_compactor(self):
        items = [user_turn(), reasoning(ANTHROPIC_BLOB)]
        self.assertEqual(self.blobs(items, 'gpt-6-astra', drop_trigger=True, replay_chain=False), [])

    def test_owner_helper_is_positive_only(self):
        self.assertTrue(adapter.reasoning_blob_is_foreign(reasoning(ANTHROPIC_BLOB), 'gpt-6-astra'))
        self.assertTrue(adapter.reasoning_blob_is_foreign(reasoning(ARK_BLOB), 'gpt-6-astra'))
        self.assertTrue(adapter.reasoning_blob_is_foreign(reasoning_text_item(), 'gpt-6-astra'))
        self.assertFalse(adapter.reasoning_blob_is_foreign(reasoning(OPENAI_BLOB), 'gpt-6-astra'))
        self.assertFalse(adapter.reasoning_blob_is_foreign(reasoning(GARBAGE_BLOB), 'gpt-6-astra'))
        self.assertFalse(adapter.reasoning_blob_is_foreign(reasoning(None), 'gpt-6-astra'))
        self.assertFalse(adapter.reasoning_blob_is_foreign(reasoning(ANTHROPIC_BLOB), 'claude-opus-5-5'))
        self.assertFalse(adapter.reasoning_blob_is_foreign(reasoning(ARK_BLOB), 'doubao-seed-2.1-pro'))


if __name__ == '__main__':
    unittest.main()
