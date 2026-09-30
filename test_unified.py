"""Offline regression tests for the standalone unified adapter candidate.

HTTP tests bind an ephemeral loopback port and inject an in-memory upstream.
No production port, configuration, database, Keychain or model is contacted.
"""

import copy
import gzip
import hashlib
import http.client
import importlib.util
import io
import json
from pathlib import Path
import threading
import unittest
from unittest import mock

import adapter
import tool_image_bridge


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


HERE = Path(__file__).resolve().parent
# The baseline is a frozen copy of the adapter *before* the call/result pairing
# fix. Keeping it byte-stable is what makes the regression witness meaningful,
# so the expected digest is pinned here rather than derived at runtime.
BASELINE_PATH = HERE / 'fixtures' / 'adapter_baseline.py'
BASELINE_CONTENT = BASELINE_PATH.read_bytes()
BASELINE_SHA256 = '31ff2e888bf04bbf2ea244d690fbfd1f1ed1a3933e234380f6462995a072e3bb'
BASELINE_BYTES = 30513
if (hashlib.sha256(BASELINE_CONTENT).hexdigest() != BASELINE_SHA256
        or len(BASELINE_CONTENT) != BASELINE_BYTES):
    raise RuntimeError('fixed_adapter_baseline_provenance_mismatch')
spec = importlib.util.spec_from_file_location('unchanged_adapter_baseline', BASELINE_PATH)
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)

TEST_KEY = bytes(range(32))
TEST_CREDENTIAL = 'synthetic-offline-only-credential'
TEST_USAGE = {'input_tokens': 137, 'output_tokens': 29, 'total_tokens': 166,
              'input_tokens_details': {'cached_tokens': 51},
              'output_tokens_details': {'reasoning_tokens': 7}}
IMAGE = {'type': 'input_image', 'image_url': 'data:image/png;base64,TEST_ONLY',
         'detail': 'original', 'future_metadata': {'retain': [1, None, True]}}
MEDIA_MODEL = 'gemini-flash'
CFG = {'upstream': 'https://offline.invalid/v1',
       'compactor_model': 'gpt-6-sol', 'compactor_effort': 'medium',
       'media_models': [MEDIA_MODEL]}
EVENTS = (b'event: response.created\ndata: {"type":"response.created"}\n\n'
          b'event: response.completed\ndata: {"type":"response.completed",'
          b'"response":{"status":"completed","output":[]}}\n\n')


def reasoning():
    return {'type': 'reasoning', 'id': 'rs_synthetic',
            'encrypted_content': 'opaque-synthetic-reasoning',
            'summary': [{'type': 'summary_text', 'text': 'Public continuation summary.'}]}


def group():
    # The summary is deliberately between the call and its image result.
    return [
        {'type': 'function_call', 'name': 'read_fixture', 'call_id': 'call_a',
         'arguments': '{"path":"synthetic"}', 'id': 'fc_a', 'status': 'completed'},
        reasoning(),
        {'type': 'function_call_output', 'call_id': 'call_a', 'id': 'out_a',
         'output': [{'type': 'input_text', 'text': 'Before', 'extra': 1},
                    copy.deepcopy(IMAGE), {'type': 'input_text', 'text': 'After'}]},
    ]


def fixture(model=MEDIA_MODEL):
    return {
        'model': model, 'stream': True, 'store': False,
        'reasoning': {'effort': 'high'}, 'service_tier': 'priority',
        'instructions': 'Synthetic current task and authorization boundary.',
        'tools': [{'type': 'function', 'name': 'read_fixture',
                   'description': 'Keep this schema and every semantic instruction.',
                   'strict': True,
                   'parameters': {'type': 'object', 'properties': {
                       'path': {'type': 'string', 'description': 'Preserve verbatim.'}},
                       'required': ['path'], 'additionalProperties': False}}],
        'guardian_history': [
            {'type': 'function_call_output', 'call_id': 'guardian_a',
             'output': [copy.deepcopy(IMAGE), {'type': 'input_text', 'text': 'Preserve guardian.'}]}],
        'future_top_level': {'preserve': [None, True, 17]},
        'input': [{'type': 'message', 'role': 'user',
                   'content': [{'type': 'input_text', 'text': 'Inspect the evidence.'}]},
                  *group()],
    }


def image_parts(items):
    return [copy.deepcopy(part) for item in items if isinstance(item, dict)
            for field in ('content', 'output')
            for part in (item.get(field) if isinstance(item.get(field), list) else [])
            if isinstance(part, dict) and part.get('type') == 'input_image']


def noninput(body):
    return {key: value for key, value in body.items() if key != 'input'}


class MockRaw:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)

    def read1(self, size, decode_content=True):
        return self.buffer.read(size)


class MockResponse:
    status_code = 200
    headers = {'Content-Type': 'text/event-stream', 'X-Synthetic-Upstream': 'preserved'}

    def __init__(self, data=EVENTS, events=None):
        self.raw = MockRaw(data)
        self.events = events or []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self, chunk_size=1):
        for event in self.events:
            yield b'data: ' + json.dumps(event).encode()


class MockTransport:
    def __init__(self):
        self.forwarded = []
        self.exports = []

    def request(self, method, url, **kwargs):
        self.forwarded.append((method, url, copy.deepcopy(kwargs)))
        return MockResponse()

    def post(self, url, **kwargs):
        self.exports.append((url, copy.deepcopy(kwargs)))
        envelope = {'export_status': 'available', 'handoff': 'Synthetic exported native state.'}
        response = {'status': 'completed', 'model': 'gpt-6-sol', 'output': [
            {'type': 'message', 'role': 'assistant', 'status': 'completed',
             'content': [{'type': 'output_text', 'text': json.dumps(envelope)}]}],
            'usage': {'input_tokens': 31, 'output_tokens': 9, 'total_tokens': 40}}
        return MockResponse(events=[{'type': 'response.completed', 'response': response}])


class RecordingStrategy:
    def __init__(self):
        self.calls = []

    def compact(self, evidence, client, options):
        self.calls.append((copy.deepcopy(evidence), copy.deepcopy(options)))
        return {'summary': 'Synthetic compacted continuation state.', 'metadata': {
            'strategy_version': '2.0', 'calls': 0,
            'usage': copy.deepcopy(TEST_USAGE)}}


class OfflineCase(unittest.TestCase):
    def setUp(self):
        # Accidental fallback to a real transport, credential or Keychain is a failure.
        for target in ('adapter.requests.request', 'adapter.requests.post',
                       'adapter.read_api_key', 'adapter.keychain_key'):
            patcher = mock.patch(target, side_effect=AssertionError('external_access_forbidden'))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.transport = MockTransport()
        self.adapter = self.make_adapter()

    def make_adapter(self, implementation=adapter, transport=None):
        if implementation is baseline:
            # The frozen fixture keeps the original deployment's checkpoint
            # constants. Align them so this test compares expansion behavior
            # rather than two different namespace labels.
            implementation.PREFIX = adapter.PREFIX
            implementation.AAD = adapter.AAD
        return implementation.Adapter(copy.deepcopy(CFG), TEST_KEY,
                                      transport=transport or self.transport,
                                      credential=lambda: TEST_CREDENTIAL)

    def checkpoint(self, retained):
        return {'type': 'compaction', 'id': 'cmp_synthetic',
                'encrypted_content': self.adapter.seal({
                    'version': 2, 'summary': 'Synthetic local checkpoint.',
                    'retained': copy.deepcopy(retained)})}


class PrepareForwardTests(OfflineCase):
    def test_old_order_reproduces_failure_and_new_order_preserves_public_summary(self):
        original = fixture()
        snapshot = copy.deepcopy(original)
        old = self.make_adapter(baseline)
        bad_order = {**original, 'input': old.expand(original['input'], original['model'])}
        with self.assertRaises(tool_image_bridge.NormalizationError):
            tool_image_bridge.normalize_request(bad_order)
        result = self.adapter.prepare_forward(original)
        self.assertEqual(original, snapshot)
        self.assertEqual(noninput(result), noninput(original))
        self.assertEqual(result['input'][1], original['input'][1])
        self.assertEqual(result['input'][2]['content'][0]['text'], 'Public continuation summary.')
        self.assertEqual(result['input'][3]['call_id'], 'call_a')
        self.assertEqual(image_parts(result['input']), [IMAGE])
        self.assertFalse(any(item.get('type') == 'reasoning' for item in result['input']))
        self.assertEqual(self.transport.forwarded, [])
        self.assertEqual(self.transport.exports, [])

    def test_provider_native_thinking_survives_for_third_party_models(self):
        """Thinking-mode providers require their own reasoning_text back on the next turn.

        Regression: expand() used to delete every non-GPT reasoning item, so DeepSeek in
        thinking mode rejected the follow-up with "The `reasoning_text` in the thinking mode
        must be passed back to the API" and the thread died about a second into turn two.
        """
        native_thinking = {'type': 'reasoning', 'id': 'rs_native_thinking', 'summary': [],
                           'content': [{'type': 'reasoning_text', 'text': 'provider-native chain'}]}
        tail = {'role': 'user', 'content': [{'type': 'input_text', 'text': 'Continue.'}]}
        original = [native_thinking, tail]
        snapshot = copy.deepcopy(original)
        expanded = self.adapter.expand(original, 'deepseek-flash')
        self.assertEqual(original, snapshot, 'input must not be mutated')
        self.assertEqual(expanded, [native_thinking, tail])
        self.assertEqual(self.transport.forwarded, [])
        self.assertEqual(self.transport.exports, [])

    def test_opaque_reasoning_still_downgrades_to_public_summary(self):
        """The summary-only conversion stays in force for non-portable OpenAI reasoning."""
        expanded = self.adapter.expand([reasoning()], 'deepseek-flash')
        self.assertFalse(any(item.get('type') == 'reasoning' for item in expanded))
        self.assertIn('Public continuation summary.', json.dumps(expanded))

    def test_local_retained_group_normalizes_after_decryption_without_mutation(self):
        original = fixture()
        cp = self.checkpoint(group())
        original['input'] = [original['input'][0], cp]
        snapshot = copy.deepcopy(original)
        result = self.adapter.prepare_forward(original)
        self.assertEqual(original, snapshot)
        self.assertEqual(image_parts(result['input']), [IMAGE])
        self.assertIn('<context_checkpoint>', result['input'][1]['content'][0]['text'])
        self.assertEqual(self.adapter.open(cp['encrypted_content'])['retained'], group())
        self.assertEqual([item.get('call_id') for item in result['input']
                          if item.get('type') in ('function_call', 'function_call_output')],
                         ['call_a', 'call_a'])

    def test_retained_and_explicit_duplicate_images_keep_original_multiplicity(self):
        parts = group()
        parts[-1]['output'].insert(2, copy.deepcopy(IMAGE))
        explicit_image = {'type': 'message', 'role': 'user', 'content': [copy.deepcopy(IMAGE)]}
        cp = self.checkpoint([parts[0], parts[-1], explicit_image])
        original = fixture()
        original['input'] = [cp, *parts, copy.deepcopy(explicit_image), copy.deepcopy(explicit_image)]
        snapshot = copy.deepcopy(original)
        expanded = self.adapter.expand(original['input'], original['model'], preserve_reasoning=True)
        result = self.adapter.prepare_forward(original)
        self.assertEqual(original, snapshot)
        self.assertEqual(image_parts(expanded), [IMAGE] * 4)
        self.assertEqual(image_parts(result['input']), [IMAGE] * 4)
        self.assertEqual(sum(item.get('type') == 'function_call' for item in result['input']), 1)
        self.assertEqual(sum(item.get('type') == 'function_call_output' for item in result['input']), 1)
        self.assertEqual(sum(item == explicit_image for item in result['input']), 2)

    def test_prepared_input_is_idempotent(self):
        once = self.adapter.prepare_forward(fixture())
        twice = self.adapter.prepare_forward(once)
        self.assertEqual(twice, once)
        self.assertEqual(image_parts(twice['input']), [IMAGE])

    def test_non_targets_equal_existing_adapter_including_retained_reasoning(self):
        for model in ('gpt-6-astra', 'gpt-6-luna', 'deepseek-flash',
                      'gemini-flash-fast', 'Gemini-Flash'):
            with self.subTest(model=model):
                body = fixture(model)
                body['input'].insert(0, self.checkpoint([reasoning()]))
                original = copy.deepcopy(body)
                old = self.make_adapter(baseline)
                expected = {**body, 'input': old.expand(body['input'], model)}
                with mock.patch('adapter.normalize_request', side_effect=AssertionError('not_target')):
                    result = self.adapter.prepare_forward(body)
                self.assertEqual(result, expected)
                self.assertEqual(body, original)

    def test_native_gpt_ciphertext_and_reasoning_stay_opaque(self):
        body = fixture('gpt-6-astra')
        cp = {'type': 'compaction', 'encrypted_content': 'opaque-native-synthetic'}
        body['input'].insert(0, cp)
        result = self.adapter.prepare_forward(body)
        self.assertEqual(result, body)
        self.assertEqual(self.transport.exports, [])

    def test_cross_native_checkpoint_mock_export_and_encrypted_memory_cache(self):
        body = fixture()
        body['input'].insert(0, {'type': 'compaction', 'encrypted_content': 'opaque-native-synthetic'})
        first = self.adapter.prepare_forward(body)
        second = self.adapter.prepare_forward(body)
        self.assertEqual(first, second)
        self.assertIn('Synthetic exported native state.', first['input'][0]['content'][0]['text'])
        self.assertEqual(image_parts(first['input']), [IMAGE])
        self.assertEqual(len(self.transport.exports), 1)
        export = sent_body(self.transport.exports[0][1])
        self.assertEqual(export['model'], 'gpt-6-sol')
        self.assertEqual(export['reasoning'], {'effort': 'medium'})
        self.assertEqual(export['input'][0], body['input'][0])
        self.assertEqual(self.adapter.stats['native_exports'], 1)
        self.assertEqual(self.adapter.stats['native_cache_hits'], 1)
        self.assertIsNone(self.adapter.native_cache.path)
        self.assertTrue(all(value.startswith(adapter.PREFIX)
                            for value in self.adapter.native_cache.memory.values()))

    def test_malformed_after_native_checkpoint_fails_before_any_export(self):
        body = fixture()
        body['input'].insert(0, {'type': 'compaction', 'encrypted_content': 'opaque-native-synthetic'})
        body['input'][-1]['call_id'] = 'unmatched'
        original = copy.deepcopy(body)
        with self.assertRaises(tool_image_bridge.NormalizationError):
            self.adapter.prepare_forward(body)
        self.assertEqual(body, original)
        self.assertEqual(self.transport.exports, [])
        self.assertEqual(self.transport.forwarded, [])
        self.assertEqual(self.adapter.native_cache.memory, {})

    def test_local_checkpoint_retained_malformed_group_fails_without_mutation(self):
        broken = group()
        broken[-1]['output'].append({'type': 'input_file', 'file_id': 'synthetic-file'})
        body = fixture()
        body['input'] = [self.checkpoint(broken)]
        original = copy.deepcopy(body)
        with self.assertRaises(tool_image_bridge.NormalizationError) as raised:
            self.adapter.prepare_forward(body)
        self.assertEqual(raised.exception.code, 'unknown_tool_content')
        self.assertEqual(body, original)
        self.assertEqual(self.transport.exports, [])

    def test_public_modules_expose_their_documented_entry_points(self):
        import build_catalog
        import direct_handoff
        import model_presets
        import native_checkpoint
        for module, names in (
                (direct_handoff, ('Strategy',)),
                (native_checkpoint, ('NativeCheckpointCache',)),
                (model_presets, ('apply_policy_to_catalog', 'media_models',
                                 'family_for', 'describe')),
                (build_catalog, ('fetch_catalog', 'parse_catalog', 'main'))):
            for name in names:
                with self.subTest(module=module.__name__, name=name):
                    self.assertTrue(hasattr(module, name))

    def test_structurally_invalid_checkpoints_are_refused(self):
        """A payload that decrypts but is not task state must not be trusted."""
        cases = {
            "wrong version": {"version": 99, "summary": "synthetic", "retained": []},
            "summary not text": {"version": 2, "summary": ["synthetic"], "retained": []},
            "retained not list": {"version": 2, "summary": "synthetic", "retained": {}},
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                item = {"type": "compaction", "id": "cmp_synthetic",
                        "encrypted_content": self.adapter.seal(payload)}
                with self.assertRaises(ValueError):
                    self.adapter.expand([item], "deepseek-flash")

    def test_a_valid_checkpoint_is_still_accepted(self):
        good = self.checkpoint([{"type": "message", "role": "user",
                                 "content": [{"type": "input_text", "text": "kept"}]}])
        expanded = self.adapter.expand([good], "deepseek-flash")
        self.assertTrue(any(item.get("type") == "message" for item in expanded))


class HTTPTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.server = adapter.ThreadingHTTPServer(('127.0.0.1', 0), adapter.handler_for(self.adapter))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.assertNotIn(self.server.server_port, (15731, 15732))

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def post(self, body, encoding='identity', path='/responses'):
        raw = json.dumps(body, ensure_ascii=False).encode()
        if encoding == 'gzip':
            raw = gzip.compress(raw)
        elif encoding == 'zstd':
            raw = adapter.zstd.ZstdCompressor().compress(raw)
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            client.request('POST', path, raw, headers={
                'Authorization': 'Bearer ' + TEST_CREDENTIAL,
                'Content-Type': 'application/json', 'Content-Encoding': encoding,
                'X-Codex-Beta-Features': 'synthetic-feature',
                'Session_Id': 'synthetic-session', 'X-Client-Request-Id': 'synthetic-request'})
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def test_identity_gzip_zstd_complete_public_reasoning_group_and_headers(self):
        for encoding in ('identity', 'gzip', 'zstd'):
            with self.subTest(encoding=encoding):
                body = fixture()
                before = copy.deepcopy(body)
                status, headers, data = self.post(body, encoding)
                self.assertEqual(status, 200)
                self.assertEqual(data, EVENTS)
                self.assertEqual(headers['X-Synthetic-Upstream'], 'preserved')
                method, url, kwargs = self.transport.forwarded[-1]
                sent = sent_body(kwargs)
                self.assertEqual(method, 'POST')
                self.assertEqual(noninput(sent), noninput(body))
                self.assertEqual(image_parts(sent['input']), [IMAGE])
                self.assertEqual(sent['input'][2]['content'][0]['text'], 'Public continuation summary.')
                self.assertEqual(sent['input'][3]['call_id'], 'call_a')
                upstream_headers = {key.lower(): value for key, value in kwargs['headers'].items()}
                # The adapter decodes whatever Codex sent and re-encodes the body it forwards,
                # so the outbound header describes the body that actually goes out instead of
                # relaying the inbound value. All three inbound encodings must end up the same.
                self.assertEqual(upstream_headers.get('content-encoding'), 'zstd')
                self.assertNotIn('content-length', upstream_headers)
                self.assertEqual(upstream_headers['x-codex-beta-features'], 'synthetic-feature')
                self.assertEqual(upstream_headers['session_id'], 'synthetic-session')
                self.assertEqual(upstream_headers['x-client-request-id'], 'synthetic-request')
                self.assertEqual(body, before)
        self.assertEqual(len(self.transport.forwarded), 3)
        self.assertEqual(self.transport.exports, [])

    def test_http_local_checkpoint_retained_image_and_duplicate_count(self):
        parts = group()
        parts[-1]['output'].insert(2, copy.deepcopy(IMAGE))
        body = fixture()
        body['input'] = [self.checkpoint([parts[0], parts[-1]]), *parts]
        status, _, _ = self.post(body, 'zstd', '/v1/responses')
        self.assertEqual(status, 200)
        sent = sent_body(self.transport.forwarded[0][2])
        self.assertEqual(image_parts(sent['input']), [IMAGE, IMAGE])
        self.assertEqual(sum(item.get('type') == 'function_call' for item in sent['input']), 1)
        self.assertEqual(sum(item.get('type') == 'function_call_output' for item in sent['input']), 1)

    def test_http_native_checkpoint_export_is_simulated_and_then_forwarded(self):
        body = fixture()
        body['input'].insert(0, {'type': 'compaction', 'encrypted_content': 'opaque-native-synthetic'})
        status, _, _ = self.post(body, 'gzip')
        self.assertEqual(status, 200)
        self.assertEqual(len(self.transport.exports), 1)
        self.assertEqual(len(self.transport.forwarded), 1)
        sent = sent_body(self.transport.forwarded[0][2])
        self.assertFalse(any(item.get('type') == 'compaction' for item in sent['input']))
        self.assertEqual(image_parts(sent['input']), [IMAGE])

    def test_non_target_and_native_compact_requests_match_existing_forwarding(self):
        cases = [('gpt-6-astra', '/responses'), ('deepseek-flash', '/responses'),
                 ('gemini-flash-fast', '/v1/responses'), ('gpt-6-astra', '/responses/compact')]
        for model, path in cases:
            with self.subTest(model=model, path=path):
                body = fixture(model)
                if model.startswith('gpt-'):
                    body['input'].insert(0, {'type': 'compaction', 'encrypted_content': 'opaque-native-synthetic'})
                old = self.make_adapter(baseline)
                expected = {**body, 'input': old.expand(body['input'], model)}
                with mock.patch('adapter.normalize_request', side_effect=AssertionError('must_bypass')):
                    status, _, data = self.post(body, path=path)
                self.assertEqual(status, 200)
                self.assertEqual(data, EVENTS)
                self.assertEqual(sent_body(self.transport.forwarded[-1][2]), expected)
        self.assertEqual(self.transport.exports, [])

    def test_compact_routes_and_trigger_keep_existing_evidence_retained_and_strategy(self):
        cases = [('/responses/compact', False), ('/v1/responses/compact', False), ('/responses', True)]
        for path, trigger in cases:
            with self.subTest(path=path, trigger=trigger):
                body = fixture()
                body['stream'] = False
                if trigger:
                    body['input'].append({'type': 'compaction_trigger'})
                original = copy.deepcopy(body)
                self.adapter.strategy = RecordingStrategy()
                old = self.make_adapter(baseline)
                old.strategy = RecordingStrategy()
                expected_item, expected_usage = old.compact(copy.deepcopy(body), {})
                with mock.patch('adapter.normalize_request', side_effect=AssertionError('compact_must_bypass')):
                    status, _, raw = self.post(body, 'zstd', path)
                self.assertEqual(status, 200)
                result = json.loads(raw)
                self.assertEqual(expected_usage, TEST_USAGE)
                self.assertEqual(result['usage'], TEST_USAGE)
                self.assertEqual(self.adapter.stats['last_compaction_plan']['usage'], TEST_USAGE)
                self.assertEqual(old.stats['last_compaction_plan']['usage'], TEST_USAGE)
                self.assertEqual(self.adapter.strategy.calls, old.strategy.calls)
                got_checkpoint = self.adapter.open(result['output'][0]['encrypted_content'])
                expected_checkpoint = old.open(expected_item['encrypted_content'])
                self.assertEqual(got_checkpoint, expected_checkpoint)
                self.assertEqual(image_parts(got_checkpoint['retained']), [IMAGE])
                self.assertEqual(body, original)
        self.assertEqual(self.transport.forwarded, [])
        self.assertEqual(self.transport.exports, [])

    def test_normalization_errors_are_fixed_422_and_zero_upstream_for_all_encodings(self):
        for encoding in ('identity', 'gzip', 'zstd'):
            for kind in ('ambiguous_tool_image_group', 'invalid_tool_image',
                         'unknown_image_detail', 'unknown_tool_content'):
                with self.subTest(encoding=encoding, kind=kind):
                    body = fixture()
                    body['input'].insert(0, {'type': 'compaction', 'encrypted_content': 'opaque-native-synthetic'})
                    if kind == 'ambiguous_tool_image_group':
                        body['input'][-1]['call_id'] = 'missing'
                    elif kind == 'invalid_tool_image':
                        body['input'][-1]['output'][1]['image_url'] = 'file:///synthetic/private.png'
                    elif kind == 'unknown_image_detail':
                        body['input'][-1]['output'][1]['detail'] = 'unrecognized'
                    else:
                        body['input'][-1]['output'].append({'type': 'input_file', 'file_id': 'synthetic'})
                    original = copy.deepcopy(body)
                    status, _, raw = self.post(body, encoding)
                    self.assertEqual(status, 422)
                    error = json.loads(raw)['error']
                    self.assertEqual(error['code'], kind)
                    self.assertIn('original history must be retained', error['message'])
                    self.assertNotIn(TEST_CREDENTIAL, raw.decode())
                    self.assertNotIn('private.png', raw.decode())
                    self.assertEqual(body, original)
                    self.assertEqual(self.transport.forwarded, [])
                    self.assertEqual(self.transport.exports, [])
                    self.assertEqual(self.adapter.stats['last_error']['code'], kind)


if __name__ == '__main__':
    unittest.main()
