"""Prompt versioning via Langfuse prompt management.

Prompts are fetched by name with label "production" (cached 5 min). The code-reviewed default in the repo
is the fallback, so a Langfuse outage never breaks answers. Every deploy runs `prompt-seed`, which publishes
a repo default as a new "production" version only when its text changed (so the reviewed code IS what runs);
a rollback is moving the label back in Langfuse.
"""
from __future__ import annotations

import os
from functools import lru_cache

LABEL = os.getenv("PROMPT_LABEL", "production")


@lru_cache
def _client():
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        return None
    from langfuse import Langfuse

    return Langfuse()


def get_prompt(name: str, default: str) -> tuple[str, str]:
    """-> (prompt text, version tag) - version tag is 'langfuse:<n>' or 'repo'."""
    lf = _client()
    if lf is None:
        return default, "repo"
    try:
        p = lf.get_prompt(name, label=LABEL, cache_ttl_seconds=300, fallback=default, fetch_timeout_seconds=2, max_retries=1)
        return p.prompt, (f"langfuse:{p.version}" if not getattr(p, "is_fallback", False) else "repo-fallback")
    except Exception:  # noqa: BLE001 - never let prompt management break answering
        return default, "repo-fallback"


def defaults() -> dict[str, str]:
    from .explain_llm import SYSTEM as EXPLAINER
    from .loop import SYSTEM as ASSISTANT

    return {"assistant-system": ASSISTANT, "explainer-system": EXPLAINER}


def seed_cli() -> None:
    """Runs on every deploy (Helm hook). Never fails a deploy: without Langfuse the repo default is used anyway."""
    lf = _client()
    if lf is None:
        print("prompt-seed: Langfuse keys not set; the repo defaults are used directly")
        return
    try:
        _seed(lf)
    except Exception as e:  # noqa: BLE001
        print(f"prompt-seed: Langfuse unavailable ({type(e).__name__}); prompts stay at their current version")


def _seed(lf) -> None:
    for name, text in defaults().items():
        try:
            live = lf.get_prompt(name, label=LABEL, cache_ttl_seconds=0)
            if live.prompt == text:
                print(f"{name}: unchanged (version {live.version} is {LABEL})")
                continue
        except Exception as e:  # noqa: BLE001 - not created yet
            print(f"{name}: no {LABEL} version yet ({type(e).__name__})")
        p = lf.create_prompt(name=name, prompt=text, labels=[LABEL], type="text",
                             commit_message="published from repo default by deploy")
        print(f"{name}: published version {p.version} labelled {LABEL}")
    lf.flush()
