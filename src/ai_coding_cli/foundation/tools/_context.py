"""ToolContext passed to every tool call. See ADR-0013."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from ..config import Config


@dataclass(frozen=True)
class ToolContext:
    """Per-invocation context. Tools read from it; they do not mutate it.

    Created by the Tool Registry just before dispatching `Tool.call(args, ctx)`.
    """

    config: "Config"
    session_id: UUID | None
    conversation_id: UUID | None
    invocation_id: str
    dry_run: bool = False
