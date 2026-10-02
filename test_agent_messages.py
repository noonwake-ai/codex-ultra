"""Inter-agent message delivery on routed third-party providers.

A team message reaches the model as an ``agent_message`` item. Real traffic uses two
shapes, and both are dropped by a routed provider that only understands plain text:

* ``input_text`` header + body in an ``encrypted_content`` part (a new task), and
* ``input_text`` header + body in the *same* text part (a child's final answer).

Native Responses routes decode the body either way; a routed provider forwards neither,
so the receiving agent answered "no task payload" and a parent never saw a child's
answer. Observed on the live route: every child of a third-party-model thread reported an
empty assignment, while the same spawn on an OpenAI-model thread worked. These tests pin
the local rewrite that delivers a readable body as ordinary message text.
"""
import json
import unittest

from adapter import (agent_message_body, agent_message_is_opaque, agent_message_text,
                     agent_payload_is_opaque, agent_sealed_notice, inline_agent_messages)
from test_upstream_upload import FakeResponse, SSE_200, body_of, make, post

HEADER = 'Message Type: NEW_TASK\nTask name: /root/probe\nSender: /root\nPayload:\n'
ANSWER_HEADER = 'Message Type: FINAL_ANSWER\nTask name: /root\nSender: /root/probe\nPayload:\n'
FERNET = 'gAAAAABqv0aV' + 'A' * 240
LONG_BASE64 = 'QmFzZTY0' * 100
LONG_CJK = '这是一条没有任何空格的长中文载荷用来确认不会被误判成不透明状态' * 40
FERNET_PROSE = 'gAAAAABqv0aV is what my config prints; is that a Fernet token?'


def agent_message(body=None, parts=None, header=HEADER, author='/root', recipient='/root/probe'):
    content = [{'type': 'input_text', 'text': header}]
    if parts is None:
        content.append({'type': 'encrypted_content', 'encrypted_content': body})
    else:
        content.extend(parts)
    return {'type': 'agent_message', 'id': 'amsg_1', 'author': author,
            'recipient': recipient, 'content': content}


def inline_answer(text, header=ANSWER_HEADER):
    """A message whose body travels inside the text part, as real traffic sends it."""
    return agent_message(parts=[{'type': 'input_text', 'text': header + text}],
                         author='/root/probe', recipient='/root')


def user(text):
    return {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}


class PayloadReadingTests(unittest.TestCase):
    def test_plain_text_body_is_readable(self):
        text = agent_message_text(agent_message('只回复 PROBE-9M4X'))
        self.assertIn('NEW_TASK', text)
        self.assertIn('PROBE-9M4X', text)

    def test_text_only_message_is_readable(self):
        text = agent_message_text(inline_answer('子代理的结论：一切正常'))
        self.assertIn('FINAL_ANSWER', text)
        self.assertIn('一切正常', text)

    def test_body_after_header_is_extracted(self):
        self.assertEqual(agent_message_body(HEADER + '正文'), '正文')
        self.assertEqual(agent_message_body(HEADER), '')
        self.assertEqual(agent_message_body('没有标题'), '没有标题')

    def test_fernet_blob_is_opaque(self):
        self.assertTrue(agent_payload_is_opaque(FERNET))

    def test_long_base64_blob_is_opaque(self):
        self.assertTrue(agent_payload_is_opaque(LONG_BASE64))

    def test_prose_starting_with_fernet_prefix_is_readable(self):
        self.assertFalse(agent_payload_is_opaque(FERNET_PROSE))
        self.assertIsNotNone(agent_message_text(agent_message(FERNET_PROSE)))

    def test_long_body_without_spaces_is_still_text(self):
        self.assertFalse(agent_payload_is_opaque(LONG_CJK))

    def test_empty_value_is_not_opaque(self):
        for value in ('', None, 7):
            self.assertFalse(agent_payload_is_opaque(value))

    def test_header_only_message_has_no_body(self):
        self.assertIsNone(agent_message_text(agent_message(parts=[])))

    def test_message_without_content_is_ignored(self):
        self.assertIsNone(agent_message_text({'type': 'agent_message', 'id': 'amsg_2'}))


class InlineTests(unittest.TestCase):
    def test_message_becomes_user_text_and_keeps_order(self):
        items = [user('前一条'), agent_message('正文甲'), user('后一条')]
        out = inline_agent_messages(items)
        self.assertEqual([i['type'] for i in out], ['message', 'message', 'message'])
        self.assertEqual(out[1]['role'], 'user')
        self.assertIn('正文甲', out[1]['content'][0]['text'])
        self.assertIs(out[0], items[0])
        self.assertIs(out[2], items[2])

    def test_text_only_message_becomes_user_text(self):
        out = inline_agent_messages([inline_answer('结论已交付')])
        self.assertEqual(out[0]['type'], 'message')
        self.assertIn('结论已交付', out[0]['content'][0]['text'])

    def test_untouched_list_is_returned_identical(self):
        items = [user('只有普通消息')]
        self.assertIs(inline_agent_messages(items), items)

    def test_opaque_message_is_left_alone(self):
        item = agent_message(FERNET)
        out = inline_agent_messages([item])
        self.assertIs(out[0], item)

    def test_counters_report_inlined_and_left_alone(self):
        counters = {}
        inline_agent_messages([agent_message('正文'), agent_message(FERNET),
                               inline_answer('结论')], counters)
        self.assertEqual(counters, {'inlined': 2, 'left_alone': 1, 'left_opaque': 1})

    def test_opaque_and_unreadable_are_told_apart(self):
        counters = {}
        inline_agent_messages([agent_message(FERNET), agent_message(parts=[])], counters)
        self.assertEqual(counters, {'left_alone': 2, 'left_opaque': 1, 'left_unreadable': 1})
        self.assertTrue(agent_message_is_opaque(agent_message(FERNET)))
        self.assertFalse(agent_message_is_opaque(agent_message(parts=[])))

    def test_counters_are_optional(self):
        self.assertIsNotNone(inline_agent_messages([agent_message('正文')], None))

    def test_non_list_input_is_returned_unchanged(self):
        self.assertEqual(inline_agent_messages(None), None)


class SealedShapeTests(unittest.TestCase):
    """Shapes observed in real traffic, plus the ones that could silently lose a body."""

    def test_empty_encrypted_part_counts_as_sealed(self):
        item = agent_message('')
        self.assertTrue(agent_message_is_opaque(item))
        self.assertIsNone(agent_message_text(item))

    def test_ciphertext_inside_the_text_part_counts_as_sealed(self):
        item = agent_message(parts=[{'type': 'input_text', 'text': HEADER + FERNET}])
        self.assertTrue(agent_message_is_opaque(item))
        self.assertIsNone(agent_message_text(item))

    def test_readable_body_after_the_header_stays_readable(self):
        item = agent_message(parts=[{'type': 'input_text', 'text': HEADER + '只回复 PROBE-1'}])
        self.assertFalse(agent_message_is_opaque(item))
        self.assertIn('PROBE-1', agent_message_text(item))

    def test_ciphertext_without_a_header_counts_as_sealed(self):
        item = agent_message(parts=[{'type': 'input_text', 'text': FERNET}])
        self.assertTrue(agent_message_is_opaque(item))
        self.assertIsNone(agent_message_text(item))
        notice = agent_sealed_notice(item)
        self.assertIn('cannot read native encrypted state', notice)
        self.assertNotIn(FERNET, notice)

    def test_wrapped_ciphertext_counts_as_sealed(self):
        wrapped = '\n'.join(FERNET[i:i + 64] for i in range(0, len(FERNET), 64))
        item = agent_message(parts=[{'type': 'input_text', 'text': HEADER + wrapped}])
        self.assertTrue(agent_message_is_opaque(item))
        self.assertIsNone(agent_message_text(item))
        self.assertNotIn(FERNET[:64], agent_sealed_notice(item))

    def test_readable_line_beside_ciphertext_in_one_part_survives(self):
        # Real shape: one text part holding a readable sentence and a sealed blob.
        item = agent_message(parts=[{'type': 'input_text',
                                     'text': HEADER + '结论：预览已验证，未发布。\n' + FERNET}])
        self.assertTrue(agent_message_is_opaque(item))
        notice = agent_sealed_notice(item)
        self.assertIn('预览已验证', notice)
        self.assertIn('cannot read native encrypted state', notice)
        self.assertNotIn(FERNET[:64], notice)
        self.assertIsNone(agent_message_text(item))

    def test_notice_keeps_a_readable_body_sitting_next_to_sealed_state(self):
        item = agent_message(parts=[
            {'type': 'encrypted_content', 'encrypted_content': '会议结论：预览已验证，未发布。'},
            {'type': 'input_text', 'text': HEADER.rstrip(chr(10))},
            {'type': 'encrypted_content', 'encrypted_content': FERNET}])
        notice = agent_sealed_notice(item)
        self.assertIn('预览已验证', notice)
        self.assertIn('cannot read native encrypted state', notice)
        self.assertNotIn(FERNET, notice)


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

    def test_text_only_team_message_reaches_the_upstream_as_text(self):
        response = post(self.server, {'model': 'deepseek-flash', 'stream': False,
                                      'input': [inline_answer('子代理的结论'), user('继续')]})
        self.assertEqual(response.status_code, 200)
        forwarded = self.forwarded()
        self.assertNotIn('agent_message', [i.get('type') for i in forwarded['input']])
        self.assertIn('子代理的结论', forwarded['input'][0]['content'][0]['text'])

    def test_opaque_team_message_keeps_its_wire_shape_on_a_native_route(self):
        adapter, transport, server = make(responses=[FakeResponse(200, SSE_200)])
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        response = post(server, {'model': 'gpt-6-sol', 'stream': False,
                                 'input': [agent_message(FERNET), user('继续')]})
        self.assertEqual(response.status_code, 200)
        forwarded = body_of(transport.calls[0])
        self.assertEqual(forwarded['input'][0]['type'], 'agent_message')
        self.assertEqual(forwarded['input'][0]['content'][1]['encrypted_content'], FERNET)
        self.assertEqual(adapter.stats.get('agent_messages_left_opaque'), 1)
        self.assertEqual(adapter.stats.get('agent_messages_notice_opaque'), 0)

    def test_sealed_body_becomes_a_readable_notice_on_a_third_party_route(self):
        """A sealed body cannot be read off-route, and dropping it silently is worse.

        The receiver keeps the header and is told the payload exists; pasting the
        ciphertext in would add bytes without meaning, and sending nothing produced
        agents that answered they had been given no task at all.
        """
        response = post(self.server, {'model': 'deepseek-flash', 'stream': False,
                                      'input': [agent_message(FERNET), user('继续')]})
        self.assertEqual(response.status_code, 200)
        forwarded = self.forwarded()
        self.assertNotIn('agent_message', [i.get('type') for i in forwarded['input']])
        delivered = forwarded['input'][0]
        self.assertEqual(delivered['role'], 'user')
        text = delivered['content'][0]['text']
        self.assertIn('NEW_TASK', text)
        self.assertIn('/root/probe', text)
        self.assertIn('cannot read native encrypted state', text)
        self.assertNotIn(FERNET, text)
        self.assertEqual(self.adapter.stats.get('agent_messages_notice_opaque'), 1)
        self.assertEqual(self.adapter.stats.get('agent_messages_left_opaque'), 0)

    def test_notice_route_still_inlines_readable_bodies(self):
        post(self.server, {'model': 'deepseek-flash', 'stream': False,
                           'input': [agent_message('正文'), inline_answer('结论'),
                                     agent_message(FERNET)]})
        self.assertEqual(self.adapter.stats.get('agent_messages_inlined'), 2)
        self.assertEqual(self.adapter.stats.get('agent_messages_notice_opaque'), 1)

    def test_empty_sealed_part_becomes_a_notice_on_a_third_party_route(self):
        # A header with an empty body used to sit in the request as an unreadable item.
        response = post(self.server, {'model': 'deepseek-flash', 'stream': False,
                                      'input': [agent_message(''), user('继续')]})
        self.assertEqual(response.status_code, 200)
        forwarded = self.forwarded()
        self.assertNotIn('agent_message', [i.get('type') for i in forwarded['input']])
        self.assertIn('cannot read native encrypted state',
                      forwarded['input'][0]['content'][0]['text'])
        self.assertEqual(self.adapter.stats.get('agent_messages_notice_opaque'), 1)
        self.assertEqual(self.adapter.stats.get('agent_messages_left_unreadable'), 0)

    def test_ciphertext_in_the_text_part_is_not_forwarded_to_a_third_party_route(self):
        response = post(self.server, {'model': 'deepseek-flash', 'stream': False,
                                      'input': [agent_message(parts=[{'type': 'input_text',
                                                                      'text': HEADER + FERNET}])]})
        self.assertEqual(response.status_code, 200)
        forwarded = self.forwarded()
        self.assertNotIn(FERNET, json.dumps(forwarded))
        self.assertEqual(self.adapter.stats.get('agent_messages_notice_opaque'), 1)

    def test_delivery_is_counted(self):
        post(self.server, {'model': 'deepseek-flash', 'stream': False,
                           'input': [agent_message('正文'), inline_answer('结论')]})
        self.assertEqual(self.adapter.stats.get('agent_messages_inlined'), 2)
        # A zero must be published too, so a "must be 0" monitor cannot pass by absence.
        self.assertEqual(self.adapter.stats.get('agent_messages_left_alone'), 0)

    def test_native_model_routes_also_get_text_delivery(self):
        """Deliberate: the rewrite is not gated on the model name.

        A ``gpt-*`` id is not proof that the upstream decodes Responses items — the same
        id can be relayed to a third-party upstream that drops them. Inlining on every
        route keeps the body reachable; only the item shape the model sees changes.
        """
        adapter, transport, server = make(responses=[FakeResponse(200, SSE_200)] * 2)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        for model in ('gpt-6.1-sol', 'gpt-6-sol'):
            with self.subTest(model=model):
                response = post(server, {'model': model, 'stream': False,
                                         'input': [inline_answer('原生路由也要送达')]})
                self.assertEqual(response.status_code, 200)
                forwarded = body_of(transport.calls[-1])
                self.assertNotIn('agent_message', [i.get('type') for i in forwarded['input']])
                self.assertIn('原生路由也要送达', forwarded['input'][0]['content'][0]['text'])


class RecordingStrategy:
    """Captures the evidence the compactor is asked to summarise."""

    def __init__(self):
        self.evidence = None

    def compact(self, evidence, client, options):
        self.evidence = evidence
        return {'summary': 'summary body', 'metadata': {'strategy_version': 'test'}}


class CompactPathTests(unittest.TestCase):
    def test_compaction_evidence_carries_the_team_message_text(self):
        adapter, transport, server = make()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        adapter.strategy = RecordingStrategy()
        body = {'model': 'deepseek-flash',
                'input': [agent_message('压缩前必须看到的正文'), inline_answer('压缩前的结论')]}
        item, _ = adapter.compact(body, {})
        self.assertEqual(item['type'], 'compaction')
        evidence = json.dumps(adapter.strategy.evidence, ensure_ascii=False)
        self.assertIn('压缩前必须看到的正文', evidence)
        self.assertIn('压缩前的结论', evidence)
        self.assertNotIn('agent_message', evidence)


if __name__ == '__main__':
    unittest.main()
