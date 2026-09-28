"""Fail the build if a credential or an operator-specific identifier leaks.

A repository-wide text scan with no allowlist file: adding a file that contains
an internal hostname, a personal path or a key-shaped string breaks CI at once.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
SKIP_DIRS = {".git", "__pycache__", "runtime", ".venv", "venv", "node_modules"}
SKIP_SUFFIXES = {".pyc", ".log", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}

CREDENTIAL_PATTERNS = (
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("bearer literal", re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{24,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("credential in url", re.compile(r"https?://[^\s/@]+:[^\s/@]+@")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")),
)

# Operator-specific identifiers that would reveal a private deployment.
INTERNAL_PATTERNS = (
    ("operator hostname", re.compile(r"coding\.noonwake\.ai", re.I)),
    ("legacy checkpoint prefix", re.compile(r"nwcp\d")),
    ("private provider id", re.compile(r"openai-composite-\d{8}")),
    ("private network ip", re.compile(
        r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
    ("personal home path", re.compile(r"/(?:Users|home)/[A-Za-z][A-Za-z0-9._-]*/")),
)

BRAND = re.compile(r"noonwake", re.I)
# Documentation may credit the public organisation that publishes the project.
DOCS = {"README.md", "README.en.md", "LICENSE",
        "docs/INSTALL.md", "docs/MODELS.md",
        "docs/ai-install.md", "docs/ai-install.en.md",
        "CONTRIBUTING.md", "SECURITY.md"}
# This file necessarily contains the patterns it searches for.
SELF = "test_secret_scan.py"

PUBLIC_URL_ALLOW = re.compile(
    r"https?://(?:"
    r"github\.com|img\.shields\.io|shields\.io|api\.star-history\.com|"
    r"www\.python\.org|docs\.python\.org|pypi\.org|opensource\.org|"
    r"raw\.githubusercontent\.com|www\.w3\.org|127\.0\.0\.1|localhost)[/:]")
ALLOWED_HOST_SUFFIXES = (".invalid", ".example", ".test", "example.com")


def repository_files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        yield path


def text_of(path):
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


class SecretScanTests(unittest.TestCase):
    def test_no_credential_shaped_strings(self):
        findings = []
        for path in repository_files():
            text = text_of(path)
            if text is None:
                continue
            for label, pattern in CREDENTIAL_PATTERNS:
                for match in pattern.finditer(text):
                    findings.append("%s: %s near offset %d" % (
                        path.relative_to(ROOT), label, match.start()))
        self.assertEqual(findings, [], "credential-shaped text found:\n" + "\n".join(findings))

    def test_no_operator_specific_identifiers(self):
        findings = []
        for path in repository_files():
            relative = str(path.relative_to(ROOT))
            if relative == SELF:
                continue
            text = text_of(path)
            if text is None:
                continue
            for label, pattern in INTERNAL_PATTERNS:
                for match in pattern.finditer(text):
                    findings.append("%s: %s near offset %d" % (relative, label, match.start()))
            if relative not in DOCS:
                for match in BRAND.finditer(text):
                    findings.append("%s: operator brand near offset %d" % (relative, match.start()))
            for match in re.finditer(r"https?://[^\s)\"'`]+", text):
                url = match.group(0)
                if PUBLIC_URL_ALLOW.match(url):
                    continue
                host = url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
                if host.endswith(ALLOWED_HOST_SUFFIXES):
                    continue
                if not host.isascii() or any(char in url for char in "[](){}*+?\\"):
                    continue  # a placeholder or a regular expression, not a real host
                findings.append("%s: unreviewed external url %s" % (relative, url))
        self.assertEqual(findings, [], "operator-specific text found:\n" + "\n".join(findings))

    def test_loopback_is_the_only_listening_address(self):
        adapter = (ROOT / "adapter.py").read_text()
        self.assertIn("'127.0.0.1'", adapter)
        self.assertNotIn("'0.0.0.0'", adapter)

    def test_installer_requires_https_and_refuses_loopback_upstreams(self):
        source = (ROOT / "install.py").read_text()
        self.assertIn("upstream_must_be_https", source)
        self.assertIn("upstream_must_not_be_loopback", source)

    def test_no_runtime_state_is_committed(self):
        forbidden = ("models.json", "config.json", "service.log", "service-error.log",
                     "routing-rollback.json", "team-plan.json")
        present = [name for name in forbidden if (ROOT / name).exists()]
        self.assertEqual(present, [], "runtime state must not be committed: " + ", ".join(present))

    def test_readme_badges_match_the_actual_suite_size(self):
        """A stale "N tests" badge is the first thing a visitor can catch you on."""
        import unittest as unittest_module
        suite = unittest_module.defaultTestLoader.discover(str(ROOT), pattern="test_*.py")
        actual = suite.countTestCases()
        for doc in ("README.md", "README.en.md"):
            with self.subTest(doc=doc):
                text = (ROOT / doc).read_text()
                match = re.search(r"Tests-(\d+)%20offline", text)
                self.assertIsNotNone(match, "test badge missing from " + doc)
                self.assertEqual(int(match.group(1)), actual,
                                 "%s advertises %s tests but the suite has %d"
                                 % (doc, match.group(1), actual))

    def test_every_documented_image_exists_and_is_reproducible(self):
        """A broken image is the fastest way for a README to look abandoned."""
        import struct
        docs = ["README.md", "README.en.md", "docs/INSTALL.md", "docs/MODELS.md",
                "docs/ai-install.md", "docs/ai-install.en.md"]
        referenced = set()
        for doc in docs:
            path = ROOT / doc
            if not path.exists():
                continue
            text = path.read_text()
            for match in re.finditer(r'<img\s+src="([^"]+)"', text):
                target = match.group(1)
                if target.startswith(("http://", "https://")):
                    continue
                referenced.add(target)
                with self.subTest(doc=doc, image=target):
                    resolved = (path.parent / target).resolve()
                    self.assertTrue(resolved.is_file(), "%s points at a missing %s"
                                    % (doc, target))
        # The illustrations are generated, so the generator must ship with them.
        self.assertTrue((ROOT / "docs/assets/src/render_assets.py").is_file(),
                        "illustration source is missing; the PNGs become unreproducible")
        for name in ("model-picker.zh.png", "model-picker.en.png",
                     "compaction.zh.png", "compaction.en.png",
                     "switching.zh.png", "switching.en.png",
                     "capabilities.zh.png", "capabilities.en.png"):
            with self.subTest(asset=name):
                data = (ROOT / "docs/assets" / name).read_bytes()
                self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
                width, height = struct.unpack(">II", data[16:24])
                self.assertGreater(width, 800, name + " is too small to read")
                self.assertGreater(height, 200, name + " is too small to read")
        self.assertEqual(referenced, {
            "docs/assets/banner.svg",
            "docs/assets/model-picker.zh.png", "docs/assets/model-picker.en.png",
            "docs/assets/compaction.zh.png", "docs/assets/compaction.en.png",
            "docs/assets/switching.zh.png", "docs/assets/switching.en.png",
            "docs/assets/capabilities.zh.png", "docs/assets/capabilities.en.png"},
            "a documented image disappeared without the docs being updated")


if __name__ == "__main__":
    unittest.main(verbosity=2)
