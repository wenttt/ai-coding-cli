"""Three-tier Context Layer. See ADR-0010.

Public exports:
    - ContextBuilder: assembles messages list for the LLM
    - StaticPrefixAssembler: builds Tier 2 static prefix
    - RepoFacts, LoadedSkill: small input types
    - load_system_prompt(): reads packaged Tier 1 prompt
"""

from __future__ import annotations

from ._builder import ContextBuilder
from ._static_prefix import LoadedSkill, RepoFacts, StaticPrefixAssembler
from ._system_prompt import load_system_prompt

__all__ = [
    "ContextBuilder",
    "StaticPrefixAssembler",
    "RepoFacts",
    "LoadedSkill",
    "load_system_prompt",
]
