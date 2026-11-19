"""Version fingerprints recorded with every run and every piece of feedback, so a later reader knows
which prompts, ICP rules and collection/sending policy produced the thing that was judged."""
from __future__ import annotations

import hashlib
from typing import Dict

from .config import CONFIG_DIR

POLICY_VERSION = "policy-2026-11"   # bump when safety/sending/collection rules in code change


def _h(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


def prompt_version() -> str:
    from . import drafting, llm
    return "prompts-" + _h(llm.EXTRACT_PROMPT, drafting.REWRITE_PROMPT, drafting.CLOSING_EXTRA,
                           " ".join(drafting.HOOK_PRIORITY))


def icp_version() -> str:
    try:
        raw = (CONFIG_DIR / "icp.yaml").read_text(encoding="utf-8")
    except OSError:
        raw = ""
    return "icp-" + _h(raw)


def policy_version() -> str:
    try:
        raw = (CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8")
    except OSError:
        raw = ""
    return "%s-%s" % (POLICY_VERSION, _h(raw))


def current() -> Dict[str, str]:
    return {"prompt_version": prompt_version(), "icp_version": icp_version(), "policy_version": policy_version()}
