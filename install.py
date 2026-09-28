#!/usr/bin/env python3
"""Install the Code Ultra local adapter on macOS and route Codex through it.

    python3 install.py --upstream https://gateway.example/v1 \
        --compactor-model gpt-6-sol --compactor-effort medium

What it does:

1. copies the source into a private application-support directory,
2. builds an isolated virtual environment and downloads the public tokenizer
   tables (once, then cached so restarts work offline),
3. creates the checkpoint key in the login Keychain, one exact entry,
4. registers a per-user launchd job and waits for a healthy health check,
5. only after the service is healthy, rewrites one `base_url` in
   `~/.codex/config.toml` and records how to undo that.

It never reads, copies or prints a credential, and never sends a model request.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_ROOT = pathlib.Path.home() / "Library/Application Support/Code Ultra"
DEFAULT_PORT = 15731
DEFAULT_LABEL = "ai.codeultra.local-adapter"
SOURCES = (
    "adapter.py", "tool_image_bridge.py", "model_presets.py", "build_catalog.py",
    "direct_handoff.py", "native_checkpoint.py", "configure.py", "requirements.txt",
)


class InstallError(Exception):
    """Fixed, non-sensitive failure code."""


def _fail(code):
    raise InstallError(code)


def validate_upstream(url):
    if not isinstance(url, str) or not re.match(r"^https://[^/\s]+", url):
        _fail("upstream_must_be_https")
    host = url.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0]
    if host in ("localhost", "127.0.0.1", "::1"):
        _fail("upstream_must_not_be_loopback")
    return url.rstrip("/")


def read_media_models(catalog_path):
    """Media routing is a property of the catalog, not of this script."""
    if not catalog_path:
        return []
    try:
        catalog = json.loads(pathlib.Path(catalog_path).read_text())
    except (OSError, ValueError):
        _fail("catalog_unreadable")
    import model_presets
    return model_presets.media_models(catalog)


def validate_compactor_model(model, catalog_path):
    """The compactor has to be a model this key was actually shown.

    The catalog is the gateway's own answer to "what do I offer you", so it is
    the only honest source for this check. A wrong compactor model would
    otherwise install cleanly and only surface when a long task first needs to
    compact, which is the worst possible moment to find out. With no catalog
    supplied the check is skipped rather than guessed.
    """
    if not catalog_path:
        return
    try:
        catalog = json.loads(pathlib.Path(catalog_path).read_text())
    except (OSError, ValueError):
        _fail("catalog_unreadable")
    slugs = set()
    for entry in catalog.get("models") or []:
        slug = entry.get("slug") if isinstance(entry, dict) else None
        if isinstance(slug, str) and slug:
            slugs.add(slug)
    if model not in slugs:
        _fail("compactor_model_not_in_catalog:" + str(model))


def resolve_provider(config_path, override):
    """Which config.toml provider key the adapter must read the credential from."""
    if override:
        return override
    import configure as router
    parsed = router.load_config(config_path)
    return router.active_provider(parsed)


def build_runtime(root, python):
    """Build the private virtual environment, or fail with one fixed code.

    A missing interpreter, no network for pip or a full disk used to escape as a
    raw CalledProcessError traceback, which tells an installer user nothing.
    """
    runtime = root / "runtime"
    pip = str(runtime / "bin/python")
    try:
        if not (runtime / "bin/python").exists():
            subprocess.run([python, "-m", "venv", str(runtime)], check=True)
        subprocess.run([pip, "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
                       check=True)
        subprocess.run([pip, "-m", "pip", "install", "--quiet",
                        "-r", str(root / "requirements.txt")], check=True)
        cache = root / "tokenizer-cache"
        cache.mkdir(exist_ok=True)
        os.chmod(cache, 0o700)
        subprocess.run([pip, "-c",
                        'import os,sys,tiktoken;os.environ["TIKTOKEN_CACHE_DIR"]=sys.argv[1];'
                        'tiktoken.get_encoding("o200k_base");tiktoken.get_encoding("cl100k_base")',
                        str(cache)], check=True)
    except (subprocess.CalledProcessError, OSError):
        _fail("runtime_build_failed_check_python_and_network")
    return pip


def write_launchagent(root, label, python, config):
    plist_path = pathlib.Path.home() / "Library/LaunchAgents" / (label + ".plist")
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": label,
        "ProgramArguments": [python, str(root / "adapter.py"), "--config", str(config)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "WorkingDirectory": str(root),
        "StandardOutPath": str(root / "service.log"),
        "StandardErrorPath": str(root / "service-error.log"),
        "Umask": 63,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    target = "gui/" + str(os.getuid()) + "/" + label
    loaded = subprocess.run(["launchctl", "print", target],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if loaded:
        subprocess.run(["launchctl", "bootout", target], check=True,
                       stdout=subprocess.DEVNULL)
    plist_path.write_bytes(plistlib.dumps(payload))
    os.chmod(plist_path, 0o600)
    subprocess.run(["launchctl", "bootstrap", "gui/" + str(os.getuid()),
                    str(plist_path)], check=True)
    return plist_path


def wait_for_health(port, seconds=20):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/health" % port, timeout=1) as response:
                if json.load(response).get("status") == "ok":
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--upstream", required=True, help="gateway base URL")
    parser.add_argument("--compactor-model", required=True,
                        help="model used to summarize context, for example deepseek-v4-flash")
    parser.add_argument("--compactor-effort", default="medium")
    parser.add_argument("--catalog", default="models.json",
                        help="catalog built by build_catalog.py, used for media routing")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--codex-config", default=str(pathlib.Path.home() / ".codex/config.toml"))
    parser.add_argument("--provider", help="provider key in config.toml (default: current)")
    parser.add_argument("--cc-switch-db", default=None)
    parser.add_argument("--skip-route", action="store_true",
                        help="install the service but leave Codex pointing at the gateway")
    args = parser.parse_args(argv)

    if sys.version_info < (3, 11):
        _fail("python_3_11_or_newer_required")
    if not sys.platform == "darwin":
        _fail("macos_required_for_the_launchagent_installer")

    try:
        upstream = validate_upstream(args.upstream)
        root = pathlib.Path(args.root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        for name in SOURCES:
            source = HERE / name
            if not source.exists():
                _fail("missing_source:" + name)
            target = root / name
            if source != target:
                shutil.copy2(source, target)
        existing = root / "config.json"
        if existing.exists():
            _fail("already_installed_use_configure_status")
        validate_compactor_model(args.compactor_model, args.catalog)
        python = build_runtime(root, sys.executable)

        provider = resolve_provider(args.codex_config, args.provider)
        config = root / "config.json"
        config.write_text(json.dumps({
            "port": args.port,
            "upstream": upstream,
            "compactor_model": args.compactor_model,
            "compactor_effort": args.compactor_effort,
            "media_models": read_media_models(args.catalog),
            "catalog": str(pathlib.Path(args.catalog).expanduser().resolve())
                       if args.catalog else None,
            "cc_db": args.cc_switch_db,
            "credential_provider_id": provider if args.cc_switch_db else None,
            "credential_env": "CODE_ULTRA_API_KEY",
        }, indent=2) + "\n")
        os.chmod(config, 0o600)

        if subprocess.run([python, str(root / "adapter.py"), "--config", str(config),
                           "--init-key"]).returncode != 0:
            _fail("checkpoint_key_init_failed")
        write_launchagent(root, args.label, python, config)
        if not wait_for_health(args.port):
            _fail("service_not_healthy_route_unchanged")
        if args.skip_route:
            print(json.dumps({"ok": True, "service": "healthy", "route": "unchanged"}))
            return 0
        route = [python, str(root / "configure.py"), "apply",
                 "--upstream", upstream, "--local", "http://127.0.0.1:%d" % args.port,
                 "--config", args.codex_config, "--provider", provider]
        if args.cc_switch_db:
            route += ["--cc-switch-db", args.cc_switch_db]
        if subprocess.run(route).returncode != 0:
            _fail("routing_step_failed")
    except InstallError as exc:
        print(json.dumps({"ok": False, "code": str(exc)}), file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(json.dumps({"ok": False, "code": "step_failed",
                          "command": pathlib.Path(exc.cmd[0]).name}), file=sys.stderr)
        return 2

    print(json.dumps({"ok": True, "service": "healthy", "route": "local adapter",
                      "next": "start a new Codex chat or restart Codex"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
