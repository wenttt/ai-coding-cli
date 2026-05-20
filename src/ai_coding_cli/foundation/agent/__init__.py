"""Agent Core: ReAct loop. See ADR-0009 + ADR-0030.

Public exports:
    - Agent: the ReAct runtime
    - AgentResult: outcome dataclass
    - AgentOutcome: enum of terminal states
"""

from __future__ import annotations

from ._agent import Agent
from ._result import AgentOutcome, AgentResult

__all__ = [
    "Agent",
    "AgentResult",
    "AgentOutcome",
]
