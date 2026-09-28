"""Synthetic media-history regressions; no real media or model requests."""
import copy
import json
import secrets
import unittest

from adapter import Adapter
from test_compaction_failure import terminal
from test_native_checkpoint import CFG, TOKEN, FakeTransport


def image(marker="synthetic"):
    return {"type": "input_image", "image_url": "data:image/png;base64," + marker}


def call(call_id, custom=False):
    return {"type": "custom_tool_call" if custom else "function_call",
            "id": "item_" + call_id, "call_id": call_id, "name": "synthetic_tool",
            "input" if custom else "arguments": "{}"}


def result(call_id, parts, custom=False):
    return {"type": "custom_tool_call_output" if custom else "function_call_output",
            "id": "output_" + call_id, "call_id": call_id, "output": parts}


class MediaHistoryTests(unittest.TestCase):
    def adapter(self, count=1):
        transport = FakeTransport([[terminal()] for _ in range(count)])
        return Adapter(CFG, secrets.token_bytes(32), transport, lambda: TOKEN), transport

    def test_message_media_placeholders_preserve_text_types_and_other_parts(self):
        parts = [
            {"type": "input_text", "text": "Synthetic input text."}, image(),
            {"type": "output_text", "text": "Synthetic output text."},
            {"type": "input_file", "filename": "synthetic.pdf", "file_data": "data:application/pdf;base64,synthetic"},
            {"type": "text", "text": "Synthetic text part."},
            {"type": "input_audio", "data": "synthetic-audio"},
            {"type": "input_video", "video_url": "data:video/mp4;base64,synthetic"},
            {"type": "refusal", "refusal": "Synthetic unchanged metadata."},
            "synthetic non-dict metadata",
        ]
        message = {"type": "message", "id": "msg_media", "role": "user", "content": parts}
        original = copy.deepcopy(message)
        evidence, retained = Adapter.prepare_history([message])
        self.assertEqual(message, original)
        self.assertEqual(retained, [message])
        self.assertIs(retained[0], message)
        self.assertEqual({k: v for k, v in evidence[0].items() if k != "content"},
                         {k: v for k, v in message.items() if k != "content"})
        for index, part in enumerate(parts):
            if isinstance(part, dict) and part["type"].startswith("input_") and part["type"] != "input_text":
                self.assertEqual(evidence[0]["content"][index]["type"], part["type"])
                self.assertIn("placeholder", evidence[0]["content"][index])
            else:
                self.assertEqual(evidence[0]["content"][index], part)
        self.assertNotIn("base64", json.dumps(evidence))
        self.assertNotIn("synthetic-audio", json.dumps(evidence))

    def test_function_and_custom_media_outputs_retain_complete_original_pairs(self):
        for custom in (False, True):
            with self.subTest(custom=custom):
                invocation = call("call_mixed", custom)
                output = result("call_mixed", [
                    {"type": "input_text", "text": "Synthetic screenshot evidence."}, image("mixed-image"),
                    {"type": "output_text", "text": "Synthetic output text preserved."},
                    {"type": "input_file", "file_data": "data:application/pdf;base64,mixed-file"},
                ], custom)
                tail = {"role": "user", "content": "Continue from this result."}
                items = [invocation, output, tail]
                original = copy.deepcopy(items)
                evidence, retained = Adapter.prepare_history(items)
                self.assertEqual(items, original)
                self.assertEqual(retained, [invocation, output])
                self.assertIs(retained[0], invocation)
                self.assertIs(retained[1], output)
                self.assertEqual(evidence[0], invocation)
                self.assertEqual(evidence[-1], tail)
                self.assertEqual(evidence[1]["type"], output["type"])
                self.assertEqual(evidence[1]["call_id"], output["call_id"])
                self.assertEqual(evidence[1]["id"], output["id"])
                self.assertEqual(evidence[1]["output"][0], output["output"][0])
                self.assertEqual(evidence[1]["output"][2], output["output"][2])
                self.assertNotIn("base64", json.dumps(evidence))
                self.assertNotIn("mixed-image", json.dumps(evidence))
                self.assertNotIn("mixed-file", json.dumps(evidence))

    def test_interleaved_media_results_close_each_call_once_in_original_order(self):
        first, second, plain, pending = (call("first"), call("second", True), call("plain"), call("pending", True))
        second_result = result("second", [image("second")], True)
        first_result = result("first", [image("first")])
        extra_result = {**result("first", [image("first-extra")]), "id": "output_first_extra"}
        plain_result = result("plain", [{"type": "input_text", "text": "No media here."}])
        message = {"role": "user", "content": [image("message")]}
        items = [first, second, plain, second_result, first_result, extra_result, plain_result, pending, message]
        original = copy.deepcopy(items)
        evidence, retained = Adapter.prepare_history(items)
        self.assertEqual(items, original)
        self.assertEqual(retained, [first, second, second_result, first_result, extra_result, pending, message])
        self.assertEqual(retained.count(first), 1)
        self.assertEqual(retained.count(second), 1)
        self.assertEqual(retained.count(pending), 1)
        self.assertEqual(evidence[2], plain)
        self.assertEqual(evidence[6], plain_result)
        self.assertNotIn("base64", json.dumps(evidence))

    def test_five_large_image_outputs_do_not_enter_textual_compactor_input(self):
        items = [{"role": "user", "content": "Synthetic active goal; use these five screenshots."}]
        original_pairs = []
        for index in range(5):
            pair = [call("large_%d" % index, index % 2 == 1),
                    result("large_%d" % index, [
                        {"type": "input_text", "text": "Screenshot %d interpretation context." % index},
                        image("A" * 65536),
                    ], index % 2 == 1)]
            original_pairs.extend(pair)
            items.extend(pair)
        original = copy.deepcopy(items)
        adapter, transport = self.adapter()
        checkpoint, _ = adapter.compact({"model": "deepseek-flash", "input": items}, {})
        request = transport.calls[0]["json"]
        textual_input = json.dumps(request["input"], ensure_ascii=False)
        self.assertGreater(len(json.dumps(original)), 300000)
        self.assertLess(len(textual_input), 10000)
        self.assertNotIn("base64", textual_input)
        self.assertNotIn("A" * 256, textual_input)
        for index in range(5):
            self.assertIn("Screenshot %d interpretation context." % index, textual_input)
        self.assertEqual(adapter.open(checkpoint["encrypted_content"])["retained"], original_pairs)
        self.assertEqual(items, original)

    def test_reexpanded_media_checkpoint_keeps_completed_and_new_tool_pairs_once(self):
        old_call = call("old")
        old_result = result("old", [{"type": "input_text", "text": "Old media evidence."}, image("old")])
        pending = call("next", True)
        initial = [{"role": "user", "content": "Synthetic original request."}, old_call, old_result, pending]
        initial_copy = copy.deepcopy(initial)
        adapter, transport = self.adapter(count=2)
        first, _ = adapter.compact({"model": "deepseek-flash", "input": initial}, {})
        self.assertEqual(adapter.open(first["encrypted_content"])["retained"], [old_call, old_result, pending])
        next_result = result("next", [{"type": "input_text", "text": "New media evidence."}, image("next")], True)
        tail = {"role": "user", "content": "Synthetic newer instruction."}
        next_items = [first, next_result, tail]
        next_copy = copy.deepcopy(next_items)
        expanded = adapter.expand(next_items, "deepseek-flash")
        self.assertEqual(expanded[1:], [old_call, old_result, pending, next_result, tail])
        evidence, retained = adapter.prepare_history(expanded)
        self.assertEqual(retained, [old_call, old_result, pending, next_result])
        self.assertEqual(evidence[-1], tail)
        second, _ = adapter.compact({"model": "deepseek-flash", "input": next_items}, {})
        self.assertEqual(adapter.open(second["encrypted_content"])["retained"], retained)
        for item in retained:
            self.assertEqual(retained.count(item), 1)
        for request in transport.calls:
            self.assertNotIn("base64", json.dumps(request["json"]["input"]))
        self.assertEqual(initial, initial_copy)
        self.assertEqual(next_items, next_copy)

    def test_explicit_media_and_tool_pairs_suppress_only_checkpoint_copies(self):
        adapter, transport = self.adapter()
        media = {"type": "message", "id": "msg_explicit", "role": "user", "content": [image("explicit")]}
        invocation = call("explicit")
        output = result("explicit", [image("explicit-output")])
        checkpoint = {"type": "compaction", "encrypted_content": adapter.seal({
            "version": 2, "summary": "Synthetic prior context.", "retained": [media, invocation, output],
        })}
        # Canonical comparison must not depend on JSON object key order.
        raw_media, raw_call, raw_output = json.loads(json.dumps([media, invocation, output], sort_keys=True))
        tail = {"role": "user", "content": "Synthetic fresh instruction."}
        items = [raw_media, checkpoint, raw_call, raw_output, tail]
        original = copy.deepcopy(items)
        expanded = adapter.expand(items, "deepseek-flash")
        self.assertEqual(items, original)
        self.assertEqual(expanded[0], raw_media)
        self.assertIn("<context_checkpoint>", expanded[1]["content"][0]["text"])
        self.assertEqual(expanded[2:], [raw_call, raw_output, tail])
        self.assertIs(expanded[0], raw_media)
        self.assertIs(expanded[2], raw_call)
        self.assertIs(expanded[3], raw_output)
        self.assertEqual(transport.calls, [])

    def test_checkpoint_deduplication_preserves_raw_and_retained_multiplicity(self):
        media = {"type": "message", "id": "msg_repeated", "role": "user", "content": [image("repeated")]}
        for retained_count, raw_count in ((1, 2), (2, 1), (2, 2), (3, 1)):
            with self.subTest(retained=retained_count, explicit=raw_count):
                adapter, _ = self.adapter()
                checkpoint = {"type": "compaction", "encrypted_content": adapter.seal({
                    "version": 2, "summary": "Synthetic duplicate count.",
                    "retained": [copy.deepcopy(media) for _ in range(retained_count)],
                })}
                raw = [copy.deepcopy(media) for _ in range(raw_count)]
                items = [checkpoint] + raw
                original = copy.deepcopy(items)
                expanded = adapter.expand(items, "deepseek-flash")
                self.assertEqual(expanded.count(media), max(retained_count, raw_count))
                self.assertEqual(expanded[-raw_count:], raw)
                self.assertTrue(all(left is right for left, right in zip(expanded[-raw_count:], raw)))
                self.assertEqual(items, original)

    def test_checkpoint_only_retains_every_item_and_stable_id_differences(self):
        adapter, _ = self.adapter()
        media = {"type": "message", "id": "msg_original", "role": "user", "content": [image("same-pixels")]}
        invocation, output = call("retained"), result("retained", [image("retained-output")])
        retained = [media, copy.deepcopy(media), invocation, output]
        checkpoint = {"type": "compaction", "encrypted_content": adapter.seal({
            "version": 2, "summary": "Synthetic self-contained checkpoint.", "retained": retained,
        })}
        self.assertEqual(adapter.expand([checkpoint], "deepseek-flash")[1:], retained)
        changed_id = {**media, "id": "msg_intentionally_new"}
        expanded = adapter.expand([checkpoint, changed_id], "deepseek-flash")
        self.assertEqual(expanded[1:], retained + [changed_id])
        self.assertIs(expanded[-1], changed_id)

    def test_explicit_match_count_is_consumed_across_multiple_checkpoints(self):
        adapter, _ = self.adapter()
        media = {"type": "message", "id": "msg_shared", "role": "user", "content": [image("shared")]}
        checkpoints = [{"type": "compaction", "encrypted_content": adapter.seal({
            "version": 2, "summary": "Synthetic checkpoint %d." % index, "retained": [media],
        })} for index in range(2)]
        items = checkpoints + [media]
        original = copy.deepcopy(items)
        expanded = adapter.expand(items, "deepseek-flash")
        self.assertEqual(expanded.count(media), 2)
        self.assertEqual(items, original)
        self.assertIs(expanded[-1], media)

    def test_repeated_client_preserved_attachments_do_not_accumulate_per_cycle(self):
        raw_media = [{"type": "message", "id": "msg_cycle_%d" % index, "role": "user",
                      "content": [image("user-media-%d" % index)]} for index in range(4)]
        tool_pairs = []
        for index in range(5):
            tool_pairs.extend([call("cycle_%d" % index), result("cycle_%d" % index, [image("tool-media-%d" % index)])])
        adapter, transport = self.adapter(count=3)
        initial = raw_media + tool_pairs
        initial_copy = copy.deepcopy(initial)
        checkpoint, _ = adapter.compact({"model": "deepseek-flash", "input": initial}, {})
        for cycle in range(3):
            retained = adapter.open(checkpoint["encrypted_content"])["retained"]
            self.assertEqual(retained, raw_media + tool_pairs)
            self.assertEqual(len(retained), 14)
            if cycle < 2:
                current = raw_media + [checkpoint, {"role": "user", "content": "Synthetic follow-up %d." % cycle}]
                original = copy.deepcopy(current)
                checkpoint, _ = adapter.compact({"model": "deepseek-flash", "input": current}, {})
                self.assertEqual(current, original)
        self.assertEqual(initial, initial_copy)
        self.assertEqual(len(transport.calls), 3)

    def test_nonmedia_history_unchanged_and_pending_calls_retained(self):
        invocation = call("plain")
        completed = result("plain", [{"type": "input_text", "text": "Small textual result."}])
        pending = call("pending", True)
        items = [{"role": "user", "content": "Synthetic text."}, invocation, completed, pending]
        original = copy.deepcopy(items)
        evidence, retained = Adapter.prepare_history(items)
        self.assertEqual(evidence, original)
        self.assertEqual(retained, [pending])
        self.assertEqual(items, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
