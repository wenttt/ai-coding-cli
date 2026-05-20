"""Loads the packaged Tier 1 system prompt. See ADR-0010."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_SYSTEM_PROMPT_PATH = Path(__file__).with_name("system_prompt.md")


@lru_cache(maxsize=1)
def load_system_prompt() -> str:
    """Return the Tier 1 system prompt content.

    Cached per process — the prompt is identical across all Agent invocations
    for a given installed package version, which is what makes Tier 1
    cache-friendly on the LLM provider side too.
    """
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
