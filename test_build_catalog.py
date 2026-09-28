"""Catalog builder tests; a local HTTP server stands in for the gateway."""

import json
import pathlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import build_catalog

GATEWAY_CATALOG = {"models": [
    {"slug": "deepseek-v4-flash", "display_name": "DeepSeek V4 Flash",
     "context_window": 1000000, "max_context_window": 1000000,
     "input_modalities": ["text"], "supported_reasoning_levels": []},
    {"slug": "gemini-3.8-flash", "display_name": "Gemini 3.8 Flash",
     "context_window": 1000000, "max_context_window": 1000000,
     "input_modalities": ["text"], "supported_reasoning_levels": []},
    {"slug": "kimi-k2", "display_name": "Kimi K2",
     "context_window": 262144, "max_context_window": 262144,
     "input_modalities": ["text"]},
]}
SYNTHETIC_KEY = "synthetic-gateway-key-for-tests-only"


class Handler(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        Handler.seen.append(self.path)
        if self.headers.get("Authorization") != "Bearer " + SYNTHETIC_KEY:
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps(GATEWAY_CATALOG).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = pathlib.Path(self.temp.name) / "models.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_offline_build_applies_policy(self):
        source = pathlib.Path(self.temp.name) / "gateway.json"
        source.write_text(json.dumps(GATEWAY_CATALOG))
        code = build_catalog.main(["--from-file", str(source), "--out", str(self.out)])
        self.assertEqual(code, 0)
        merged = json.loads(self.out.read_text())
        self.assertEqual([m["slug"] for m in merged["models"]],
                         ["deepseek-v4-flash", "gemini-3.8-flash", "kimi-k2"])
        self.assertEqual(merged["models"][0]["context_window"], 1000000)

    def test_live_fetch_uses_the_configured_environment_variable(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            catalog = build_catalog.fetch_catalog(
                "http://127.0.0.1:%d" % server.server_port, SYNTHETIC_KEY, "0.158.0")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(len(catalog["models"]), 3)
        self.assertIn("client_version=0.158.0", Handler.seen[-1])

    def test_wrong_key_is_reported_without_echoing_it(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(build_catalog.BuildError) as raised:
                build_catalog.fetch_catalog(
                    "http://127.0.0.1:%d" % server.server_port, "wrong-key", "0.158.0")
        finally:
            server.shutdown()
            server.server_close()
        self.assertNotIn("wrong-key", str(raised.exception))
        self.assertEqual(str(raised.exception), "gateway_http_401")

    def test_only_and_exclude_filter_the_selection(self):
        catalog = build_catalog.select(GATEWAY_CATALOG, "kimi-k2,gemini-3.8-flash", None)
        self.assertEqual([m["slug"] for m in catalog["models"]],
                         ["gemini-3.8-flash", "kimi-k2"])
        catalog = build_catalog.select(GATEWAY_CATALOG, None, "kimi-k2")
        self.assertNotIn("kimi-k2", [m["slug"] for m in catalog["models"]])

    def test_requesting_a_model_the_key_cannot_see_fails_loudly(self):
        with self.assertRaises(build_catalog.BuildError) as raised:
            build_catalog.select(GATEWAY_CATALOG, "gpt-9-imaginary", None)
        self.assertIn("requested_model_not_offered", str(raised.exception))

    def test_output_carries_no_credential(self):
        source = pathlib.Path(self.temp.name) / "gateway.json"
        source.write_text(json.dumps(GATEWAY_CATALOG))
        build_catalog.main(["--from-file", str(source), "--out", str(self.out)])
        self.assertNotIn(SYNTHETIC_KEY, self.out.read_text())
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)

    def test_credential_like_text_in_a_catalog_is_refused(self):
        poisoned = json.loads(json.dumps(GATEWAY_CATALOG))
        poisoned["models"][0]["description"] = "sk-" + "a" * 30
        with self.assertRaises(build_catalog.BuildError):
            build_catalog.write_catalog(self.out, poisoned)

    def test_non_codex_payloads_are_rejected(self):
        with self.assertRaises(build_catalog.BuildError):
            build_catalog.parse_catalog(b"<html>gateway login</html>")
        with self.assertRaises(build_catalog.BuildError):
            build_catalog.parse_catalog(json.dumps({"data": []}).encode())

    def test_list_families_runs_without_a_gateway(self):
        self.assertEqual(build_catalog.main(["--list-families"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
