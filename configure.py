#!/usr/bin/env python3
"""Point Codex at the local adapter, and point it back on rollback.

Only one field is ever touched: the `base_url` of the provider that Codex is
currently using. Model choice, reasoning effort, catalog, skills, MCP servers,
plugins, auth tokens and chat history are left exactly as they were.

    python3 configure.py status
    python3 configure.py apply   --upstream https://gateway.example/v1
    python3 configure.py rollback

CC Switch users can add `--cc-switch-db ~/.cc-switch/cc-switch.db` so its stored
copy of the same provider is updated too. That keeps the next CC Switch switch
from silently undoing this change. The rollback record stores endpoint metadata
only; never credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

try:
    import tomli as tomllib
except ImportError:  # Python 3.11+
    import tomllib

LOCAL_DEFAULT = "http://127.0.0.1:15731"
HOME = Path.home()
ROOT = HOME / "Library/Application Support/Codex Ultra"
CONFIG = HOME / ".codex/config.toml"
CC_DB = HOME / ".cc-switch/cc-switch.db"


class ConfigureError(Exception):
    """Fixed, non-sensitive failure code."""


def _fail(code):
    raise ConfigureError(code)


def load_config(path):
    try:
        return tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        _fail("codex_config_missing")
    except tomllib.TOMLDecodeError:
        _fail("codex_config_unparseable")


def active_provider(parsed, override=None):
    """The provider Codex is currently routed through."""
    providers = parsed.get("model_providers") or {}
    if not providers:
        _fail("no_model_providers_configured")
    if override:
        if override not in providers:
            _fail("provider_not_found:" + override)
        return override
    current = parsed.get("model_provider")
    if isinstance(current, str) and current in providers:
        return current
    if len(providers) == 1:
        return next(iter(providers))
    _fail("model_provider_not_set_pass_--provider")


def replace_endpoint(text, provider, source, target):
    """Rewrite exactly one base_url, then prove nothing else changed."""
    parsed = tomllib.loads(text)
    section = ""
    lines, count = [], 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            section = line.strip().strip("[]")
        if section == "model_providers." + provider and re.match(r"\s*base_url\s*=", line):
            current = parsed["model_providers"][provider].get("base_url")
            if current != source:
                _fail("endpoint_changed_concurrently")
            line = re.sub(r"(?<==)\s*.*$", " " + json.dumps(target), line.rstrip("\n")) + "\n"
            count += 1
        lines.append(line)
    if count != 1:
        _fail("expected_exactly_one_base_url")
    result = "".join(lines)
    expected = json.loads(json.dumps(parsed))
    expected["model_providers"][provider]["base_url"] = target
    if tomllib.loads(result) != expected:
        _fail("non_endpoint_diff")
    return result


def write_atomic(path, content):
    path = Path(path).resolve()
    mode = path.stat().st_mode & 0o777
    handle, temp = tempfile.mkstemp(prefix=".codex-ultra-", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def cc_switch_row(db, provider):
    connection = sqlite3.connect("file:" + str(db) + "?mode=ro", uri=True, timeout=5)
    try:
        row = connection.execute(
            "SELECT settings_config FROM providers WHERE id=? AND app_type='codex'",
            (provider,)).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        _fail("cc_switch_database_unreadable")
    finally:
        connection.close()


def cc_switch_write(db, provider, stored):
    connection = sqlite3.connect(db, timeout=5)
    try:
        connection.execute(
            "UPDATE providers SET settings_config=? WHERE id=? AND app_type='codex'",
            (json.dumps(stored, ensure_ascii=False), provider))
        connection.commit()
    finally:
        connection.close()


def cc_switch_plan(db, provider, source, target):
    """Validate and prepare the CC Switch row without writing anything.

    This runs before `config.toml` is touched so that a missing provider, an
    unreadable database or a credential-bearing row aborts the whole command
    instead of leaving the platform config half-switched.
    """
    raw = cc_switch_row(db, provider)
    if raw is None:
        _fail("cc_switch_provider_not_found:" + provider)
    stored = json.loads(raw)
    stored["config"] = replace_endpoint(stored["config"], provider, source, target)
    if "OPENAI_API_KEY" in stored["config"]:
        _fail("refusing_to_rewrite_provider_that_holds_credentials")
    return stored


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["status", "apply", "rollback"])
    parser.add_argument("--config", default=str(CONFIG))
    parser.add_argument("--provider", help="provider key in config.toml (default: current)")
    parser.add_argument("--upstream", help="gateway base URL, required for apply")
    parser.add_argument("--local", default=LOCAL_DEFAULT)
    parser.add_argument("--rollback-record", default=str(ROOT / "routing-rollback.json"))
    parser.add_argument("--cc-switch-db", nargs="?", const=str(CC_DB), default=None)
    args = parser.parse_args(argv)

    try:
        live = Path(args.config).read_text()
        parsed = load_config(args.config)
        provider = active_provider(parsed, args.provider)
        record = Path(args.rollback_record)

        if args.action == "status":
            print(json.dumps({
                "provider": provider,
                "endpoint": parsed["model_providers"][provider].get("base_url"),
                "local_endpoint": args.local,
                "routed_locally": parsed["model_providers"][provider].get("base_url") == args.local,
                "rollback_record": record.exists(),
            }, ensure_ascii=False, indent=2))
            return 0

        if args.action == "apply":
            if not args.upstream:
                parser.error("apply requires --upstream")
            source, target = args.upstream, args.local
            if record.exists():
                _fail("rollback_record_already_exists")
            updated = replace_endpoint(live, provider, source, target)
        else:
            if not record.exists():
                _fail("rollback_record_missing")
            saved = json.loads(record.read_text())
            source, target = saved["local_endpoint"], saved["upstream"]
            updated = replace_endpoint(live, provider, source, target)

        stored = None
        if args.cc_switch_db:
            stored = cc_switch_plan(args.cc_switch_db, provider, source, target)

        if args.action == "apply":
            record.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(record.parent, 0o700)
            record.write_text(json.dumps({
                "provider": provider,
                "upstream": source,
                "local_endpoint": target,
                "original_config_sha256": hashlib.sha256(live.encode()).hexdigest(),
                "note": "Endpoint metadata only. No credentials or prompt content.",
            }, ensure_ascii=False, indent=2) + "\n")
            os.chmod(record, 0o600)

        if Path(args.config).read_text() != live:
            _fail("config_changed_during_write")
        write_atomic(args.config, updated)
        if stored is not None:
            cc_switch_write(args.cc_switch_db, provider, stored)
        if args.action == "rollback":
            record.unlink()
        print(json.dumps({"ok": True, "action": args.action, "provider": provider,
                          "endpoint": target}, ensure_ascii=False))
        return 0
    except ConfigureError as exc:
        print(json.dumps({"ok": False, "code": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
