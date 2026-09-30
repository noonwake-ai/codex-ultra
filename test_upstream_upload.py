"""Outbound upload behaviour: re-compression, and the retry for a truncated upload.

Codex already sends its own request body zstd-compressed. This service decompresses it to
adapt the body, so re-compressing the forwarded copy is what keeps the upload small on the
slow links where a truncated body shows up as the gateway's generic 400. These tests pin
that behaviour: compress on the way out, retry once on a fresh connection when the gateway
rejects the body before any model saw it, and never retry an error that may have run one.
"""
import io, json, secrets, threading, time, unittest
import requests, zstandard as zstd
from http.server import ThreadingHTTPServer
from adapter import (Adapter, DEFAULT_UPSTREAM_ENCODING, GATEWAY_BODY_READ_ERROR,
                     ZSTD_PROBE_COOLDOWN, body_read_failure, handler_for)


def is_compressed(call):
    """Whether this upstream call carried a compressed body."""
    return call["headers"].get("Content-Encoding") == "zstd"


def body_of(call):
    """Decode the body of a recorded upstream call, whatever the encoding."""
    data = call.get("data")
    if isinstance(data, (bytes, bytearray)) and is_compressed(call):
        data = zstd.ZstdDecompressor().decompress(data)
    return json.loads(bytes(data).decode("utf-8"))

TOKEN = 'synthetic-upload-test-credential'
BODY_READ_400 = json.dumps({'error': {'message': 'Failed to read request body',
                                      'type': 'invalid_request_error'}}).encode()
OTHER_400 = json.dumps({'error': {'message': 'model not found',
                                  'type': 'invalid_request_error'}}).encode()
SSE_200 = b'data: {"type":"response.completed","response":{"status":"completed","output":[]}}\n\n'


class Raw:
    def __init__(self, data):
        self._buf = io.BytesIO(data)

    def read(self, size=-1, decode_content=True):
        return self._buf.read(size)

    def read1(self, size, decode_content=True):
        return self._buf.read(size)


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self.headers = {'Content-Type': 'application/json' if status != 200 else 'text/event-stream',
                        'Content-Length': str(len(payload))}
        self.raw = Raw(payload)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ScriptedTransport:
    """Serves pre-scripted upstream responses and records every request."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError('upstream called more times than the script allows')
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        # The compaction path posts directly rather than going through request().
        return self.request('POST', url, **kwargs)


class StreamResponse:
    """Streaming upstream response for the compaction path."""
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.headers = {'Content-Type': 'text/event-stream' if status == 200 else 'application/json'}

    @property
    def content(self):
        return self._payload

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self, chunk_size=1):
        for line in self._payload.split(b'\n'):
            if line:
                yield line


def make(body=None, encoding='identity', responses=None):
    cfg = {'upstream': 'https://gateway.invalid/v1', 'compactor_model': 'gpt-6-sol',
           'compactor_effort': 'medium', 'port': 0, 'upstream_encoding': encoding}
    transport = ScriptedTransport(responses or [FakeResponse(200, SSE_200)])
    adapter = Adapter(cfg, secrets.token_bytes(32), transport=transport,
                      credential=lambda: TOKEN)
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(adapter))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01},
                     daemon=True).start()
    return adapter, transport, server


def post(server, payload, path='/responses'):
    return requests.post('http://127.0.0.1:%d%s' % (server.server_port, path), json=payload,
                         headers={'Authorization': 'Bearer ' + TOKEN}, timeout=20)


PAYLOAD = {'model': 'deepseek-flash', 'stream': False,
           'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'hello'}]}]}


class UploadEncodingTests(unittest.TestCase):
    def tearDown(self):
        for s in getattr(self, '_servers', []):
            s.shutdown(); s.server_close()

    def track(self, server):
        self._servers = getattr(self, '_servers', []) + [server]

    def test_default_encoding_sends_plain_json(self):
        adapter, transport, server = make(); self.track(server)
        self.assertEqual(post(server, PAYLOAD).status_code, 200)
        self.assertFalse(is_compressed(transport.calls[0]))
        self.assertEqual(body_of(transport.calls[0]), PAYLOAD)

    def test_zstd_encoding_compresses_the_upload(self):
        adapter, transport, server = make(encoding='zstd'); self.track(server)
        self.assertEqual(post(server, PAYLOAD).status_code, 200)
        call = transport.calls[0]
        self.assertTrue(is_compressed(call))
        sent = json.loads(zstd.ZstdDecompressor().decompress(call['data']))
        self.assertEqual(sent, PAYLOAD)
        self.assertEqual(adapter.stats['upstream_zstd'], 1)

    def test_compressed_upload_is_much_smaller_than_the_json(self):
        adapter, transport, server = make(encoding='zstd'); self.track(server)
        big = {'model': 'deepseek-flash', 'stream': False, 'instructions': 'detail ' * 4000,
               'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'line\n' * 4000}]}]}
        self.assertEqual(post(server, big).status_code, 200)
        plain = len(json.dumps(big, ensure_ascii=False, separators=(',', ':')).encode())
        self.assertLess(len(transport.calls[0]['data']), plain)


class TruncatedUploadRetryTests(unittest.TestCase):
    def tearDown(self):
        for s in getattr(self, '_servers', []):
            s.shutdown(); s.server_close()

    def track(self, server):
        self._servers = getattr(self, '_servers', []) + [server]

    def test_a_truncated_upload_is_retried_once_and_the_client_sees_success(self):
        adapter, transport, server = make(responses=[FakeResponse(400, BODY_READ_400),
                                                     FakeResponse(200, SSE_200)])
        self.track(server)
        response = post(server, PAYLOAD)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'response.completed', response.content)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(adapter.stats['upstream_retries'], 1)

    def test_the_retry_is_bounded_to_one_extra_attempt(self):
        adapter, transport, server = make(responses=[FakeResponse(400, BODY_READ_400),
                                                     FakeResponse(400, BODY_READ_400)])
        self.track(server)
        self.assertEqual(post(server, PAYLOAD).status_code, 400)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(adapter.stats['upstream_retries'], 1)

    def test_a_different_400_is_forwarded_without_retrying(self):
        adapter, transport, server = make(responses=[FakeResponse(400, OTHER_400)])
        self.track(server)
        response = post(server, PAYLOAD)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'model not found', response.content)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(adapter.stats['upstream_retries'], 0)

    def test_a_successful_response_is_never_retried(self):
        adapter, transport, server = make(); self.track(server)
        self.assertEqual(post(server, PAYLOAD).status_code, 200)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(adapter.stats['upstream_retries'], 0)

    def test_a_gateway_that_rejects_zstd_falls_back_to_plain_json(self):
        """The generic 400 is identical for 'cannot decode zstd' and 'body truncated',
        so one rejection must NOT be enough to give up on compression."""
        adapter, transport, server = make(encoding='zstd',
                                          responses=[FakeResponse(400, BODY_READ_400),
                                                     FakeResponse(200, SSE_200)])
        self.track(server)
        self.assertEqual(post(server, PAYLOAD).status_code, 200)
        self.assertTrue(is_compressed(transport.calls[0]))
        self.assertFalse(is_compressed(transport.calls[1]))
        self.assertEqual(adapter.upstream_encoding, 'zstd',
                         'a single transient rejection must not disable compression')
        self.assertEqual(adapter.zstd_rejections, 1)

    def test_a_run_of_rejections_eventually_disables_compression(self):
        """Three rejections in a row is a pattern, so stop paying a failed attempt each time."""
        scripted = []
        for _ in range(3):
            scripted += [FakeResponse(400, BODY_READ_400), FakeResponse(200, SSE_200)]
        scripted.append(FakeResponse(200, SSE_200))
        adapter, transport, server = make(encoding='zstd', responses=scripted)
        self.track(server)
        for _ in range(3):
            self.assertEqual(post(server, PAYLOAD).status_code, 200)
        self.assertEqual(adapter.upstream_encoding, 'identity')
        self.assertEqual(adapter.stats['upstream_zstd_downgraded'], 1)
        self.assertEqual(post(server, PAYLOAD).status_code, 200)
        self.assertFalse(is_compressed(transport.calls[-1]))

    def test_a_success_clears_the_rejection_streak(self):
        adapter, transport, server = make(encoding='zstd',
                                          responses=[FakeResponse(400, BODY_READ_400),
                                                     FakeResponse(200, SSE_200),
                                                     FakeResponse(200, SSE_200)])
        self.track(server)
        self.assertEqual(post(server, PAYLOAD).status_code, 200)
        self.assertEqual(adapter.zstd_rejections, 1)
        self.assertEqual(post(server, PAYLOAD).status_code, 200)
        self.assertEqual(adapter.zstd_rejections, 0)
        self.assertEqual(adapter.upstream_encoding, 'zstd')

    def test_a_connection_exception_is_not_retried(self):
        """Only the proven 400 signature justifies a retry; a socket error does not."""
        class Boom:
            def request(self, *a, **k):
                raise ConnectionError('synthetic connect failure')
        cfg = {'upstream': 'https://gateway.invalid/v1', 'compactor_model': 'gpt-6-sol',
               'compactor_effort': 'medium', 'port': 0, 'upstream_encoding': 'zstd'}
        adapter = Adapter(cfg, secrets.token_bytes(32), transport=Boom(),
                          credential=lambda: TOKEN)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(adapter))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01},
                         daemon=True).start()
        self.track(server)
        self.assertGreaterEqual(post(server, PAYLOAD).status_code, 500)
        self.assertEqual(adapter.stats['upstream_retries'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)


COMPACTION_SSE = (b'data: {"type":"response.completed","response":{"status":"completed",'
                  b'"model":"gpt-6-sol","output":[],"usage":{"input_tokens":9,'
                  b'"output_tokens":3,"total_tokens":12}}}\n\n')


class CompactionUploadTests(unittest.TestCase):
    """The compaction path sends the largest body, so it gets the same protection."""
    def make(self, encoding='identity', responses=None):
        cfg = {'upstream': 'https://gateway.invalid/v1', 'compactor_model': 'gpt-6-sol',
               'compactor_effort': 'medium', 'port': 0, 'upstream_encoding': encoding}
        transport = ScriptedTransport(responses or [StreamResponse(200, COMPACTION_SSE)])
        return Adapter(cfg, secrets.token_bytes(32), transport=transport,
                       credential=lambda: TOKEN), transport

    def payload(self):
        return {'model': 'gpt-6-sol', 'reasoning': {'effort': 'medium'}, 'stream': True,
                'max_output_tokens': 4096, 'store': False,
                'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'history ' * 200}]}]}

    def test_zstd_compaction_upload_is_compressed(self):
        adapter, transport = self.make(encoding='zstd')
        payload = self.payload()
        adapter.call_compactor(payload, {}, 'compaction')
        call = transport.calls[0]
        self.assertEqual(call['headers']['Content-Encoding'], 'zstd')
        self.assertEqual(json.loads(zstd.ZstdDecompressor().decompress(call['data'])), payload)
        self.assertEqual(adapter.stats['upstream_zstd'], 1)

    def test_compaction_retries_once_when_the_gateway_cannot_read_the_body(self):
        adapter, transport = self.make(responses=[StreamResponse(400, BODY_READ_400),
                                                  StreamResponse(200, COMPACTION_SSE)])
        adapter.call_compactor(self.payload(), {}, 'compaction')
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(adapter.stats['upstream_retries'], 1)

    def test_compaction_with_zstd_falls_back_to_plain_json(self):
        adapter, transport = self.make(encoding='zstd',
                                       responses=[StreamResponse(400, BODY_READ_400),
                                                  StreamResponse(200, COMPACTION_SSE)])
        adapter.call_compactor(self.payload(), {}, 'compaction')
        self.assertTrue(is_compressed(transport.calls[0]))
        self.assertFalse(is_compressed(transport.calls[1]))
        self.assertEqual(adapter.upstream_encoding, 'zstd',
                         'one rejection must not disable compression')

    def test_a_different_compaction_error_still_fails_and_is_not_retried(self):
        adapter, transport = self.make(responses=[StreamResponse(500, b'upstream exploded')])
        with self.assertRaises(RuntimeError) as raised:
            adapter.call_compactor(self.payload(), {}, 'compaction')
        self.assertEqual(str(raised.exception), 'compactor_http_500')
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(adapter.stats['upstream_retries'], 0)


class BodyReadSignatureTests(unittest.TestCase):
    """The retry trigger must match the gateway's error exactly, not a substring."""
    def test_the_exact_gateway_error_matches(self):
        self.assertTrue(body_read_failure(BODY_READ_400))

    def test_a_different_error_does_not_match(self):
        for payload in (OTHER_400,
                        json.dumps({'error': {'message': 'Request body is empty'}}).encode(),
                        json.dumps({'error': 'Failed to read request body'}).encode(),
                        json.dumps({'message': 'Failed to read request body'}).encode()):
            with self.subTest(payload=payload[:40]):
                self.assertFalse(body_read_failure(payload))

    def test_a_quoted_phrase_inside_another_error_does_not_match(self):
        """A model error that merely quotes the phrase must not trigger a retry."""
        quoted = json.dumps({'error': {'message': 'upstream said: Failed to read request body',
                                       'type': 'upstream_error'}}).encode()
        self.assertFalse(body_read_failure(quoted))

    def test_non_json_and_empty_payloads_do_not_match(self):
        for payload in (b'', b'<html>400</html>', b'{}', b'null', 'not-bytes', None):
            with self.subTest(payload=repr(payload)[:24]):
                self.assertFalse(body_read_failure(payload))


class InstallerDefaultTests(unittest.TestCase):
    """A default that never reaches config.json is not a default."""
    def test_the_installer_writes_the_compressed_encoding(self):
        source = (__import__('pathlib').Path(__file__).resolve().parent / 'install.py').read_text()
        self.assertIn('"upstream_encoding": "zstd"', source)
        self.assertIn('"upstream_encoding"', source.split("config.write_text")[1])

    def test_the_component_has_no_other_upload_path(self):
        """Every body this service sends upstream must go through the two checked paths."""
        source = (__import__('pathlib').Path(__file__).resolve().parent / 'adapter.py').read_text()
        self.assertEqual(source.count("transport.post("), 1)
        self.assertEqual(source.count("transport.request("), 1)


class EncodingSelectionTests(unittest.TestCase):
    """Which encoding a config selects, including configs written before the key existed.

    Installing over an existing install only replaces the source files, so a config that
    never had this key has to pick up compression by itself; an explicit value always wins.
    """
    def adapter(self, **override):
        cfg = {'upstream': 'https://gateway.invalid/v1', 'compactor_model': 'gpt-6-sol',
               'compactor_effort': 'medium', 'port': 0}
        cfg.update(override)
        return Adapter(cfg, secrets.token_bytes(32),
                       transport=ScriptedTransport([FakeResponse(200, SSE_200)]),
                       credential=lambda: TOKEN)

    def test_a_config_without_the_key_uses_compression(self):
        self.assertEqual(self.adapter().upstream_encoding, DEFAULT_UPSTREAM_ENCODING)

    def test_an_explicit_identity_is_respected(self):
        self.assertEqual(self.adapter(upstream_encoding='identity').upstream_encoding, 'identity')

    def test_the_configured_value_is_case_insensitive(self):
        self.assertEqual(self.adapter(upstream_encoding=' ZSTD ').upstream_encoding, 'zstd')

    def test_an_unknown_value_falls_back_to_a_plain_upload(self):
        """A typo must not brick the service: fall back to plain JSON, visibly."""
        self.assertEqual(self.adapter(upstream_encoding='gzip').upstream_encoding, 'identity')

    def test_an_unconfigured_adapter_compresses_the_forwarded_body(self):
        adapter = self.adapter()
        transport = adapter.transport
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(adapter))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01},
                         daemon=True).start()
        try:
            self.assertEqual(post(server, PAYLOAD).status_code, 200)
            self.assertTrue(is_compressed(transport.calls[0]))
            self.assertEqual(body_of(transport.calls[0]), PAYLOAD)
        finally:
            server.shutdown(); server.server_close()

    def test_health_reports_the_encoding_in_use(self):
        adapter = self.adapter(upstream_encoding='identity')
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(adapter))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01},
                         daemon=True).start()
        try:
            reported = requests.get('http://127.0.0.1:%d/health' % server.server_port,
                                    timeout=20).json()
            self.assertEqual(reported['upstream_encoding'], 'identity')
            for counter in ('upstream_zstd', 'upstream_zstd_bytes', 'upstream_retries',
                            'upstream_retries_ok', 'upstream_zstd_downgraded'):
                self.assertIn(counter, reported)
        finally:
            server.shutdown(); server.server_close()


class CounterTests(unittest.TestCase):
    """Every counter exposed on /health must actually move.

    Two of these were declared but never incremented, so the endpoint reported 0 forever
    and the fix looked inert in production.
    """
    def tearDown(self):
        for s in getattr(self, '_servers', []):
            s.shutdown(); s.server_close()

    def track(self, server):
        self._servers = getattr(self, '_servers', []) + [server]

    def test_compressed_bytes_are_counted(self):
        adapter, transport, server = make(encoding='zstd'); self.track(server)
        self.assertEqual(post(server, PAYLOAD).status_code, 200)
        self.assertEqual(adapter.stats['upstream_zstd'], 1)
        self.assertEqual(adapter.stats['upstream_zstd_bytes'], len(transport.calls[0]['data']))
        self.assertGreater(adapter.stats['upstream_zstd_bytes'], 0)

    def test_a_successful_retry_is_counted_as_recovered(self):
        adapter, transport, server = make(responses=[FakeResponse(400, BODY_READ_400),
                                                     FakeResponse(200, SSE_200)])
        self.track(server)
        self.assertEqual(post(server, PAYLOAD).status_code, 200)
        self.assertEqual(adapter.stats['upstream_retries'], 1)
        self.assertEqual(adapter.stats['upstream_retries_ok'], 1,
                         'a retry that produced the client response must be visible')

    def test_an_unrecovered_retry_is_not_counted_as_recovered(self):
        adapter, transport, server = make(responses=[FakeResponse(400, BODY_READ_400),
                                                     FakeResponse(400, BODY_READ_400)])
        self.track(server)
        self.assertEqual(post(server, PAYLOAD).status_code, 400)
        self.assertEqual(adapter.stats['upstream_retries'], 1)
        self.assertEqual(adapter.stats['upstream_retries_ok'], 0)

    def test_every_exposed_counter_has_an_increment_site(self):
        import pathlib, re
        source = (pathlib.Path(__file__).resolve().parent / 'adapter.py').read_text()
        declared = set(re.findall(r"'(upstream_[a-z_]+)':0", source))
        self.assertTrue(declared, 'counter declaration block not found')
        for name in sorted(declared):
            with self.subTest(counter=name):
                calls = re.findall(r"count\('" + name + r"'", source)
                adds = re.findall(r"stats\['" + name + r"'\]\s*\+=", source)
                self.assertTrue(calls or adds,
                                name + ' is exposed on /health but never incremented')

    def test_the_compaction_path_counts_bytes_too(self):
        adapter, transport = CompactionUploadTests().make(encoding='zstd')
        adapter.call_compactor(CompactionUploadTests().payload(), {}, 'compaction')
        self.assertEqual(adapter.stats['upstream_zstd'], 1)
        self.assertEqual(adapter.stats['upstream_zstd_bytes'], len(transport.calls[0]['data']))


class Utf8BodyTests(unittest.TestCase):
    """Non-ASCII must travel as UTF-8, not as \\uXXXX escapes.

    requests' json= uses ensure_ascii=True, which inflated the uncompressed fallback
    upload by 11-18% on a script containing Chinese text.
    """
    def tearDown(self):
        for s in getattr(self, '_servers', []):
            s.shutdown(); s.server_close()

    def track(self, server):
        self._servers = getattr(self, '_servers', []) + [server]

    CHINESE = {'model': 'deepseek-flash', 'stream': False,
               'input': [{'role': 'user', 'content': [{'type': 'input_text',
                          'text': '尚哥，这是一段中文测试内容。' * 40}]}]}

    def test_identity_body_keeps_non_ascii_as_utf8(self):
        adapter, transport, server = make(); self.track(server)
        big = dict(self.CHINESE)
        self.assertEqual(post(server, big).status_code, 200)
        raw = transport.calls[0]['data']
        self.assertIn('中文测试'.encode('utf-8'), raw)
        self.assertNotIn(b'\\u', raw, 'non-ASCII must not be escaped into \\uXXXX')
        escaped = len(json.dumps(big, ensure_ascii=True).encode())
        self.assertLess(len(raw), escaped)

    def test_compressed_body_round_trips_non_ascii(self):
        adapter, transport, server = make(encoding='zstd'); self.track(server)
        self.assertEqual(post(server, self.CHINESE).status_code, 200)
        self.assertEqual(body_of(transport.calls[0]), self.CHINESE)

    def test_compaction_body_keeps_non_ascii_as_utf8(self):
        adapter, transport = CompactionUploadTests().make()
        payload = dict(CompactionUploadTests().payload())
        payload['input'] = self.CHINESE['input']
        adapter.call_compactor(payload, {}, 'compaction')
        raw = transport.calls[0]['data']
        self.assertIn('中文测试'.encode('utf-8'), raw)
        self.assertNotIn(b'\\u', raw)


UPSTREAM_TYPED_400 = json.dumps({'error': {
    'message': 'Failed to read request body', 'type': 'upstream_error'}}).encode()


class AuditBoundaryTests(unittest.TestCase):
    """Boundaries raised by an independent audit of this fix.

    Each case is a real way the earlier revision could behave wrongly; they exist so the
    reasoning cannot regress silently.
    """
    def tearDown(self):
        for s in getattr(self, '_servers', []):
            s.shutdown(); s.server_close()

    def track(self, server):
        self._servers = getattr(self, '_servers', []) + [server]

    # --- 1. a relayed upstream error must not trigger a retry -------------------
    def test_a_relayed_upstream_error_with_the_same_wording_is_not_retried(self):
        """The gateway types its own body-read failure invalid_request_error.

        A relayed upstream error is typed upstream_error and may have reached a model,
        so retrying it could execute the request twice.
        """
        self.assertFalse(body_read_failure(UPSTREAM_TYPED_400))
        adapter, transport, server = make(responses=[FakeResponse(400, UPSTREAM_TYPED_400)])
        self.track(server)
        self.assertEqual(post(server, PAYLOAD).status_code, 400)
        self.assertEqual(len(transport.calls), 1, 'a relayed upstream error must not be retried')
        self.assertEqual(adapter.stats['upstream_retries'], 0)

    def test_the_gateways_own_error_type_is_required(self):
        for payload in (BODY_READ_400,):
            with self.subTest(payload=payload[:40]):
                self.assertTrue(body_read_failure(payload))

    # --- 2. the compaction path must only retry on 400 -------------------------
    def test_compaction_does_not_retry_a_429_that_quotes_the_phrase(self):
        adapter, transport = CompactionUploadTests().make(
            responses=[StreamResponse(429, BODY_READ_400)])
        with self.assertRaises(RuntimeError) as raised:
            adapter.call_compactor(CompactionUploadTests().payload(), {}, 'compaction')
        self.assertEqual(str(raised.exception), 'compactor_http_429')
        self.assertEqual(len(transport.calls), 1, '429 must not be retried')
        self.assertEqual(adapter.stats['upstream_retries'], 0)

    def test_compaction_does_not_retry_a_401_or_502(self):
        for status in (401, 502, 503):
            with self.subTest(status=status):
                adapter, transport = CompactionUploadTests().make(
                    responses=[StreamResponse(status, BODY_READ_400)])
                with self.assertRaises(RuntimeError):
                    adapter.call_compactor(CompactionUploadTests().payload(), {}, 'compaction')
                self.assertEqual(len(transport.calls), 1)

    def test_forward_does_not_retry_a_non_400_either(self):
        for status in (401, 429, 502, 503):
            with self.subTest(status=status):
                adapter, transport, server = make(responses=[FakeResponse(status, BODY_READ_400)])
                self.track(server)
                self.assertEqual(post(server, PAYLOAD).status_code, status)
                self.assertEqual(len(transport.calls), 1)

    # --- 3. a transport failure must not pause compression --------------------
    def test_transport_failure_does_not_count_against_zstd(self):
        """If the uncompressed fallback fails identically, the cause is transport.

        Pausing compression then would be backwards: the uncompressed body is several
        times larger.
        """
        adapter, transport, server = make(encoding='zstd',
                                          responses=[FakeResponse(400, BODY_READ_400),
                                                     FakeResponse(400, BODY_READ_400)])
        self.track(server)
        self.assertEqual(post(server, PAYLOAD).status_code, 400)
        self.assertEqual(adapter.zstd_rejections, 0,
                         'a transport failure says nothing about compression')
        self.assertEqual(adapter.upstream_encoding, 'zstd')
        self.assertEqual(adapter.stats['upstream_zstd_downgraded'], 0)

    def test_repeated_transport_failures_never_pause_compression(self):
        scripted = [FakeResponse(400, BODY_READ_400), FakeResponse(400, BODY_READ_400)] * 5
        adapter, transport, server = make(encoding='zstd', responses=scripted)
        self.track(server)
        for _ in range(5):
            post(server, PAYLOAD)
        self.assertEqual(adapter.upstream_encoding, 'zstd')
        self.assertEqual(adapter.zstd_rejections, 0)

    # --- 4. a downgrade must not last forever --------------------------------
    def test_a_downgrade_is_a_pause_and_is_retried_after_the_cooldown(self):
        scripted = []
        for _ in range(3):
            scripted += [FakeResponse(400, BODY_READ_400), FakeResponse(200, SSE_200)]
        adapter, transport, server = make(encoding='zstd', responses=scripted)
        self.track(server)
        for _ in range(3):
            self.assertEqual(post(server, PAYLOAD).status_code, 200)
        self.assertEqual(adapter.upstream_encoding, 'identity', 'three rejections pause it')
        self.assertGreater(adapter.zstd_probe_at, 0, 'a probe must be scheduled')

        # The cooldown is wall-clock based, so move the deadline instead of sleeping.
        with adapter.lock:
            adapter.zstd_probe_at = time.time() - 1
        self.assertTrue(adapter.due_for_zstd_probe(),
                        'once the cooldown has passed the adapter must try zstd again')
        self.assertEqual(adapter.upstream_encoding, 'zstd')

    def test_the_cooldown_backs_off_so_a_permanent_rejection_is_not_paid_every_request(self):
        adapter, *_ = make(encoding='zstd')
        first = adapter.zstd_cooldown
        for _ in range(3):
            adapter.note_zstd_outcome(False)
        self.assertEqual(adapter.upstream_encoding, 'identity')
        self.assertGreater(adapter.zstd_cooldown, first, 'the cooldown must grow')

    def test_a_success_resets_the_cooldown(self):
        adapter, *_ = make(encoding='zstd')
        for _ in range(3):
            adapter.note_zstd_outcome(False)
        adapter.note_zstd_outcome(True)
        self.assertEqual(adapter.zstd_cooldown, ZSTD_PROBE_COOLDOWN)
        self.assertEqual(adapter.zstd_probe_at, 0)

    def test_an_explicitly_configured_identity_never_probes(self):
        """Only a downgrade schedules a probe; an operator choice is respected."""
        adapter, *_ = make(encoding='identity')
        self.assertEqual(adapter.zstd_probe_at, 0)
        self.assertFalse(adapter.due_for_zstd_probe())
        self.assertEqual(adapter.upstream_encoding, 'identity')


class UploadDeadlineTests(unittest.TestCase):
    """A request body must not be bounded by the connect timeout.

    urllib3 arms the socket with the CONNECT value for the whole request and only installs the
    READ value once the response starts being read, so a body that needs longer than the connect
    deadline to leave the socket is cut off mid-upload and the gateway answers
    `400 Failed to read request body`. Measured 2026-09-30 on a real link: a 26 MB body at about
    1.5 MB/s died at exactly 20.0 s with the stock adapter and completed unchanged once the
    connection re-armed its own upload budget.
    """

    def test_the_body_gets_its_own_deadline_even_on_a_reused_connection(self):
        import http.server
        import urllib3
        import adapter as adapter_module
        seen = []

        class Recording(adapter_module.GatewayHTTPConnection):
            def send(self, data):
                self._extend_upload_deadline()
                seen.append(self.sock.gettimeout() if getattr(self, 'sock', None) else None)
                return super().send(data)

        class Pool(urllib3.HTTPConnectionPool):
            ConnectionCls = Recording

        class SessionAdapter(requests.adapters.HTTPAdapter):
            def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
                self.poolmanager = urllib3.PoolManager(num_pools=connections, maxsize=maxsize,
                                                       block=block, **kwargs)
                self.poolmanager.pool_classes_by_scheme = {'http': Pool}

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                self.send_response(400)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            session = requests.Session()
            session.mount('http://', SessionAdapter())
            for _ in range(2):                 # the second call reuses the pooled connection
                session.post('http://127.0.0.1:%d/x' % server.server_port,
                             data=b'x' * 200000, timeout=(20, 480))
            armed = [value for value in seen if value is not None]
            self.assertTrue(armed, 'no socket timeout was observed while sending')
            for value in armed:
                self.assertEqual(value, adapter_module.GATEWAY_UPLOAD_BUDGET,
                                 'the body was still bounded by the connect timeout')
        finally:
            server.shutdown(); server.server_close()

    def test_the_default_transport_is_the_upload_budget_session(self):
        import adapter as adapter_module
        session = adapter_module.gateway_transport()
        self.assertIsInstance(session, requests.Session)
        self.assertIsInstance(session.get_adapter('https://gateway.invalid/v1'),
                              adapter_module.GatewayTransport)

    def test_the_budget_leaves_room_for_a_large_upload(self):
        import adapter as adapter_module
        self.assertGreater(adapter_module.GATEWAY_UPLOAD_BUDGET, 60)
