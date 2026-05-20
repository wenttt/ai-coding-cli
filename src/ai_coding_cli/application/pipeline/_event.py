"""JiraStateChangeEvent: orchestrator's input. See ADR-0003 + ADR-0029."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class JiraStateChangeEvent:
    """One observed Jira state transition.

    The orchestrator deduplicates by `dedup_key` against the
    `processed_jira_events` table per ADR-0029.
    """

    jira_key: str
    from_status: str | None
    to_status: str
    observed_at: datetime
    delivery_channel: Literal["webhook", "polling", "manual"]

    @property
    def dedup_key(self) -> str:
        """sha256(jira_key + to_status + observed_at_epoch_seconds). ADR-0029."""
        seed = (
            f"{self.jira_key}|{self.to_status}|{int(self.observed_at.timestamp())}"
        )
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()
