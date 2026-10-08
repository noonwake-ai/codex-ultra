"""A replayed chain must not leave a thinking block as the last block of a turn.

Anthropic answers 400 ``The final block in an assistant message cannot be thinking`` when the
assistant message ends on a thinking block. A turn that was interrupted after the model
thought but before it answered produces exactly that shape once the chain is replayed, and
the 400 repeats on every following request, so the session cannot continue.
"""
import unittest

import adapter


def reasoning(blob='anthropic-thinking-v1:AAAA', summary='模型想了什么'):
    item = {'type': 'reasoning', 'id': 'rs_1', 'encrypted_content': blob}
    if summary is not None:
        item['summary'] = [{'type': 'summary_text', 'text': summary}]
    return item


def message(text='答案'):
    return {'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': text}]}


def native_reasoning():
    return {'type': 'reasoning', 'id': 'rs_2',
            'content': [{'type': 'reasoning_text', 'text': 'DeepSeek 自带链'}]}


class DemoteTrailingReasoning(unittest.TestCase):
    def test_trailing_chain_becomes_a_summary_message(self):
        items = [message('问'), reasoning()]
        out = adapter.Adapter.demote_trailing_reasoning(items, 'claude-opus-5-5')
        self.assertEqual([i['type'] for i in out], ['message', 'message'])
        self.assertEqual(out[-1]['content'][0]['text'], '模型想了什么')
        self.assertEqual(out[-1]['role'], 'assistant')

    def test_a_chain_followed_by_its_answer_still_replays(self):
        items = [reasoning(), message('答案')]
        out = adapter.Adapter.demote_trailing_reasoning(items, 'claude-opus-5-5')
        self.assertIs(out, items)
        self.assertEqual(out[0]['type'], 'reasoning')

    def test_a_chain_inside_a_later_turn_still_replays(self):
        items = [reasoning(), message(), message('新问题'), reasoning(), message()]
        out = adapter.Adapter.demote_trailing_reasoning(items, 'claude-opus-5-5')
        self.assertIs(out, items)

    def test_only_the_trailing_run_of_several_is_demoted(self):
        items = [reasoning(), message(), message('新问题'), reasoning(), reasoning()]
        out = adapter.Adapter.demote_trailing_reasoning(items, 'claude-opus-5-5')
        # Both trailing chains become their own summary message; the earlier turn is intact.
        self.assertEqual([i['type'] for i in out],
                         ['reasoning', 'message', 'message', 'message', 'message'])

    def test_a_chain_with_no_summary_is_dropped_not_replayed(self):
        items = [message('问'), reasoning(summary=None)]
        out = adapter.Adapter.demote_trailing_reasoning(items, 'claude-opus-5-5')
        self.assertEqual([i['type'] for i in out], ['message'])

    def test_provider_native_reasoning_is_left_alone(self):
        # DeepSeek and Gemini routes require their own reasoning_text on the follow-up.
        for model in ('deepseek-flash', 'gemini-3.8-flash'):
            with self.subTest(model=model):
                items = [message('问'), native_reasoning()]
                out = adapter.Adapter.demote_trailing_reasoning(items, model)
                self.assertIs(out, items)

    def test_other_vendors_are_not_touched(self):
        for model in ('gemini-3.8-flash', 'x-ai/grok-4.7', 'gpt-6-astra'):
            with self.subTest(model=model):
                items = [message('问'), reasoning(blob='gAAAAAbcd')]
                out = adapter.Adapter.demote_trailing_reasoning(items, model)
                self.assertIs(out, items)

    def test_untouched_paths_return_the_same_list(self):
        for items in ([], [message()], [message(), {'type': 'function_call', 'name': 'ls'}]):
            with self.subTest(items=items):
                self.assertIs(adapter.Adapter.demote_trailing_reasoning(items, 'claude-opus-5-5'), items)


def empty_message(text=None):
    part = {'type': 'output_text'}
    if text is not None:
        part['text'] = text
    return {'type': 'message', 'role': 'assistant', 'content': [part]}


def function_call():
    return {'type': 'function_call', 'name': 'ls', 'call_id': 'c1', 'arguments': '{}'}


class TrailingRunIsTransparent(unittest.TestCase):
    """The review's live repro: an empty assistant message after the chain still 400s.

    ``[user, function_call, function_call_output, reasoning, empty assistant]`` answered
    ``The final block in an assistant message cannot be thinking`` against the deployed
    adapter, because the walk stopped on the empty message and left the chain in place.
    """

    def test_chain_before_an_empty_message_is_still_demoted(self):
        items = [message('问'), function_call(), reasoning(), empty_message()]
        out = adapter.Adapter.demote_trailing_reasoning(items, 'claude-opus-5-5')
        self.assertEqual([i['type'] for i in out],
                         ['message', 'function_call', 'message', 'message'])
        self.assertFalse(any(i.get('type') == 'reasoning' for i in out))

    def test_whitespace_only_text_is_empty_too(self):
        items = [reasoning(), empty_message('   \n  ')]
        out = adapter.Adapter.demote_trailing_reasoning(items, 'claude-opus-5-5')
        self.assertFalse(any(i.get('type') == 'reasoning' for i in out))

    def test_a_message_with_an_image_is_not_empty(self):
        items = [reasoning(), {'type': 'message', 'role': 'assistant',
                               'content': [{'type': 'output_image', 'image_url': 'x'}]}]
        out = adapter.Adapter.demote_trailing_reasoning(items, 'claude-opus-5-5')
        self.assertIs(out, items)

    def test_real_text_still_stops_the_walk(self):
        items = [reasoning(), empty_message(), message('答案')]
        out = adapter.Adapter.demote_trailing_reasoning(items, 'claude-opus-5-5')
        self.assertIs(out, items)

    def test_a_user_message_is_never_ignorable(self):
        self.assertFalse(adapter.Adapter.is_ignorable_assistant_message(
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': ''}]}))
        self.assertFalse(adapter.Adapter.is_ignorable_assistant_message(function_call()))


if __name__ == '__main__':
    unittest.main()
