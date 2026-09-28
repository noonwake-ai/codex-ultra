"""Installer guard tests. Nothing is installed, no network and no Keychain access."""

import json
import pathlib
import tempfile
import unittest

import adapter
import install

CONFIG = (
    'model_provider = "MyGateway"\n'
    'model = "deepseek-v4-flash"\n'
    '\n'
    '[model_providers.MyGateway]\n'
    'base_url = "https://gateway.example/v1"\n'
    'wire_api = "responses"\n'
)


class UpstreamTests(unittest.TestCase):
    def test_https_is_required(self):
        with self.assertRaises(install.InstallError) as raised:
            install.validate_upstream("http://gateway.example/v1")
        self.assertEqual(str(raised.exception), "upstream_must_be_https")

    def test_loopback_upstreams_are_refused(self):
        for url in ("https://127.0.0.1:8080/v1", "https://localhost/v1"):
            with self.subTest(url=url), self.assertRaises(install.InstallError) as raised:
                install.validate_upstream(url)
            self.assertEqual(str(raised.exception), "upstream_must_not_be_loopback")

    def test_trailing_slash_is_normalised(self):
        self.assertEqual(install.validate_upstream("https://gateway.example/v1/"),
                         "https://gateway.example/v1")

    def test_nonsense_is_refused(self):
        for url in ("", None, "gateway.example/v1", "ftp://gateway.example/v1"):
            with self.subTest(url=url), self.assertRaises(install.InstallError):
                install.validate_upstream(url)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = pathlib.Path(self.temp.name) / "config.toml"
        self.config.write_text(CONFIG)

    def tearDown(self):
        self.temp.cleanup()

    def test_current_provider_is_detected(self):
        self.assertEqual(install.resolve_provider(str(self.config), None), "MyGateway")

    def test_explicit_provider_wins(self):
        self.assertEqual(install.resolve_provider(str(self.config), "Other"), "Other")


class MediaRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = pathlib.Path(self.temp.name) / "models.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_media_models_come_from_the_built_catalog(self):
        self.catalog.write_text(json.dumps({"models": [
            {"slug": "gemini-3.8-flash"}, {"slug": "deepseek-v4-flash"}]}))
        self.assertEqual(install.read_media_models(str(self.catalog)),
                         ["gemini-3.8-flash"])

    def test_no_catalog_means_no_media_routing(self):
        self.assertEqual(install.read_media_models(None), [])

    def test_an_unreadable_catalog_fails_loudly(self):
        with self.assertRaises(install.InstallError) as raised:
            install.read_media_models(str(self.catalog))
        self.assertEqual(str(raised.exception), "catalog_unreadable")


class CompactorModelTests(unittest.TestCase):
    """A wrong compactor model must fail at install time, not mid-task."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = pathlib.Path(self.temp.name) / "models.json"
        self.catalog.write_text(json.dumps({"models": [
            {"slug": "deepseek-v4-flash"}, {"slug": "gemini-3.8-flash"}]}))

    def tearDown(self):
        self.temp.cleanup()

    def test_a_model_the_gateway_offered_is_accepted(self):
        install.validate_compactor_model("gemini-3.8-flash", str(self.catalog))

    def test_a_model_the_gateway_never_offered_is_refused(self):
        with self.assertRaises(install.InstallError) as raised:
            install.validate_compactor_model("definitely-not-offered", str(self.catalog))
        self.assertEqual(str(raised.exception),
                         "compactor_model_not_in_catalog:definitely-not-offered")

    def test_a_catalog_without_models_refuses_everything(self):
        self.catalog.write_text(json.dumps({"models": []}))
        with self.assertRaises(install.InstallError):
            install.validate_compactor_model("deepseek-v4-flash", str(self.catalog))

    def test_no_catalog_means_the_check_is_skipped_not_guessed(self):
        install.validate_compactor_model("anything-at-all", None)


class RuntimeTests(unittest.TestCase):
    """A broken toolchain has to come back as a code, not a traceback."""

    def test_a_broken_interpreter_is_reported_as_a_fixed_code(self):
        with tempfile.TemporaryDirectory() as temp:
            broken = str(pathlib.Path(temp) / "no-such-python")
            with self.assertRaises(install.InstallError) as raised:
                install.build_runtime(pathlib.Path(temp), broken)
            self.assertEqual(str(raised.exception),
                             "runtime_build_failed_check_python_and_network")


class SourceTests(unittest.TestCase):
    def test_every_file_the_installer_copies_exists_in_the_repository(self):
        for name in install.SOURCES:
            with self.subTest(name=name):
                self.assertTrue((install.HERE / name).is_file())

    def test_installer_refuses_to_overwrite_an_existing_install(self):
        source = (install.HERE / "install.py").read_text()
        self.assertIn("already_installed_use_configure_status", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class LiveMediaRoutingTests(unittest.TestCase):
    """Media routing follows the catalog, so a new model needs no reinstall."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.catalog = self.root / "models.json"

    def tearDown(self):
        self.temp.cleanup()

    def write_catalog(self, slugs):
        self.catalog.write_text(json.dumps({"models": [{"slug": s} for s in slugs]}))

    def test_catalog_wins_over_the_frozen_install_time_list(self):
        self.write_catalog(["gemini-3.8-flash", "kimi-k2"])
        resolved = adapter.resolve_media_models({
            "media_models": ["stale-model-from-install-time"],
            "catalog": str(self.catalog)})
        self.assertEqual(resolved, ("gemini-3.8-flash",))

    def test_a_newly_exposed_model_is_picked_up_without_reinstalling(self):
        self.write_catalog(["kimi-k2"])
        before = adapter.resolve_media_models({"catalog": str(self.catalog)})
        self.assertEqual(before, ())
        self.write_catalog(["kimi-k2", "gemini-3.9-flash"])
        after = adapter.resolve_media_models({"catalog": str(self.catalog)})
        self.assertEqual(after, ("gemini-3.9-flash",))

    def test_without_a_catalog_the_explicit_list_is_used(self):
        self.assertEqual(adapter.resolve_media_models({"media_models": ["gemini-3.8-flash"]}),
                         ("gemini-3.8-flash",))

    def test_an_unreadable_catalog_falls_back_instead_of_refusing_to_start(self):
        resolved = adapter.resolve_media_models({
            "media_models": ["gemini-3.8-flash"],
            "catalog": str(self.root / "missing.json")})
        self.assertEqual(resolved, ("gemini-3.8-flash",))
