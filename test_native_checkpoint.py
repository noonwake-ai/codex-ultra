"""Synthetic, offline regressions for native GPT checkpoint portability."""
import base64
import copy
import json
import secrets
import tempfile
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from unittest.mock import patch
from pathlib import Path

from adapter import Adapter, PREFIX


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


CFG = {
    "upstream": "https://example.invalid",
    "compactor_model": "gpt-6-sol",
    "compactor_effort": "medium",
    "port": 0,
}
TOKEN = "synthetic-native-export-auth-not-a-real-secret"
OPAQUE = "synthetic-native-opaque-checkpoint-with-no-readable-history"
SUMMARY = "Synthetic project ABC-42; preview only; verification remains pending."


def completion(text=None):
    if text is None:
        text = json.dumps({"export_status": "available", "handoff": SUMMARY})
    return {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "model": "gpt-6-sol",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }],
            "usage": {"input_tokens": 10, "output_tokens": 8, "total_tokens": 18},
        },
    }


class FakeResponse:
    status_code = 200

    def __init__(self, events):
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self, **kwargs):
        for event in self.events:
            yield b"data: " + json.dumps(event).encode()


class FakeTransport:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [[completion()]])

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **copy.deepcopy(kwargs)})
        if not self.responses:
            raise AssertionError("Unexpected extra native export")
        return FakeResponse(self.responses.pop(0))


def visible_text(item):
    content = item.get("content", [])
    if isinstance(content, str):
        return content
    return "\n".join(
        part.get("text", "") for part in content if isinstance(part, dict)
    )


class NativeCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native-checkpoint-test-")
        self.addCleanup(self.temp.cleanup)
        self.cfg = {**CFG, "native_cache_dir": self.temp.name}
        self.key = secrets.token_bytes(32)
        self.transport = FakeTransport()
        self.adapter = self.new_adapter(self.transport)
        self.native = {
            "type": "compaction",
            "id": "cmp_synthetic_native",
            "encrypted_content": OPAQUE,
        }

    def new_adapter(self, transport):
        return Adapter(self.cfg, self.key, transport, lambda: TOKEN)

    def assert_checkpoint(self, item):
        self.assertEqual(item.get("role"), "user")
        self.assertIn("<context_checkpoint>", visible_text(item))
        self.assertIn(SUMMARY, visible_text(item))
        self.assertIn("</context_checkpoint>", visible_text(item))

    def assert_encrypted_cache(self):
        files = [p for p in Path(self.temp.name).rglob("*") if p.is_file()]
        self.assertTrue(files, "A successful export must survive adapter restart")
        for path in files:
            data = path.read_bytes()
            self.assertNotIn(OPAQUE.encode(), data)
            self.assertNotIn(SUMMARY.encode(), data)
            self.assertNotIn(TOKEN.encode(), data)
            self.assertNotIn(OPAQUE, path.name)
            self.assertNotIn("ABC-42", path.name)
        return files

    def test_native_export_preserves_typed_state_and_current_tail(self):
        prefix = {"role": "user", "content": "Synthetic scope: local preview only."}
        latest_user = {"role": "user", "content": "Verify the second revision now."}
        call = {
            "type": "function_call", "id": "fc_latest", "call_id": "call_latest",
            "name": "inspect_revision", "arguments": '{"revision":"rev-2"}',
        }
        result = {
            "type": "function_call_output", "call_id": "call_latest",
            "output": '{"revision":"rev-2","verified":true}',
        }
        items = [prefix, self.native, latest_user, call, result]
        original = copy.deepcopy(items)
        expanded = self.adapter.expand(items, "deepseek-flash", headers={"session_id": "synthetic-session"})

        self.assertEqual(items, original, "Expansion must not mutate caller history")
        self.assertEqual(expanded[0], prefix)
        self.assert_checkpoint(expanded[1])
        self.assertEqual(expanded[2:], [latest_user, call, result])
        self.assertNotIn(OPAQUE, json.dumps(expanded))
        self.assertEqual(len(self.transport.calls), 1)
        payload = sent_body(self.transport.calls[0])
        self.assertEqual(payload["model"], "gpt-6-sol")
        self.assertEqual(payload["reasoning"]["effort"], "medium")
        self.assertIn(self.native, payload["input"], "Native state must be a typed input item")
        self.assertFalse(payload.get("tools"))
        self.assertFalse(any(i.get("type") == "compaction_trigger" for i in payload["input"]))
        self.assertNotIn(latest_user, payload["input"])
        self.assertNotIn(call, payload["input"])
        self.assertNotIn(result, payload["input"])
        self.assertEqual(self.adapter.stats["native_exports"], 1)

    def test_same_checkpoint_reuses_cache_with_fresh_tail_after_restart(self):
        first_tail = {"role": "user", "content": "Synthetic first-turn request."}
        first = self.adapter.expand([self.native, first_tail], "deepseek-flash")
        self.assert_checkpoint(first[0])
        self.assertEqual(first[1:], [first_tail])
        self.assert_encrypted_cache()

        current_user = {"role": "user", "content": "Synthetic second-turn request."}
        current_call = {
            "type": "function_call", "call_id": "call_second", "name": "check",
            "arguments": '{"version":2}',
        }
        current_result = {
            "type": "function_call_output", "call_id": "call_second", "output": "version-2-ok",
        }
        second_transport = FakeTransport()
        restarted = self.new_adapter(second_transport)
        second = restarted.expand([self.native, current_user, current_call, current_result], "deepseek-flash")
        self.assertEqual(second[0], first[0])
        self.assertEqual(second[1:], [current_user, current_call, current_result])
        self.assertNotIn(first_tail, second)
        self.assertEqual(second_transport.calls, [])
        self.assertEqual(restarted.stats["native_exports"], 0)
        self.assertEqual(restarted.stats["native_cache_hits"], 1)
        self.assert_encrypted_cache()

    def test_compaction_trigger_dropped_after_native_export(self):
        tail = {"role": "user", "content": "Synthetic current question."}
        expanded = self.adapter.expand(
            [self.native, tail, {"type": "compaction_trigger"}], "deepseek-flash", drop_trigger=True,
        )
        self.assert_checkpoint(expanded[0])
        self.assertEqual(expanded[1:], [tail])
        self.assertFalse(any(i.get("type") == "compaction_trigger" for i in sent_body(self.transport.calls[0])["input"]))

    def test_encrypted_reasoning_keeps_only_visible_summary_for_deepseek(self):
        opaque_reasoning = {
            "type": "reasoning", "id": "rs_synthetic", "encrypted_content": "synthetic-hidden-reasoning-state",
            "summary": [{"type": "summary_text", "text": "Visible brief summary for the user."}],
        }
        empty_reasoning = {
            "type": "reasoning", "id": "rs_empty", "encrypted_content": "synthetic-empty-hidden-state", "summary": [],
        }
        tail = {"role": "user", "content": "Continue the synthetic task."}
        original = copy.deepcopy([opaque_reasoning, empty_reasoning, tail])
        expanded = self.adapter.expand([opaque_reasoning, empty_reasoning, tail], "deepseek-flash")
        self.assertEqual([opaque_reasoning, empty_reasoning, tail], original)
        self.assertEqual(expanded[-1], tail)
        summaries = [i for i in expanded if i.get("role") == "assistant"]
        self.assertEqual(len(summaries), 1)
        self.assertIn("Visible brief summary for the user.", visible_text(summaries[0]))
        self.assertFalse(any(i.get("type") == "reasoning" for i in expanded))
        self.assertNotIn("encrypted_content", json.dumps(expanded))
        self.assertNotIn("synthetic-hidden", json.dumps(expanded))
        self.assertEqual(self.transport.calls, [], "Hidden reasoning must not trigger an export")

    def test_native_gpt_keeps_checkpoint_reasoning_and_trigger_verbatim(self):
        reasoning = {
            "type": "reasoning", "id": "rs_native", "encrypted_content": "synthetic-gpt-reasoning",
            "summary": [{"type": "summary_text", "text": "Synthetic visible summary."}],
        }
        items = [self.native, reasoning, {"type": "compaction_trigger"}, {"role": "user", "content": "Continue."}]
        original = copy.deepcopy(items)
        self.assertEqual(self.adapter.expand(items, "gpt-6-sol"), original)
        self.assertEqual(items, original)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.adapter.stats["native_exports"], 0)

    def test_failed_incomplete_or_empty_export_never_caches_success(self):
        failures = {
            "incomplete": [{"type": "response.incomplete", "response": {"status": "incomplete"}}],
            "failed": [{"type": "response.failed", "response": {"status": "failed"}}],
            "missing_terminal": [{"type": "response.output_item.done", "item": completion()["response"]["output"][0]}],
            "empty": [completion("   ")],
            "empty_handoff": [completion(json.dumps({"export_status": "available", "handoff": "   "}))],
            "unavailable": [completion(json.dumps({"export_status": "unavailable", "handoff": ""}))],
            "cannot_read": [completion("I cannot read checkpoint state; please provide the original history.")],
            "invalid_json": [completion('{"export_status":"available","handoff":')],
            "invalid_envelope": [completion(json.dumps(["available", SUMMARY]))],
            "non_string_handoff": [completion(json.dumps({"export_status": "available", "handoff": 7}))],
            "refusal": [{
                "type": "response.completed", "response": {"status": "completed", "output": [{
                    "type": "message", "role": "assistant", "content": [{
                        "type": "refusal", "refusal": "Cannot export this checkpoint.",
                    }],
                }]},
            }],
            "reasoning_only": [{
                "type": "response.completed", "response": {"status": "completed", "output": [{
                    "type": "reasoning", "summary": [{"type": "summary_text", "text": "Not an assistant handoff."}],
                }]},
            }],
        }
        for label, events in failures.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory(prefix="native-failure-") as cache:
                cfg = {**CFG, "native_cache_dir": cache}
                transport = FakeTransport([events, [completion()]])
                adapter = Adapter(cfg, self.key, transport, lambda: TOKEN)
                with self.assertRaises((RuntimeError, ValueError)):
                    adapter.expand([self.native], "deepseek-flash")
                self.assertEqual(adapter.stats["native_exports"], 0)
                self.assertEqual([p for p in Path(cache).rglob("*") if p.is_file()], [])
                expanded = adapter.expand([self.native], "deepseek-flash")
                self.assert_checkpoint(expanded[0])
                self.assertEqual(len(transport.calls), 2)
                self.assertEqual(adapter.stats["native_exports"], 1)
                self.assertEqual(adapter.stats["native_cache_hits"], 0)

    def test_cache_substitution_or_tamper_fails_closed(self):
        second_native = {**self.native, "id": "cmp_other", "encrypted_content": "synthetic-other-opaque-state"}
        transport = FakeTransport([[completion()], [completion()]])
        adapter = self.new_adapter(transport)
        adapter.expand([self.native], "deepseek-flash")
        first_file = self.assert_encrypted_cache()[0]
        first_bytes = first_file.read_bytes()
        adapter.expand([second_native], "deepseek-flash")
        second_file = next(p for p in self.assert_encrypted_cache() if p != first_file)

        # Valid authenticated bytes for a different native checkpoint cannot be reused.
        first_file.write_bytes(second_file.read_bytes())
        restarted_transport = FakeTransport()
        restarted = self.new_adapter(restarted_transport)
        with self.assertRaisesRegex(ValueError, "native_cache_identity_mismatch"):
            restarted.expand([self.native], "deepseek-flash")
        self.assertEqual(restarted_transport.calls, [])
        self.assertEqual(restarted.stats["native_cache_hits"], 0)

        raw = bytearray(base64.urlsafe_b64decode(first_bytes[len(PREFIX):]))
        raw[-1] ^= 1
        first_file.write_text(PREFIX + base64.urlsafe_b64encode(raw).decode())
        tampered_transport = FakeTransport()
        tampered = self.new_adapter(tampered_transport)
        with self.assertRaisesRegex(ValueError, "checkpoint_integrity_or_key_failure"):
            tampered.expand([self.native], "deepseek-flash")
        self.assertEqual(tampered_transport.calls, [])
        self.assertEqual(tampered.stats["native_cache_hits"], 0)

    def test_concurrent_identical_checkpoint_exports_once_and_keeps_distinct_tails(self):
        entered = threading.Event()
        waiting = threading.Event()
        release = threading.Event()
        calls = []

        class ObservedFuture(Future):
            def result(self, timeout=None):
                waiting.set()
                return super().result(timeout)

        def blocked_compactor(payload, headers, label="compaction"):
            calls.append(copy.deepcopy(payload))
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release synthetic export")
            return completion()["response"]

        self.adapter.call_compactor = blocked_compactor
        first_tail = {"role": "user", "content": "Synthetic concurrent first tail."}
        second_tail = {"role": "user", "content": "Synthetic concurrent second tail."}
        with patch("native_checkpoint.Future", ObservedFuture), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.adapter.expand, [self.native, first_tail], "deepseek-flash")
            try:
                self.assertTrue(entered.wait(5), "First export did not start")
                second = pool.submit(self.adapter.expand, [self.native, second_tail], "deepseek-flash")
                self.assertTrue(waiting.wait(5), "Second request must join the in-flight export")
            finally:
                release.set()
            first_result = first.result(timeout=5)
            second_result = second.result(timeout=5)

        self.assertEqual(len(calls), 1)
        self.assert_checkpoint(first_result[0])
        self.assertEqual(first_result[0], second_result[0])
        self.assertEqual(first_result[1:], [first_tail])
        self.assertEqual(second_result[1:], [second_tail])
        self.assertEqual(self.adapter.stats["native_exports"], 1)
        self.assertEqual(self.adapter.stats["native_cache_hits"], 1)
        self.assertEqual(self.transport.calls, [])
        self.assert_encrypted_cache()

    def test_corrupt_local_checkpoint_fails_closed_without_native_export(self):
        sealed = self.adapter.seal({"version": 2, "summary": "Synthetic local state.", "retained": []})
        raw = bytearray(base64.urlsafe_b64decode(sealed[len(PREFIX):]))
        raw[-1] ^= 1
        bad = PREFIX + base64.urlsafe_b64encode(raw).decode()
        with self.assertRaisesRegex(ValueError, "checkpoint_integrity_or_key_failure"):
            self.adapter.expand([{"type": "compaction", "encrypted_content": bad}], "deepseek-flash")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.adapter.stats["native_exports"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
