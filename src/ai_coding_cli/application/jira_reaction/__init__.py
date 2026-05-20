"""Jira polling reactor. See ADR-0029.

Lite scope: polling-only (no webhook / relay). The reactor runs as part of
the daemon's asyncio loop; one poll cycle queries Jira for assigned tickets
updated since the last cursor, materializes JiraStateChangeEvent objects,
and feeds them to PipelineOrchestrator.react.

Public exports:
    - JiraReactor: poll-loop runner
    - JiraReactorConfig: cadence + lookback knobs
"""

from __future__ import annotations

from ._reactor import JiraReactor, JiraReactorConfig

__all__ = [
    "JiraReactor",
    "JiraReactorConfig",
]
