"""A valid terminal SSE event completes compaction without waiting for TCP EOF."""
import json
import secrets
import unittest

import requests

from adapter import Adapter
from test_native_checkpoint import CFG, TOKEN
from test_compaction_failure import terminal


class StreamResponse:
    status_code = 200

    def __init__(self, events):
        self.events = events
        self.advanced_after_terminal = False
        self.closed = False
        self.yielded = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True
        return False

    def iter_lines(self, **kwargs):
        for event in self.events:
            self.yielded += 1
            yield b"data: " + json.dumps(event).encode()
        self.advanced_after_terminal = True
        raise requests.ConnectionError("Synthetic connection close failure after terminal")


class Transport:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return self.response


class CompactorStreamTests(unittest.TestCase):
    def call(self, events):
        response = StreamResponse(events)
        transport = Transport(response)
        adapter = Adapter(CFG, secrets.token_bytes(32), transport, lambda: TOKEN)
        payload = {"model": "gpt-6-sol", "reasoning": {"effort": "medium"},
                   "input": [{"role": "user", "content": "Synthetic compaction source."}],
                   "max_output_tokens": 16000}
        return adapter, response, transport, payload

    def test_completed_does_not_advance_into_post_terminal_connection_error(self):
        event = terminal()
        adapter, response, transport, payload = self.call([event])
        result = adapter.call_compactor(payload, {})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"], event["response"]["output"])
        self.assertEqual(response.yielded, 1)
        self.assertFalse(response.advanced_after_terminal)
        self.assertTrue(response.closed)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(adapter.stats["last_compaction"]["status"], "completed")

    def test_completed_uses_done_items_without_waiting_for_eof(self):
        item = terminal()["response"]["output"][0]
        event = terminal()
        event["response"]["output"] = []
        adapter, response, transport, payload = self.call([
            {"type": "response.output_item.done", "item": item}, event,
        ])
        result = adapter.call_compactor(payload, {})
        self.assertEqual(result["output"], [item])
        self.assertEqual(response.yielded, 2)
        self.assertFalse(response.advanced_after_terminal)
        self.assertTrue(response.closed)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(adapter.stats["last_compaction"]["visible_chars"], len(item["content"][0]["text"]))

    def test_failed_or_incomplete_terminal_still_raises_and_closes(self):
        for event_type, status in (("response.incomplete", "incomplete"), ("response.failed", "failed")):
            with self.subTest(event_type=event_type):
                event = terminal(event_type, status, incomplete_details={"reason": "max_output_tokens"})
                adapter, response, transport, payload = self.call([event, terminal()])
                with self.assertRaisesRegex(RuntimeError, "^compactor_incomplete$"):
                    adapter.call_compactor(payload, {})
                self.assertEqual(response.yielded, 1)
                self.assertFalse(response.advanced_after_terminal)
                self.assertTrue(response.closed)
                self.assertEqual(transport.calls, 1)
                self.assertEqual(adapter.stats["last_compaction"]["status"], status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
