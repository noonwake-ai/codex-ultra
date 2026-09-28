#!/usr/bin/env python3
"""Build a Codex model catalog from a gateway, then apply vendor policy.

The gateway is the only source of truth about which models your key can reach.
This tool never adds a model that the gateway did not return; it only fills in
the policy fields described in model_presets.py.

    python3 build_catalog.py --gateway https://gateway.example/v1 --out models.json
    python3 build_catalog.py --from-file gateway-models.json --out models.json
    python3 build_catalog.py --list-families

The API key is read from an environment variable and never written to disk, never
echoed, and never placed in the output catalog.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import model_presets

MAX_BYTES = 8 * 1024 * 1024
SUSPECT = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._~-]{16,}|https?://[^\s/]+:[^\s/@]+@")


class BuildError(Exception):
    """Fixed, non-sensitive failure code."""


def _fail(code):
    raise BuildError(code)


def fetch_catalog(gateway, api_key, client_version, timeout=30):
    """Read the gateway's Codex model list. Read-only, single request."""
    url = gateway.rstrip("/") + "/models?" + urllib.parse.urlencode(
        {"client_version": client_version})
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        _fail("gateway_http_" + str(exc.code))
    except urllib.error.URLError:
        _fail("gateway_unreachable")
    if len(body) > MAX_BYTES:
        _fail("catalog_too_large")
    if api_key.encode() in body:
        _fail("gateway_echoed_credential")
    return parse_catalog(body)


def parse_catalog(body):
    try:
        catalog = json.loads(body)
    except (UnicodeError, ValueError):
        _fail("gateway_returned_non_json")
    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        _fail("not_a_codex_modelinfo_list")
    slugs = [m.get("slug") for m in catalog["models"] if isinstance(m, dict)]
    if len(slugs) != len(catalog["models"]) or any(not isinstance(s, str) for s in slugs):
        _fail("model_without_slug")
    if len(set(slugs)) != len(slugs):
        _fail("duplicate_model_slug")
    return catalog


def select(catalog, only, exclude):
    models = catalog["models"]
    if only:
        wanted = [s.strip() for s in only.split(",") if s.strip()]
        missing = [s for s in wanted if s not in {m["slug"] for m in models}]
        if missing:
            _fail("requested_model_not_offered:" + ",".join(missing))
        models = [m for m in models if m["slug"] in set(wanted)]
    if exclude:
        blocked = {s.strip() for s in exclude.split(",") if s.strip()}
        models = [m for m in models if m["slug"] not in blocked]
    if not models:
        _fail("no_models_selected")
    return {**catalog, "models": models}


def overrides_from(path):
    if not path:
        return {}
    data = json.loads(pathlib.Path(path).read_text())
    if not isinstance(data, dict):
        _fail("overrides_must_be_a_json_object")
    return data


def write_catalog(path, catalog):
    target = pathlib.Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    if SUSPECT.search(text):
        _fail("catalog_contains_credential_like_text")
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(text)
    temp.replace(target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def cmd_list_families():
    rows = model_presets.describe()
    width = max(len(row["vendor"]) for row in rows)
    print("Vendor".ljust(width), "Context policy", "Reasoning", "Media bridge")
    for row in rows:
        cap = row["standard_cap"]
        policy = row["context_policy"] + (f" (cap {cap})" if cap else "")
        print(row["vendor"].ljust(width), policy.ljust(14),
              row["reasoning"].ljust(9), row["media"])
    print("\nPatterns:")
    for row in rows:
        print("  " + row["vendor"] + ": " + ", ".join(row["match"]))
    print("\nThe gateway decides which models exist. These rules decide how each one "
          "is treated once it appears.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gateway", help="gateway base URL, for example https://gateway.example/v1")
    parser.add_argument("--from-file", help="use a previously saved gateway catalog")
    parser.add_argument("--api-key-env", default="CODEX_ULTRA_API_KEY")
    parser.add_argument("--client-version", default="0.158.0")
    parser.add_argument("--out", default="models.json")
    parser.add_argument("--overrides", help="JSON file mapping slug to policy overrides")
    parser.add_argument("--only", help="comma separated slugs to keep")
    parser.add_argument("--exclude", help="comma separated slugs to drop")
    parser.add_argument("--list-families", action="store_true")
    parser.add_argument("--print-media-models", action="store_true")
    args = parser.parse_args(argv)

    if args.list_families:
        cmd_list_families()
        return 0
    if bool(args.gateway) == bool(args.from_file):
        parser.error("pass exactly one of --gateway or --from-file")

    try:
        overrides = overrides_from(args.overrides)
        if args.from_file:
            catalog = parse_catalog(pathlib.Path(args.from_file).read_bytes())
        else:
            api_key = os.environ.get(args.api_key_env)
            if not api_key:
                _fail("api_key_env_not_set:" + args.api_key_env)
            catalog = fetch_catalog(args.gateway, api_key, args.client_version)
        catalog = select(catalog, args.only, args.exclude)
        merged = model_presets.apply_policy_to_catalog(catalog, overrides)
        target = write_catalog(args.out, merged)
    except BuildError as exc:
        print(json.dumps({"ok": False, "code": str(exc)}), file=sys.stderr)
        return 2

    families = {}
    for model in merged["models"]:
        family = model_presets.family_for(model["slug"])
        families.setdefault(family["vendor"] if family else "unclassified", []).append(
            model["slug"])
    summary = {
        "ok": True,
        "catalog": str(target),
        "models": len(merged["models"]),
        "by_vendor": {k: len(v) for k, v in sorted(families.items())},
        "media_bridge_models": model_presets.media_models(merged, overrides),
    }
    if args.print_media_models:
        print(json.dumps(summary["media_bridge_models"], ensure_ascii=False))
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
