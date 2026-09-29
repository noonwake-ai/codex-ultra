"""Offline regressions for complete-history compaction and safe failure evidence."""
import copy
import json
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapter import Adapter
from test_native_checkpoint import CFG, TOKEN, FakeTransport, completion


def sent_body(kwargs):
    """Decode the request body from either transport form.

    The adapter may hand `requests` a `json=` structure or pre-serialized `data=`, and it
    may compress that data with zstd. Protocol tests should not care which, so they read
    every forwarded body through this helper.
    """
    if "json" in kwargs:
        return kwargs["json"]
    data = kwargs.get("data")
    if isinstance(data, (bytes, bytearray)):
        import zstandard
        try:
            data = zstandard.ZstdDecompressor().decompress(data)
        except Exception:
            pass
        return json.loads(bytes(data).decode("utf-8"))
    return data


PARTIAL = "Synthetic partial handoff; this text must never enter diagnostics."
PRIVATE = "synthetic-sensitive-content-not-for-telemetry"


def terminal(event="response.completed", status="completed", text="Synthetic complete handoff.", **fields):
    return {
        "type": event,
        "response": {
            "status": status,
            "model": "gpt-6-sol",
            "service_tier": "priority",
            "output": [{
                "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }],
            **fields,
        },
    }


class CompactionFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="compaction-failure-test-")
        self.addCleanup(self.temp.cleanup)
        self.cfg = {**CFG, "native_cache_dir": self.temp.name}
        self.key = secrets.token_bytes(32)

    def adapter(self, responses):
        transport = FakeTransport(responses)
        return Adapter(self.cfg, self.key, transport, lambda: TOKEN), transport

    def body(self):
        return {
            "model": "deepseek-flash",
            "instructions": "Synthetic contract: preview only; do not publish.",
            "input": [{"role": "user", "content": PRIVATE}, {"type": "compaction_trigger"}],
        }

    def test_full_native_to_four_tools_pipeline_keeps_original_history_after_incomplete(self):
        native = {"type": "compaction", "id": "cmp_synthetic_full", "encrypted_content": "opaque-synthetic-full"}
        media = {"role": "user", "content": [{"type": "input_image", "image_url": "data:image/png;base64,synthetic"}]}
        pending = {"type": "function_call", "call_id": "call_pending", "name": "verify", "arguments": "{}"}
        body = self.body()
        body["input"] = [{"role": "user", "content": "Synthetic earliest scope."}, native]
        for index in range(4):
            body["input"].extend([
                {"type": "function_call", "call_id": "call_%d" % index,
                 "name": "synthetic_inspect", "arguments": json.dumps({"revision": index})},
                {"type": "function_call_output", "call_id": "call_%d" % index,
                 "output": "Synthetic result %d: " % index + ("long-history evidence; " * 2048)},
            ])
        body["input"].extend([media, pending, {"role": "user", "content": "Synthetic latest instruction."}, {"type": "compaction_trigger"}])
        original = copy.deepcopy(body)
        failed = terminal(
            "response.incomplete", "incomplete", PARTIAL,
            incomplete_details={"reason": "max_output_tokens"},
            usage={"input_tokens": 50000, "output_tokens": 16000, "total_tokens": 66000,
                   "output_tokens_details": {"reasoning_tokens": 15990}},
        )
        partial_event = {"type": "response.output_item.done", "item": failed["response"]["output"][0]}
        adapter, transport = self.adapter([[completion()], [partial_event, failed], [terminal()]])

        with self.assertRaisesRegex(RuntimeError, "^compactor_incomplete$"):
            adapter.compact(body, {})
        self.assertEqual(body, original)
        self.assertEqual(adapter.stats["compactions"], 0)
        self.assertEqual(adapter.stats["native_exports"], 1)
        self.assertEqual(adapter.stats["last_compaction"]["incomplete_reason"], "max_output_tokens")
        self.assertEqual(adapter.stats["last_compaction"]["usage"]["reasoning_tokens"], 15990)
        self.assertEqual(len(list(Path(self.temp.name).glob("*.checkpoint"))), 1)

        native_request = sent_body(transport.calls[0])
        self.assertIn(native, native_request["input"])
        b_request = sent_body(transport.calls[1])
        self.assertEqual(b_request["model"], "gpt-6-sol")
        self.assertEqual(b_request["reasoning"]["effort"], "medium")
        b_text = b_request["input"][0]["content"][0]["text"]
        raw = b_text.split("\n", 1)[1].rsplit("\nEND RAW RESPONSES HISTORY", 1)[0]
        evidence = json.loads(raw)
        self.assertGreater(len(b_text), 150000)
        self.assertEqual(evidence[0], {"role": "developer", "content": body["instructions"]})
        for item in original["input"][2:10]:
            self.assertEqual(evidence.count(item), 1)
        self.assertIn(pending, evidence)
        self.assertIn({"role": "user", "content": "Synthetic latest instruction."}, evidence)
        self.assertNotIn("opaque-synthetic-full", b_text)
        self.assertNotIn("compaction_trigger", b_text)
        self.assertNotIn("base64", b_text)
        self.assertNotIn(PARTIAL, json.dumps(adapter.stats))

        # An explicit subsequent call succeeds, proving failure did not consume
        # or replace caller state, poison the valid native cache, or stick the slot.
        checkpoint, _ = adapter.compact(body, {})
        opened = adapter.open(checkpoint["encrypted_content"])
        self.assertEqual(opened["summary"], "Synthetic complete handoff.")
        self.assertEqual(opened["retained"], [media, pending])
        self.assertEqual(body, original)
        self.assertEqual(adapter.stats["compactions"], 1)
        self.assertEqual(adapter.stats["native_cache_hits"], 1)
        self.assertEqual(len(transport.calls), 3)

    def test_incomplete_records_allowlisted_terminal_measurements_before_raise(self):
        event = terminal(
            "response.incomplete", "incomplete", PARTIAL,
            id=PRIVATE, incomplete_details={"reason": "max_output_tokens", "message": PRIVATE},
            error={"code": "server_error", "message": TOKEN},
            usage={"input_tokens": 50000, "output_tokens": 16000, "total_tokens": 66000,
                   "input_tokens_details": {"cached_tokens": 40000, "secret": PRIVATE},
                   "output_tokens_details": {"reasoning_tokens": 15990, "secret": PRIVATE}},
        )
        adapter, transport = self.adapter([[event]])
        adapter.stats["last_compaction"] = {"status": "completed", "seconds": 1}
        with patch("adapter.time") as clock:
            clock.monotonic.side_effect = [1000, 1260, 1260]
            with self.assertRaisesRegex(RuntimeError, "^compactor_incomplete$"):
                adapter.compact(self.body(), {})
        diagnostic = adapter.stats["last_compaction"]
        self.assertEqual(diagnostic["event_type"], "response.incomplete")
        self.assertEqual(diagnostic["status"], "incomplete")
        self.assertEqual(diagnostic["incomplete_reason"], "max_output_tokens")
        self.assertEqual(diagnostic["error_code"], "server_error")
        self.assertEqual(diagnostic["usage"], {"input_tokens": 50000, "output_tokens": 16000,
                         "total_tokens": 66000, "cached_tokens": 40000, "reasoning_tokens": 15990})
        self.assertEqual(diagnostic["visible_chars"], len(PARTIAL))
        self.assertEqual(diagnostic["seconds"], 260)
        self.assertEqual(diagnostic["max_output_tokens"], 16000)
        self.assertEqual(diagnostic["input_chars"], len(json.dumps(
            sent_body(transport.calls[0])["input"], ensure_ascii=False, separators=(",", ":"))))
        for forbidden in (PRIVATE, PARTIAL, TOKEN, "message", "encrypted_content"):
            self.assertNotIn(forbidden, json.dumps(diagnostic))

    def test_invalid_completed_terminals_cannot_create_checkpoint(self):
        variants = {
            "missing_status": {},
            "running": {"status": "in_progress"},
            "incomplete": {"status": "incomplete"},
            "failed": {"status": "failed"},
            "error": {"status": "completed", "error": {"code": "server_error", "message": PRIVATE}},
            "empty_error": {"status": "completed", "error": {}},
            "incomplete_details": {"status": "completed", "incomplete_details": {"reason": "max_output_tokens"}},
            "empty_incomplete_details": {"status": "completed", "incomplete_details": {}},
        }
        for label, fields in variants.items():
            with self.subTest(label=label):
                event = terminal()
                event["response"].pop("status")
                event["response"].update(fields)
                adapter, _ = self.adapter([[event]])
                body = self.body()
                original = copy.deepcopy(body)
                with self.assertRaisesRegex(RuntimeError, "^compactor_incomplete$"):
                    adapter.compact(body, {})
                self.assertEqual(body, original)
                self.assertEqual(adapter.stats["compactions"], 0)
                self.assertEqual(adapter.stats["last_compaction"]["event_type"], "response.completed")
                self.assertNotIn(PRIVATE, json.dumps(adapter.stats))
        adapter, _ = self.adapter([[terminal(error=None, incomplete_details=None)]])
        checkpoint, _ = adapter.compact(self.body(), {})
        self.assertEqual(adapter.open(checkpoint["encrypted_content"])["summary"], "Synthetic complete handoff.")

    def test_unknown_codes_and_nonnumeric_usage_are_redacted(self):
        event = terminal(
            "response.incomplete", PRIVATE, PARTIAL, model=PRIVATE, service_tier=PRIVATE,
            incomplete_details={"reason": PRIVATE}, error={"code": PRIVATE, "message": TOKEN},
            usage={"input_tokens": PRIVATE, "output_tokens": True, "total_tokens": float("nan"),
                   "input_tokens_details": {"cached_tokens": -1},
                   "output_tokens_details": {"reasoning_tokens": TOKEN}},
        )
        adapter, _ = self.adapter([[event]])
        with self.assertRaisesRegex(RuntimeError, "^compactor_incomplete$"):
            adapter.compact(self.body(), {})
        diagnostic = adapter.stats["last_compaction"]
        for name in ("status", "response_model", "response_tier", "incomplete_reason", "error_code"):
            self.assertEqual(diagnostic[name], "other")
        self.assertEqual(diagnostic["usage"], {})
        for forbidden in (PRIVATE, TOKEN, PARTIAL):
            self.assertNotIn(forbidden, json.dumps(diagnostic))

    def test_stream_error_and_missing_terminal_record_no_success(self):
        cases = [
            ([{"type": "error", "code": "rate_limit_exceeded", "message": PRIVATE}], "compactor_error", "error"),
            ([{"type": "response.output_item.done", "item": terminal(text=PARTIAL)["response"]["output"][0]}],
             "compactor_no_completed_response", "stream.ended"),
            ([terminal("response.failed", "failed", PARTIAL, error={"code": "server_error", "message": PRIVATE})],
             "compactor_incomplete", "response.failed"),
        ]
        for events, code, event_type in cases:
            with self.subTest(code=code, event_type=event_type):
                adapter, _ = self.adapter([events])
                with self.assertRaisesRegex(RuntimeError, "^" + code + "$"):
                    adapter.compact(self.body(), {})
                self.assertEqual(adapter.stats["compactions"], 0)
                self.assertEqual(adapter.stats["last_compaction"]["event_type"], event_type)
                self.assertNotIn(PRIVATE, json.dumps(adapter.stats))
                self.assertNotIn(PARTIAL, json.dumps(adapter.stats))


if __name__ == "__main__":
    unittest.main(verbosity=2)
