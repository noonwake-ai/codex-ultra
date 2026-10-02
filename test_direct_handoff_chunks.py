"""Offline synthetic tests: bounded requests, exact source coverage and fail-closed output."""
import copy
import json
import threading
import time
import unittest

from direct_handoff import Strategy, _encoding, _json, _output_text, _request_tokens


def completed(text):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant",
            "status": "completed", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}, "seconds": 0.01}


def source_data(payload):
    text = payload["input"][0]["content"][0]["text"]
    return json.loads(text.split("\n", 1)[1].rsplit("\nEND ", 1)[0])


class Client:
    def __init__(self, reply=None):
        self.reply = reply
        self.calls = []
        self.lock = threading.Lock()
        self.active = 0
        self.maximum_active = 0
        self.active_by_stage = {}
        self.maximum_active_by_stage = {}

    @staticmethod
    def stage_of(label):
        if "-map" in label:
            return "map"
        if "-reduce" in label:
            return "reduce"
        return "direct"

    def call(self, payload, label):
        stage = self.stage_of(label)
        with self.lock:
            self.calls.append((copy.deepcopy(payload), label))
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            self.active_by_stage[stage] = self.active_by_stage.get(stage, 0) + 1
            self.maximum_active_by_stage[stage] = max(self.maximum_active_by_stage.get(stage, 0),
                                                      self.active_by_stage[stage])
        try:
            if self.reply:
                return self.reply(payload, label)
            if "-map" in label:
                # Finish adjacent chunks out of order to exercise ordered assembly.
                time.sleep(0.008 if int(label.rsplit("-map", 1)[1]) % 2 == 0 else 0.002)
                indices = [unit["source_item_index"] for unit in source_data(payload)]
                return completed("Synthetic source indices " + _json(indices))
            return completed("Synthetic verified state; preserve user constraints; next step is verification.")
        finally:
            with self.lock:
                self.active -= 1
                self.active_by_stage[self.stage_of(label)] -= 1


class ChunkTests(unittest.TestCase):
    def setUp(self):
        self.options = {"model": "gpt-6-sol", "effort": "medium", "budget_tokens": 8000,
                        "max_output_tokens": 16000, "input_token_budget": 2400,
                        "scenario": "offline", "cycle": 1}
        self.encoding = _encoding()

    def assert_bounded(self, client, budget=2400):
        self.assertTrue(client.calls)
        for payload, _ in client.calls:
            self.assertLessEqual(_request_tokens(payload, self.encoding), budget)
            self.assertEqual(payload["model"], "gpt-6-sol")
            self.assertEqual(payload["reasoning"], {"effort": "medium"})
            self.assertEqual(payload["max_output_tokens"], 16000)
            self.assertFalse(payload["store"])
            self.assertNotIn("tools", payload)

    def test_single_call_keeps_original_input_and_budgets(self):
        history = [{"role": "user", "content": "Project TEST-42. Only local previews authorized."}]
        original = copy.deepcopy(history)
        client = Client()
        result = Strategy().compact(history, client, self.options)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(source_data(client.calls[0][0]), original)
        self.assertEqual(history, original)
        self.assertEqual(result["metadata"]["stages"], {"direct": 1, "map": 0, "reduce": 0})
        self.assertEqual(result["metadata"]["calls"], 1)
        self.assertEqual(result["metadata"]["usage"]["total_tokens"], 20)
        self.assert_bounded(client)

    def test_whole_items_and_tool_pairs_keep_exact_chronology(self):
        history = [{"type": "message", "role": "user", "content": "Early instruction " + "evidence " * 400}]
        for index in range(14):
            history.extend([
                {"type": "function_call", "id": "transport-%d" % index, "call_id": "call-%d" % index,
                 "name": "check_local", "arguments": _json({"index": index, "value": "v " * 100})},
                {"type": "function_call_output", "call_id": "call-%d" % index,
                 "output": "Actual synthetic result " + "observed " * 100},
            ])
        history.append({"role": "user", "content": "Latest correction: deploy is revoked; preview only."})
        original = copy.deepcopy(history)
        client = Client()
        result = Strategy().compact(history, client, self.options)
        maps = sorted(((int(label.rsplit("-map", 1)[1]), payload) for payload, label in client.calls if "-map" in label))
        units = [unit for _, payload in maps for unit in source_data(payload)]
        self.assertEqual([unit["source_item_index"] for unit in units], list(range(len(history))))
        self.assertEqual([unit["item"] for unit in units], original)
        self.assertEqual(history, original)
        capped = Client()
        Strategy().compact(history, capped, {**self.options, "map_workers": 2})
        self.assertLessEqual(capped.maximum_active, 2)
        self.assertEqual(capped.maximum_active, 2)
        self.assertGreaterEqual(client.maximum_active, 3)
        final = next(payload for payload, label in client.calls if label.endswith("-final"))
        ranges = [node["source_units"] for node in source_data(final)]
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], len(units))
        self.assertTrue(all(left[1] == right[0] for left, right in zip(ranges, ranges[1:])))
        self.assertEqual(result["metadata"]["map_chunks"], len(maps))
        self.assert_bounded(client)

    def test_giant_item_fragments_reconstruct_exact_unicode_serialization(self):
        giant = {"type": "custom_tool_call_output", "id": "synthetic-output", "call_id": "pending-42",
                 "name": "synthetic_reader", "output": ('多字节 "引用"\\换行\n🚀 <|endoftext|> ' * 600)}
        history = [{"role": "user", "content": "Only preview is allowed."}, giant,
                   {"role": "user", "content": "Correction: use TEST-43."}]
        original = copy.deepcopy(history)
        client = Client()
        result = Strategy().compact(history, client, self.options)
        maps = sorted(((int(label.rsplit("-map", 1)[1]), payload) for payload, label in client.calls if "-map" in label))
        units = [unit for _, payload in maps for unit in source_data(payload)]
        fragments = [unit for unit in units if unit["source_item_index"] == 1]
        self.assertGreater(len(fragments), 1)
        self.assertEqual("".join(unit["serialized_fragment"] for unit in fragments), _json(giant))
        self.assertEqual([unit["fragment_index"] for unit in fragments], list(range(len(fragments))))
        self.assertTrue(all(unit["fragment_count"] == len(fragments) for unit in fragments))
        cursor = 0
        for unit in fragments:
            self.assertEqual(unit["start_char"], cursor)
            cursor = unit["end_char"]
            self.assertEqual(cursor - unit["start_char"], len(unit["serialized_fragment"]))
            self.assertEqual(unit["source_item_metadata"]["call_id"], "pending-42")
        self.assertEqual(cursor, len(_json(giant)))
        self.assertEqual(units[0]["item"], original[0])
        self.assertEqual(units[-1]["item"], original[-1])
        self.assertEqual(history, original)
        self.assertEqual(result["metadata"]["split_source_items"], 1)
        self.assertEqual(result["metadata"]["source_fragments"], len(fragments))
        self.assert_bounded(client)

    def test_multilevel_reduction_bounds_every_request(self):
        history = [{"role": "user", "content": "item%d " % index + "verified " * 500}
                   for index in range(18)]

        def reply(payload, label):
            if "-map" in label:
                return completed("Source %s: " % source_data(payload)[0]["source_item_index"] + "detail " * 240)
            nodes = source_data(payload)
            self.assertTrue(all(left["source_units"][1] == right["source_units"][0]
                                for left, right in zip(nodes, nodes[1:])))
            return completed("Merged ranges %d to %d. " % (nodes[0]["source_units"][0], nodes[-1]["source_units"][1])
                             + "evidence " * 50)

        client = Client(reply)
        result = Strategy().compact(history, client, self.options)
        self.assertGreaterEqual(result["metadata"]["reduce_levels"], 2)
        self.assertEqual(result["metadata"]["calls"], len(client.calls))
        self.assertEqual(sum(result["metadata"]["stages"].values()), len(client.calls))
        final = next(payload for payload, label in client.calls if label.endswith("-final"))
        final_nodes = source_data(final)
        self.assertEqual(final_nodes[0]["source_units"][0], 0)
        self.assertEqual(final_nodes[-1]["source_units"][1], len(history))
        self.assert_bounded(client)

    def test_failed_map_never_runs_final_reduction(self):
        history = [{"role": "user", "content": "source " * 500} for _ in range(8)]

        def reply(payload, label):
            result = completed("Partial evidence must never be accepted.")
            result["status"] = "incomplete"
            result["incomplete_details"] = {"reason": "max_output_tokens"}
            return result

        client = Client(reply)
        with self.assertRaisesRegex(RuntimeError, "compactor_incomplete"):
            Strategy().compact(history, client, self.options)
        self.assertFalse(any("-reduce" in label for _, label in client.calls))
        self.assert_bounded(client)

    def test_map_workers_default_runs_more_than_two_chunks_at_once(self):
        history = [{"role": "user", "content": "chunk%d " % index + "evidence " * 400}
                   for index in range(24)]
        def reply(payload, label):
            # Hold the call open long enough for overlap to be observable at all.
            time.sleep(0.01)
            return completed("state " + "x " * 40)

        client = Client(reply)
        result = Strategy().compact(history, client, self.options)
        self.assertGreaterEqual(result["metadata"]["map_chunks"], 4)
        self.assertGreaterEqual(client.maximum_active_by_stage.get("map", 0), 3)

    def test_map_workers_can_be_pinned_for_rate_limited_gateways(self):
        history = [{"role": "user", "content": "chunk%d " % index + "evidence " * 400}
                   for index in range(24)]
        client = Client(lambda payload, label: completed("state " + "x " * 40))
        Strategy().compact(history, client, {**self.options, "map_workers": 1})
        self.assertEqual(client.maximum_active, 1)

    def test_intermediate_reductions_run_in_parallel(self):
        history = [{"role": "user", "content": "item%d " % index + "verified " * 500}
                   for index in range(18)]

        def reply(payload, label):
            time.sleep(0.01)
            if "-map" in label:
                return completed("Source %s: " % source_data(payload)[0]["source_item_index"]
                                 + "detail " * 240)
            return completed("Merged ranges. " + "evidence " * 50)

        client = Client(reply)
        result = Strategy().compact(history, client, self.options)
        self.assertGreaterEqual(result["metadata"]["reduce_levels"], 2)
        self.assertGreaterEqual(client.maximum_active_by_stage.get("reduce", 0), 2)

    def test_parallel_maps_finish_faster_than_a_single_worker(self):
        history = [{"role": "user", "content": "chunk%d " % index + "evidence " * 400}
                   for index in range(24)]

        def slow(payload, label):
            time.sleep(0.05)
            return completed("state " + "x " * 40)

        serial = Client(slow)
        started = time.monotonic()
        Strategy().compact(history, serial, {**self.options, "map_workers": 1})
        serial_seconds = time.monotonic() - started

        parallel = Client(slow)
        started = time.monotonic()
        Strategy().compact(history, parallel, {**self.options, "map_workers": 4})
        parallel_seconds = time.monotonic() - started

        self.assertEqual(len(serial.calls), len(parallel.calls))
        self.assertLess(parallel_seconds, serial_seconds * 0.75)

    def test_strict_response_validation(self):
        invalid = [
            {"status": "completed", "output_text": "Shortcut must not bypass item validation."},
            {"status": "completed", "output": [{"type": "function_call", "name": "write"}]},
            {"status": "completed", "output": [{"type": "reasoning", "summary": [{"text": "opaque"}]}]},
        ]
        for replacement in ({"role": "user"}, {"status": "in_progress"},
                            {"content": [{"type": "refusal", "refusal": "no"}]}):
            response = completed("Bad output")
            response["output"][0].update(replacement)
            invalid.append(response)
        for field, value in (("status", "incomplete"), ("error", {"code": "failed"}),
                             ("incomplete_details", {"reason": "max_output_tokens"})):
            response = completed("Bad output")
            response[field] = value
            invalid.append(response)
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                _output_text(response)
        self.assertEqual(_output_text(completed("Valid visible handoff")), "Valid visible handoff")

    def test_prompt_cost_is_counted_before_any_call(self):
        client = Client()
        with self.assertRaisesRegex(ValueError, "compactor_input_budget_too_small"):
            Strategy().compact([{"role": "user", "content": "x"}], client,
                               {**self.options, "input_token_budget": 100})
        self.assertEqual(client.calls, [])

    def test_nonconverging_reduction_fails_instead_of_looping(self):
        history = [{"role": "user", "content": "history " * 500} for _ in range(5)]
        client = Client(lambda payload, label: completed("state " * 750))
        with self.assertRaisesRegex(RuntimeError, "compactor_reduction_did_not_converge"):
            Strategy().compact(history, client, self.options)
        self.assertFalse(any("-reduce" in label for _, label in client.calls))
        self.assert_bounded(client)


if __name__ == "__main__":
    unittest.main(verbosity=2)
