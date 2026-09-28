"""Cross-vendor capability policy for Codex model catalogs.

A Codex model catalog is a list of ModelInfo objects. When several vendors sit
behind one gateway, the gateway usually returns a catalog that is accurate for
its own routing but silent about things Codex still needs to know:

* how large the usable context window is,
* whether the route accepts images,
* which reasoning levels exist,
* which models need the tool-image compatibility step.

This module holds that knowledge as *policy*, keyed by vendor family, and merges
it onto whatever the gateway reports. Two rules keep it honest:

1. The gateway decides which models exist. This module never invents a model and
   never adds one the operator's key cannot see.
2. Numbers that the vendor publishes come from the gateway or from the operator's
   own overrides. Policy only decides *how* to treat them, so a stale table here
   cannot silently shrink or inflate a working deployment.

Nothing in this file is secret and nothing here performs network or file I/O.
"""

from __future__ import annotations

import fnmatch
import copy

# Context window policies.
#   max      - use the largest window the model advertises. Correct when the
#              vendor does not surcharge long input (for example a 1M window at
#              the same per-token price).
#   standard - cap at the vendor's standard tier so an accidental 1M request is
#              not billed at a premium rate. `standard_cap` supplies the number.
#   gateway  - change nothing; trust the advertised value exactly.
MAX = "max"
STANDARD = "standard"
GATEWAY = "gateway"

# How a route treats hidden reasoning items.
#   preserve - keep opaque reasoning items untouched (native, same vendor).
#   summarize - drop hidden reasoning and keep only its public summary, because
#              the receiving vendor cannot read another vendor's ciphertext.
PRESERVE = "preserve"
SUMMARIZE = "summarize"

# Media transport policy.
#   native   - the route keeps tool results with their images; no rewriting.
#   relocate - the route breaks call/result pairing around images, so the image
#              is moved into a labelled message after the complete group.
NATIVE = "native"
RELOCATE = "relocate"


def _family(vendor, match, *, context=MAX, standard_cap=None, reasoning=SUMMARIZE,
            media=NATIVE, modes=("text",), note=""):
    return {
        "vendor": vendor,
        "match": tuple(match),
        "context": context,
        "standard_cap": standard_cap,
        "reasoning": reasoning,
        "media": media,
        "modalities": tuple(modes),
        "note": note,
    }


# Order matters: the first family whose pattern matches a model slug wins, so
# put the more specific prefixes first. Patterns are shell-style globs.
FAMILIES = (
    _family("OpenAI", ("gpt-*", "o1*", "o3*", "o4*", "codex-*"),
            context=MAX, reasoning=PRESERVE, media=NATIVE,
            modes=("text", "image"),
            note="Native Codex route: opaque checkpoints stay opaque."),
    _family("Anthropic", ("claude-*",),
            context=MAX, reasoning=SUMMARIZE, media=NATIVE,
            modes=("text", "image"),
            note="Extended thinking ciphertext is vendor specific; keep summaries."),
    _family("Google", ("gemini-*",),
            context=MAX, reasoning=SUMMARIZE, media=RELOCATE,
            modes=("text", "image"),
            note="Commonly needs the tool-image relocation step behind relays."),
    _family("xAI", ("grok-*",),
            context=MAX, reasoning=SUMMARIZE, media=NATIVE,
            modes=("text", "image")),
    _family("DeepSeek", ("deepseek-*",),
            context=MAX, reasoning=SUMMARIZE, media=NATIVE,
            note="Long context is not surcharged on current public pricing."),
    _family("MiniMax", ("minimax-*", "abab*", "m1-*", "m2-*"),
            context=MAX, reasoning=SUMMARIZE, media=NATIVE,
            modes=("text", "image")),
    _family("Moonshot", ("kimi-*", "moonshot-*", "k2*"),
            context=MAX, reasoning=SUMMARIZE, media=NATIVE),
    _family("Zhipu", ("glm-*", "chatglm*"),
            context=MAX, reasoning=SUMMARIZE, media=NATIVE),
)


def family_for(slug):
    """Return the policy family for a model slug, or None when unknown."""
    if not isinstance(slug, str):
        return None
    lowered = slug.lower()
    for family in FAMILIES:
        if any(fnmatch.fnmatchcase(lowered, pattern) for pattern in family["match"]):
            return family
    return None


def known_vendors():
    return tuple(dict.fromkeys(family["vendor"] for family in FAMILIES))


def _clamp_window(model, family):
    """Apply the context policy without ever raising an advertised limit."""
    limits = [v for v in (model.get("context_window"), model.get("max_context_window"))
              if isinstance(v, int) and v > 0]
    if not limits:
        return
    limit = max(limits)
    if family["context"] == STANDARD and family["standard_cap"]:
        limit = min(limit, family["standard_cap"])
    elif family["context"] == GATEWAY:
        return
    model["context_window"] = limit
    model["max_context_window"] = limit
    budget = model.get("auto_compact_token_limit")
    if isinstance(budget, int) and budget > limit:
        model["auto_compact_token_limit"] = int(limit * 0.85)


def apply_policy(model, overrides=None):
    """Return a copy of one ModelInfo with vendor policy applied.

    `overrides` is an optional mapping of model slug to a partial policy dict,
    which is how an operator corrects a family for their own gateway without
    editing this file. Unknown models pass through unchanged.
    """
    result = copy.deepcopy(model)
    slug = result.get("slug")
    family = family_for(slug)
    if family is None and not (overrides or {}).get(slug):
        return result
    policy = dict(family or {})
    policy.update((overrides or {}).get(slug) or {})
    _clamp_window(result, {
        "context": policy.get("context", GATEWAY),
        "standard_cap": policy.get("standard_cap"),
    })
    modes = policy.get("modalities")
    if modes:
        current = result.get("input_modalities")
        if not isinstance(current, list) or not current:
            result["input_modalities"] = list(modes)
    note = policy.get("note")
    if note and isinstance(result.get("description"), str) and note not in result["description"]:
        result["description"] = result["description"].rstrip() + " " + note
    return result


def apply_policy_to_catalog(catalog, overrides=None):
    """Apply policy to every model in a gateway catalog."""
    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        raise ValueError("catalog_must_be_modelinfo_list")
    result = copy.deepcopy(catalog)
    result["models"] = [apply_policy(model, overrides) for model in catalog["models"]]
    return result


def media_models(catalog, overrides=None):
    """Model slugs whose images must be relocated behind the gateway.

    The gateway decides availability; this list only decides which of the
    advertised models need the compatibility step.
    """
    if not isinstance(catalog, dict):
        return []
    result = []
    for model in catalog.get("models") or []:
        slug = model.get("slug") if isinstance(model, dict) else None
        if not isinstance(slug, str):
            continue
        family = family_for(slug)
        if family is None:
            continue
        policy = dict(family)
        policy.update((overrides or {}).get(slug) or {})
        if policy.get("media") == RELOCATE:
            result.append(slug)
    return result


def requires_reasoning_summary(slug, overrides=None):
    """True when hidden reasoning from another vendor must not be forwarded."""
    family = family_for(slug)
    if family is None:
        return False
    policy = dict(family)
    policy.update((overrides or {}).get(slug) or {})
    return policy.get("reasoning") == SUMMARIZE


def describe(overrides=None):
    """Human readable policy table, used by `build_catalog.py --list-families`."""
    rows = []
    for family in FAMILIES:
        policy = dict(family)
        policy.update((overrides or {}).get(family["vendor"]) or {})
        rows.append({
            "vendor": family["vendor"],
            "match": list(family["match"]),
            "context_policy": policy.get("context"),
            "standard_cap": policy.get("standard_cap"),
            "reasoning": policy.get("reasoning"),
            "media": policy.get("media"),
        })
    return rows
