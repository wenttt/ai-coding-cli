"""Side-effect classification + recording. See ADR-0013."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class SideEffectClass(StrEnum):
    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    EXTERNAL_READ = "external_read"
    EXTERNAL_WRITE = "external_write"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class SideEffectRecord:
    """One concrete side effect performed by a tool call.

    Collected on `ToolResult.side_effects_recorded`. Surfaced in operation
    logs' "Impact" section and the Dashboard side-effect timeline.
    """

    class_: SideEffectClass
    summary: str
    details: dict[str, Any]
    timestamp: datetime


class SideEffectRecorder:
    """Per-call recorder. Tools call `record()` to append a SideEffectRecord.

    The recorder is created fresh per tool invocation by the registry; it
    isn't a singleton, so concurrent tool calls do not collide.
    """

    def __init__(self, default_class: SideEffectClass) -> None:
        self._default_class = default_class
        self._records: list[SideEffectRecord] = []

    @property
    def records(self) -> list[SideEffectRecord]:
        return list(self._records)

    def record(
        self,
        summary: str,
        *,
        class_: SideEffectClass | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._records.append(
            SideEffectRecord(
                class_=class_ or self._default_class,
                summary=summary,
                details=dict(details or {}),
                timestamp=datetime.now(timezone.utc),
            )
        )
