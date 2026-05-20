"""ai-coding-cli — single-user AI Coding Agent for the Jira→deploy pipeline.

Public API surface. Internal modules live under `foundation/` (generic Agent
runtime) and `application/` (AI Coding Workflow business pipeline). See
ADR-0002 for the package layering rules.
"""

from __future__ import annotations

__version__ = "0.2.0.dev0+lite"

__all__ = ["__version__"]
