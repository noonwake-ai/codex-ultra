"""Inter-agent message delivery on routed third-party providers.

A team message reaches the model as an ``agent_message`` item whose body is a content
part typed ``encrypted_content``. Native Responses routes decode that part; a routed
provider only understands plain message text, so the header ("Message Type: NEW_TASK
... Payload:") arrived while the body was dropped, and the receiving agent answered
"no task payload". Observed on the live route: every child of a third-party-model
thread reported an empty assignment, while the same spawn on an OpenAI-model thread
worked. These tests pin the local rewrite that delivers the readable body as text.
"""
import unittest

from adapter import (agent_message_text, agent_payload_is_opaque, inline_agent_messages)
from test_upstream_upload import body_of, make, post

HEADER = 'Message Type: NEW_TASK\nTask name: /root/probe\nSender: /root\nPayload:\n'
FERNET = 'gAAAAABqv0aV' + 'A' * 240
LONG_BASE64 = 'QmFzZTY0' * 100
LONG_CJK = '这是一条没有任何空格的长中文载荷用来确认不会被误判成不透明状态' * 40


def agent_message(body=None, parts=None, header=HEADER):
    content = [{'type': 'input_text', 'text': header}]
    if parts is None:
        content.append({'type': 'encrypted_content', 'encrypted_content': body})
    else:
        content.extend(parts)
    return {'type': 'agent_message', 'id': 'amsg_1', 'author': '/root',
            'recipient': '/root/probe', 'content': content}


def user(text):
    return {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}


class PayloadReadingTests(unittest.TestCase):
    def test_plain_text_body_is_readable(self):
        text = agent_message_text(agent_message('只回复 PROBE-9M4X'))
        self.assertIn('NEW_TASK', text)
        self.assertIn('PROBE-9M4X', text)

    def test_fernet_blob_is_opaque(self):
        self.assertTrue(agent_payload_is_opaque(FERNET))

    def test_long_base64_blob_is_opaque(self):
        self.assertTrue(agent_payload_is_opaque(LONG_BASE64))

    def test_long_body_without_spaces_is_still_text(self):
        self.assertFalse(agent_payload_is_opaque(LONG_CJK))

    def test_empty_value_is_not_opaque(self):
        for value in ('', None, 7):
            self.assertFalse(agent_payload_is_opaque(value))

    def test_header_only_message_has_no_body(self):
        self.assertIsNone(agent_message_text(agent_message(parts=[])))

    def test_inline_text_message_is_not_a_payload_rewrite(self):
        item = agent_message(parts=[{'type': 'input_text', 'text': '已经内联的正文'}])
        self.assertIsNone(agent_message_text(item))


class InlineTests(unittest.TestCase):
    def test_message_becomes_user_text_and_keeps_order(self):
        items = [user('前一条'), agent_message('正文甲'), user('后一条')]
        out = inline_agent_messages(items)
        self.assertEqual([i['type'] for i in out], ['message', 'message', 'message'])
        self.assertEqual(out[1]['role'], 'user')
        self.assertIn('正文甲', out[1]['content'][0]['text'])
        self.assertEqual(out[0] is items[0], True)
        self.assertEqual(out[2] is items[2], True)

    def test_untouched_list_is_returned_identical(self):
        items = [user('只有普通消息')]
        self.assertIs(inline_agent_messages(items), items)

    def test_opaque_message_is_left_alone(self):
        item = agent_message(FERNET)
        out = inline_agent_messages([item])
        self.assertIs(out[0], item)

    def test_non_list_input_is_returned_unchanged(self):
        self.assertEqual(inline_agent_messages(None), None)


class ForwardedWireTests(unittest.TestCase):
    def setUp(self):
        self.adapter, self.transport, self.server = make()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def forwarded(self):
        return body_of(self.transport.calls[0])

    def test_readable_team_message_reaches_the_upstream_as_text(self):
        response = post(self.server, {'model': 'deepseek-flash', 'stream': False,
                                      'input': [agent_message('只回复 PROBE-9M4X'), user('继续')]})
        self.assertEqual(response.status_code, 200)
        forwarded = self.forwarded()
        self.assertNotIn('agent_message', [i.get('type') for i in forwarded['input']])
        delivered = forwarded['input'][0]
        self.assertEqual(delivered['role'], 'user')
        self.assertIn('PROBE-9M4X', delivered['content'][0]['text'])

    def test_opaque_team_message_keeps_its_wire_shape(self):
        response = post(self.server, {'model': 'deepseek-flash', 'stream': False,
                                      'input': [agent_message(FERNET), user('继续')]})
        self.assertEqual(response.status_code, 200)
        forwarded = self.forwarded()
        self.assertEqual(forwarded['input'][0]['type'], 'agent_message')
        self.assertEqual(forwarded['input'][0]['content'][1]['encrypted_content'], FERNET)


if __name__ == '__main__':
    unittest.main()
