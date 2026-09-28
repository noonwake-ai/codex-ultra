"""Endpoint-only routing tests; no real config, database or credential is used."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import configure

ORIGINAL = (
    'model_provider = "MyGateway"\n'
    'model = "deepseek-v4-flash"\n'
    'model_reasoning_effort = "high"\n'
    '\n'
    '[model_providers.MyGateway]\n'
    'name = "My Gateway"\n'
    'base_url = "https://gateway.example/v1"\n'
    'wire_api = "responses"\n'
    '\n'
    '[mcp_servers.fixture]\n'
    'command = "keep-me"\n'
)


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        self.record = self.root / "rollback.json"
        self.db = self.root / "cc.db"
        self.config.write_text(ORIGINAL)

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = configure.main(list(argv))
        return code, out.getvalue()

    def base(self, action, **extra):
        argv = [action, "--config", str(self.config),
                "--rollback-record", str(self.record)]
        for key, value in extra.items():
            argv += ["--" + key.replace("_", "-"), str(value)]
        return argv

    def test_apply_then_rollback_touches_only_the_endpoint(self):
        code, _ = self.run_cli(*self.base("apply", upstream="https://gateway.example/v1"))
        self.assertEqual(code, 0)
        routed = self.config.read_text()
        self.assertIn('base_url = "http://127.0.0.1:15731"', routed)
        self.assertIn('model = "deepseek-v4-flash"', routed)
        self.assertIn('command = "keep-me"', routed)
        self.assertNotIn("gateway.example", routed)

        # A later unrelated edit must survive rollback.
        self.config.write_text(routed.replace('model = "deepseek-v4-flash"',
                                              'model = "goose-2"'))
        code, _ = self.run_cli(*self.base("rollback"))
        self.assertEqual(code, 0)
        restored = self.config.read_text()
        self.assertIn('base_url = "https://gateway.example/v1"', restored)
        self.assertIn('model = "goose-2"', restored)
        self.assertFalse(self.record.exists())

    def test_record_holds_no_credentials(self):
        self.config.write_text(ORIGINAL + '\nexperimental_bearer_token = "synthetic-token"\n')
        self.run_cli(*self.base("apply", upstream="https://gateway.example/v1"))
        text = self.record.read_text()
        self.assertNotIn("synthetic-token", text)
        self.assertEqual(json.loads(text)["provider"], "MyGateway")

    def test_apply_refuses_when_a_record_already_exists(self):
        self.run_cli(*self.base("apply", upstream="https://gateway.example/v1"))
        code, _ = self.run_cli(*self.base("apply", upstream="https://gateway.example/v1"))
        self.assertEqual(code, 2)
        self.assertTrue(self.record.exists())

    def test_apply_refuses_an_endpoint_that_is_not_configured(self):
        code, _ = self.run_cli(*self.base("apply", upstream="https://other.example/v1"))
        self.assertEqual(code, 2)
        self.assertIn("gateway.example", self.config.read_text())

    def test_rollback_without_a_record_is_refused(self):
        code, _ = self.run_cli(*self.base("rollback"))
        self.assertEqual(code, 2)
        self.assertEqual(self.config.read_text(), ORIGINAL)

    def test_status_reports_route_without_writing(self):
        code, out = self.run_cli(*self.base("status"))
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["routed_locally"])
        self.assertEqual(self.config.read_text(), ORIGINAL)

    def test_status_calls_a_custom_port_install_routed_locally(self):
        """A working install on another port must not look broken."""
        self.config.write_text(ORIGINAL.replace(
            "https://gateway.example/v1", "http://127.0.0.1:15841"))
        code, out = self.run_cli(*self.base("status", local="http://127.0.0.1:15731"))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["routed_locally"])
        self.assertEqual(payload["endpoint"], "http://127.0.0.1:15841")

    def test_status_does_not_claim_a_remote_endpoint_is_local(self):
        for endpoint in ("https://gateway.example/v1", "https://remote.example/v1"):
            with self.subTest(endpoint=endpoint):
                self.config.write_text(ORIGINAL.replace(
                    "https://gateway.example/v1", endpoint))
                code, out = self.run_cli(*self.base("status"))
                self.assertFalse(json.loads(out)["routed_locally"])

    def test_provider_can_be_selected_explicitly(self):
        self.config.write_text(ORIGINAL.replace('model_provider = "MyGateway"', "")
                               + '\n[model_providers.Second]\nbase_url = "https://second.example/v1"\n')
        code, _ = self.run_cli(*self.base("apply", provider="Second",
                                          upstream="https://second.example/v1"))
        self.assertEqual(code, 0)
        self.assertIn('base_url = "http://127.0.0.1:15731"', self.config.read_text())

    def test_cc_switch_record_is_synced_without_touching_credentials(self):
        connection = sqlite3.connect(self.db)
        connection.executescript(
            "CREATE TABLE providers(id TEXT, app_type TEXT, settings_config TEXT);")
        connection.execute("INSERT INTO providers VALUES(?,?,?)", (
            "MyGateway", "codex",
            json.dumps({"auth": {"OPENAI_API_KEY": "synthetic"},
                        "config": ORIGINAL})))
        connection.commit()
        connection.close()

        code, _ = self.run_cli(*self.base("apply", upstream="https://gateway.example/v1",
                                          cc_switch_db=self.db))
        self.assertEqual(code, 0)
        connection = sqlite3.connect(self.db)
        stored = json.loads(connection.execute(
            "SELECT settings_config FROM providers").fetchone()[0])
        connection.close()
        self.assertEqual(stored["auth"]["OPENAI_API_KEY"], "synthetic")
        self.assertIn("127.0.0.1:15731", stored["config"])
        self.assertNotIn("synthetic", self.record.read_text())

    def cc_switch_db_with(self, settings):
        connection = sqlite3.connect(self.db)
        connection.executescript(
            "CREATE TABLE providers(id TEXT, app_type TEXT, settings_config TEXT);")
        for provider, payload in settings:
            connection.execute("INSERT INTO providers VALUES(?,?,?)", (
                provider, "codex", json.dumps(payload)))
        connection.commit()
        connection.close()

    def test_a_refused_cc_switch_sync_leaves_no_partial_apply(self):
        """The command reports failure, so nothing at all may have been done."""
        self.cc_switch_db_with([("MyGateway", {
            "auth": {},
            "config": ORIGINAL.replace(
                'wire_api = "responses"',
                'wire_api = "responses"\nOPENAI_API_KEY = "synthetic"')})])

        code, _ = self.run_cli(*self.base("apply", upstream="https://gateway.example/v1",
                                          cc_switch_db=self.db))
        self.assertEqual(code, 2)
        self.assertEqual(self.config.read_text(), ORIGINAL)
        self.assertFalse(self.record.exists(),
                         "a failed apply must not leave a rollback record behind")

    def test_a_missing_cc_switch_provider_leaves_no_partial_apply(self):
        self.cc_switch_db_with([])

        code, _ = self.run_cli(*self.base("apply", upstream="https://gateway.example/v1",
                                          cc_switch_db=self.db))
        self.assertEqual(code, 2)
        self.assertEqual(self.config.read_text(), ORIGINAL)
        self.assertFalse(self.record.exists())

    def test_an_edit_made_during_the_command_is_never_overwritten(self):
        """Another tool writing config.toml mid-command must win, not be clobbered."""
        self.cc_switch_db_with([("MyGateway", {"auth": {}, "config": ORIGINAL})])
        external = ORIGINAL.replace('model = "deepseek-v4-flash"', 'model = "edited-elsewhere"')

        def edit_then_continue(*_args, **_kwargs):
            self.config.write_text(external)
            return {"config": ORIGINAL}

        with patch.object(configure, "cc_switch_plan", side_effect=edit_then_continue):
            code, _ = self.run_cli(*self.base("apply", upstream="https://gateway.example/v1",
                                              cc_switch_db=self.db))
        self.assertEqual(code, 2)
        self.assertEqual(self.config.read_text(), external)


if __name__ == "__main__":
    unittest.main(verbosity=2)
