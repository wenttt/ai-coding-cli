"""JiraReactor: polling loop. See ADR-0029 §Polling channel."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import uuid4

from ...foundation.config import Config
from ...foundation.tools import ToolContext, ToolRegistry
from ..pipeline import JiraStateChangeEvent, PipelineOrchestrator

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class JiraReactorConfig:
    """Polling knobs. Mirrors ADR-0029 §Configuration."""

    poll_active_seconds: int = 60
    poll_idle_seconds: int = 300
    initial_lookback_minutes: int = 60
    max_events_per_poll: int = 50


class JiraReactor:
    """Reactor that polls Jira for ticket changes + drives PipelineOrchestrator.

    Lite delivers events via polling only. The reactor maintains a per-ticket
    last-seen cursor in-memory; the orchestrator's `processed_jira_events`
    table dedups across daemon restarts.

    Concurrency: the reactor processes events sequentially. Per ADR-0030 the
    Lite profile is single-asyncio-loop single-developer, so no locking is
    needed.
    """

    def __init__(
        self,
        *,
        orchestrator: PipelineOrchestrator,
        tool_registry: ToolRegistry,
        config: Config,
        reactor_config: JiraReactorConfig | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._tools = tool_registry
        self._config = config
        self._reactor_config = reactor_config or JiraReactorConfig()
        self._last_seen: dict[str, str] = {}
        self._stop_event = asyncio.Event()

    # -----------------------------------------------------------------
    # Public entry points
    # -----------------------------------------------------------------

    async def poll_once(self) -> list[JiraStateChangeEvent]:
        """Run one poll cycle and dispatch resulting events to the orchestrator.

        Returns the events dispatched (useful for tests + observability).
        """
        tickets = await self._fetch_my_tickets()
        events = self._diff_into_events(tickets)
        capped = events[: self._reactor_config.max_events_per_poll]
        for event in capped:
            try:
                await self._orchestrator.react(event)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "jira_reactor.react_failed jira_key=%s to_status=%s",
                    event.jira_key,
                    event.to_status,
                )
        # Update cursor AFTER dispatch so a crash during react() means we
        # re-deliver next poll (the dedup table prevents double-processing).
        self._commit_cursor(tickets)
        return capped

    async def run_forever(self) -> None:
        """Sleep + poll loop. Cancels on stop()."""
        while not self._stop_event.is_set():
            events = await self.poll_once()
            cadence = (
                self._reactor_config.poll_active_seconds
                if events
                else self._reactor_config.poll_idle_seconds
            )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=cadence
                )
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self._stop_event.set()

    # -----------------------------------------------------------------
    # Tools wrappers
    # -----------------------------------------------------------------

    async def _fetch_my_tickets(self) -> list[dict[str, Any]]:
        result = await self._tools.call(
            "list_my_tickets",
            {},
            self._tool_context(),
        )
        if not result.is_success:
            logger.warning(
                "jira_reactor.list_my_tickets_failed detail=%s",
                result.content[:200],
            )
            return []
        try:
            payload = json.loads(result.content)
        except (json.JSONDecodeError, ValueError):
            logger.warning("jira_reactor.tickets_parse_failed")
            return []
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("tickets"), list):
            return payload["tickets"]
        return []

    # -----------------------------------------------------------------
    # Event diffing
    # -----------------------------------------------------------------

    def _diff_into_events(
        self, tickets: Iterable[dict[str, Any]]
    ) -> list[JiraStateChangeEvent]:
        events: list[JiraStateChangeEvent] = []
        for ticket in tickets:
            key = ticket.get("key")
            status = ticket.get("status")
            updated = ticket.get("updated") or ticket.get("updated_at")
            if not key or not status:
                continue
            cursor = f"{status}@{updated or ''}"
            previous = self._last_seen.get(key)
            if previous == cursor:
                # No change since last poll.
                continue
            observed_at = _parse_iso_datetime(updated) or _utcnow()
            events.append(
                JiraStateChangeEvent(
                    jira_key=key,
                    from_status=_status_only(previous),
                    to_status=status,
                    observed_at=observed_at,
                    delivery_channel="polling",
                )
            )
        return events

    def _commit_cursor(self, tickets: Iterable[dict[str, Any]]) -> None:
        for ticket in tickets:
            key = ticket.get("key")
            status = ticket.get("status")
            updated = ticket.get("updated") or ticket.get("updated_at") or ""
            if not key or not status:
                continue
            self._last_seen[key] = f"{status}@{updated}"

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _tool_context(self) -> ToolContext:
        return ToolContext(
            config=self._config,
            session_id=None,
            conversation_id=None,
            invocation_id=uuid4().hex,
            dry_run=False,
        )


def _status_only(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    return cursor.split("@", 1)[0] or None


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Accept both naive + aware ISO strings.
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None
