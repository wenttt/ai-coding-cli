"""PipelineOrchestrator: reacts to Jira state changes. See ADR-0003."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select

from ...foundation.agent import Agent, AgentOutcome
from ...foundation.compactor import Compactor
from ...foundation.config import Config
from ...foundation.context import ContextBuilder, RepoFacts
from ...foundation.errors import (
    AgentError,
    FatalError,
    JiraTransitionForbiddenError,
    PipelineStateInconsistencyError,
    RetryableError,
)
from ...foundation.llm._adapter import LLMAdapter
from ...foundation.session import SessionManager, SessionView
from ...foundation.storage import ProcessedJiraEvent, StorageEngine
from ...foundation.tools import ToolContext, ToolRegistry
from ..operation_log import (
    OperationLogBody,
    OperationLogReader,
    OperationLogWriter,
    RetryContext,
    WrittenOperationLog,
)
from ._context import StageContext, StageResult
from ._event import JiraStateChangeEvent
from ._handler import StageHandler
from ._state_machine import PipelineStateMachine

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PipelineOrchestrator:
    """Routes Jira state-change events to stage handlers. See ADR-0003.

    Lite scope:
    - One handler per status (no greenfield/cross-project dispatcher branches)
    - Polling delivery channel only (webhook deferred to Standard)
    - Retry budget read from operation_log count per ADR-0003
    """

    def __init__(
        self,
        *,
        state_machine: PipelineStateMachine,
        storage: StorageEngine,
        session_manager: SessionManager,
        operation_log_writer: OperationLogWriter,
        operation_log_reader: OperationLogReader,
        tool_registry: ToolRegistry,
        llm: LLMAdapter,
        compactor: Compactor,
        context_builder: ContextBuilder,
        config: Config,
        primary_project_key: str,
        dry_run: bool = False,
    ) -> None:
        self._state_machine = state_machine
        self._storage = storage
        self._session_manager = session_manager
        self._operation_log_writer = operation_log_writer
        self._operation_log_reader = operation_log_reader
        self._tools = tool_registry
        self._llm = llm
        self._compactor = compactor
        self._context_builder = context_builder
        self._config = config
        self._primary_project_key = primary_project_key
        self._dry_run = dry_run

    # -----------------------------------------------------------------
    # Entry points
    # -----------------------------------------------------------------

    async def react(self, event: JiraStateChangeEvent) -> None:
        """Process one Jira state-change event. Idempotent + safe to call
        twice with the same dedup_key.
        """
        if await self._already_processed(event):
            logger.debug(
                "pipeline.dedup_hit jira_key=%s to_status=%s",
                event.jira_key,
                event.to_status,
            )
            return

        await self._mark_received(event)

        handler = self._state_machine.handler_for(event.to_status)
        if handler is None:
            logger.info(
                "pipeline.no_handler jira_key=%s to_status=%s; passive wait.",
                event.jira_key,
                event.to_status,
            )
            await self._mark_processed(event)
            return

        try:
            ctx = await self._build_context(event, handler)
        except FatalError as exc:
            logger.exception("pipeline.context_build_failed")
            await self._mark_processed(event)
            raise

        # Retry budget enforcement.
        if ctx.retry_count >= handler.max_retries:
            await self._escalate(
                ctx,
                handler,
                reason=(
                    f"retry budget exhausted: {ctx.retry_count} prior attempts "
                    f"at stage {handler.stage_name!r}"
                ),
                started_at=time.monotonic(),
            )
            await self._mark_processed(event)
            return

        # Run the handler.
        started = time.monotonic()
        try:
            result = await handler.run(ctx)
        except FatalError as exc:
            await self._escalate(
                ctx, handler, reason=str(exc), started_at=started, cause=exc
            )
            await self._mark_processed(event)
            return
        except RetryableError as exc:
            await self._record_retryable_failure(
                ctx, handler, exc, started_at=started
            )
            await self._mark_processed(event)
            return

        await self._apply_result(ctx, handler, result, started_at=started)
        await self._mark_processed(event)

    async def manual_invoke(
        self,
        jira_key: str,
        *,
        force_status: str | None = None,
    ) -> None:
        """CLI escape hatch. Reads the ticket's current status and synthesizes
        a JiraStateChangeEvent so react() can do its thing.
        """
        ticket = await self._read_jira_ticket(jira_key)
        current_status = ticket.get("status", "TODO")
        if force_status:
            current_status = force_status
        event = JiraStateChangeEvent(
            jira_key=jira_key,
            from_status=None,
            to_status=current_status,
            observed_at=_utcnow(),
            delivery_channel="manual",
        )
        await self.react(event)

    # -----------------------------------------------------------------
    # Context construction
    # -----------------------------------------------------------------

    async def _build_context(
        self,
        event: JiraStateChangeEvent,
        handler: StageHandler,
    ) -> StageContext:
        ticket = await self._read_jira_ticket(event.jira_key)

        # Prior operation logs (already on disk + DB).
        prior_logs = await self._operation_log_reader.list_for_ticket(event.jira_key)

        # Retry count for THIS stage.
        retry_count = await self._operation_log_reader.count_for_stage(
            event.jira_key, handler.stage_name
        )

        # Workspace + mode (brownfield / greenfield) from analyze_repo_state.
        workspace = self._config.workspace_path
        mode = await self._infer_mode(workspace)

        # Session + Conversation.
        session = await self._session_manager.get_or_create_session(
            user_id=self._config.user_id,
            jira_key=event.jira_key,
            primary_project_key=self._primary_project_key,
            workspace_root=workspace,
            mode=mode,
            is_cross_project=False,
        )
        conversation = await self._session_manager.start_conversation(
            session_id=session.id,
            stage=handler.stage_name,
            revision=retry_count + 1,
            llm_provider=getattr(self._llm, "provider_name", None),
            llm_model=getattr(self._llm, "model_name", None),
        )

        # Repo facts (cheap; called once per Conversation).
        repo_facts = await self._compute_repo_facts()

        agent = Agent(
            session=session,
            conversation=conversation,
            llm=self._llm,
            tools=self._tools,
            context_builder=self._context_builder,
            compactor=self._compactor,
            session_manager=self._session_manager,
            config=self._config,
            repo_facts=repo_facts,
            conventions=None,
            loaded_skills=[],
            operation_log_path=None,
            dry_run=self._dry_run,
        )

        return StageContext(
            jira_key=event.jira_key,
            jira_ticket=ticket,
            prior_logs=prior_logs,
            retry_count=retry_count,
            session=session,
            conversation=conversation,
            agent=agent,
            workspace_root=workspace,
            mode=mode,
            is_cross_project=False,
            delivery_channel=event.delivery_channel,
        )

    async def _read_jira_ticket(self, jira_key: str) -> dict[str, Any]:
        result = await self._tools.call(
            "read_jira_ticket",
            {"jira_key": jira_key},
            self._tool_context(),
        )
        if not result.is_success:
            raise PipelineStateInconsistencyError(
                f"Could not read Jira ticket {jira_key!r}: {result.content}",
                jira_key=jira_key,
            )
        return _parse_tool_result(result.content) or {"status": "TODO"}

    async def _infer_mode(
        self, workspace: Path
    ) -> Literal["brownfield", "greenfield"]:
        result = await self._tools.call(
            "analyze_repo_state",
            {},
            self._tool_context(),
        )
        if not result.is_success:
            logger.warning(
                "pipeline.repo_state_unknown content=%s; defaulting to brownfield.",
                result.content[:200],
            )
            return "brownfield"
        parsed = _parse_tool_result(result.content) or {}
        mode = parsed.get("mode", "brownfield")
        return mode if mode in ("brownfield", "greenfield") else "brownfield"

    async def _compute_repo_facts(self) -> RepoFacts:
        result = await self._tools.call(
            "analyze_repo_state",
            {},
            self._tool_context(),
        )
        parsed = _parse_tool_result(result.content) or {}
        langs = list((parsed.get("languages") or {}).keys()) if isinstance(parsed.get("languages"), dict) else []
        return RepoFacts(
            languages=langs,
            frameworks=[],
            top_level_modules=[],
            has_tests=bool(parsed.get("has_tests")),
            has_ci=bool(parsed.get("has_ci")),
        )

    # -----------------------------------------------------------------
    # Result application
    # -----------------------------------------------------------------

    async def _apply_result(
        self,
        ctx: StageContext,
        handler: StageHandler,
        result: StageResult,
        *,
        started_at: float,
    ) -> None:
        duration = time.monotonic() - started_at
        body = result.body or _default_body(
            "Stage completed without an explicit body section."
        )
        retry_ctx = self._retry_context_for(ctx)

        written = await self._operation_log_writer.write(
            jira_key=ctx.jira_key,
            stage=handler.stage_name,
            status=result.outcome,
            agent="direct",
            skill_invoked=None,
            duration_seconds=duration,
            inputs={
                "from_status": ctx.jira_ticket.get("status"),
                "delivery_channel": ctx.delivery_channel,
            },
            outputs=dict(result.artifacts),
            body=body,
            retry_context=retry_ctx,
            escalation_reason=result.escalation_reason,
        )

        # Transition Jira based on outcome.
        next_status: str | None = None
        if result.outcome == "completed":
            next_status = (
                result.next_status_override or handler.exit_status_on_success
            )
        elif result.outcome == "failed":
            next_status = handler.exit_status_on_failure

        if next_status:
            await self._transition_jira(ctx.jira_key, next_status)

        # Post Jira comment summarizing the outcome.
        await self._post_jira_comment(
            ctx.jira_key,
            self._comment_for(result, written),
        )

        # Close out the Conversation.
        await self._session_manager.end_conversation(
            ctx.conversation.id,
            status="completed" if result.outcome == "completed" else "failed",
            operation_log_id=written.db_row_id,
        )

    async def _record_retryable_failure(
        self,
        ctx: StageContext,
        handler: StageHandler,
        exc: RetryableError,
        *,
        started_at: float,
    ) -> None:
        duration = time.monotonic() - started_at
        body = _default_body(
            f"Stage {handler.stage_name!r} failed retryably: {exc}"
        )
        await self._operation_log_writer.write(
            jira_key=ctx.jira_key,
            stage=handler.stage_name,
            status="failed",
            agent="direct",
            skill_invoked=None,
            duration_seconds=duration,
            inputs={
                "from_status": ctx.jira_ticket.get("status"),
                "delivery_channel": ctx.delivery_channel,
            },
            outputs={},
            body=body,
            retry_context=self._retry_context_for(ctx),
        )
        await self._session_manager.end_conversation(
            ctx.conversation.id, status="failed"
        )

    async def _escalate(
        self,
        ctx: StageContext,
        handler: StageHandler,
        *,
        reason: str,
        started_at: float,
        cause: AgentError | None = None,
    ) -> None:
        duration = time.monotonic() - started_at
        body = _default_body(
            f"Stage {handler.stage_name!r} escalated: {reason}"
        )
        await self._operation_log_writer.write(
            jira_key=ctx.jira_key,
            stage=handler.stage_name,
            status="escalated",
            agent="direct",
            skill_invoked=None,
            duration_seconds=duration,
            inputs={
                "from_status": ctx.jira_ticket.get("status"),
                "delivery_channel": ctx.delivery_channel,
            },
            outputs={},
            body=body,
            retry_context=self._retry_context_for(ctx),
            escalation_reason=reason,
        )
        await self._post_jira_comment(
            ctx.jira_key,
            f"[ai-coding] Stage {handler.stage_name!r} escalated.\n\n"
            f"Reason: {reason}\n\n"
            "Remove the `escalated` label to re-enable agent reactions.",
        )
        await self._session_manager.end_conversation(
            ctx.conversation.id, status="escalated"
        )
        # Escalation is a terminal state for this ticket, not an exception
        # for the orchestrator's caller. The Jira comment + operation log
        # carry the signal; the reactor's `escalated` label check (Standard
        # profile) keeps subsequent events from re-entering.

    def _retry_context_for(self, ctx: StageContext) -> RetryContext | None:
        if ctx.retry_count == 0:
            return None
        return RetryContext(
            previous_attempts=[
                f"v{log.revision}: {log.body_summary[:140]}"
                for log in ctx.prior_logs
                if log.stage == ctx.conversation.stage
            ],
            failure_signal=None,
        )

    def _comment_for(
        self,
        result: StageResult,
        written: WrittenOperationLog,
    ) -> str:
        artifact_lines = [
            f"- {key}: {value}" for key, value in sorted(result.artifacts.items())
        ]
        return (
            f"[ai-coding] Stage outcome: **{result.outcome}**.\n\n"
            f"Operation log: `{written.relative_path}`\n\n"
            f"{result.summary}\n\n"
            + ("Artifacts:\n" + "\n".join(artifact_lines) if artifact_lines else "")
        )

    # -----------------------------------------------------------------
    # Jira write helpers (orchestrator-only tools)
    # -----------------------------------------------------------------

    async def _transition_jira(self, jira_key: str, to_status: str) -> None:
        result = await self._tools.call(
            "transition_jira_status",
            {"jira_key": jira_key, "to_status": to_status},
            self._tool_context(),
        )
        if not result.is_success:
            content = result.content
            if "permission" in content.lower() or "forbidden" in content.lower():
                raise JiraTransitionForbiddenError(
                    f"Transition {to_status!r} forbidden on {jira_key!r}: {content}",
                    ticket_key=jira_key,
                    to_status=to_status,
                )
            logger.warning(
                "pipeline.transition_failed jira_key=%s to_status=%s detail=%s",
                jira_key,
                to_status,
                content[:200],
            )

    async def _post_jira_comment(self, jira_key: str, body: str) -> None:
        result = await self._tools.call(
            "add_jira_comment",
            {"jira_key": jira_key, "body": body},
            self._tool_context(),
        )
        if not result.is_success:
            logger.warning(
                "pipeline.comment_post_failed jira_key=%s detail=%s",
                jira_key,
                result.content[:200],
            )

    # -----------------------------------------------------------------
    # Dedup table
    # -----------------------------------------------------------------

    async def _already_processed(self, event: JiraStateChangeEvent) -> bool:
        async with self._storage.session() as s:
            row = await s.get(ProcessedJiraEvent, event.dedup_key)
            return row is not None and row.processed_at is not None

    async def _mark_received(self, event: JiraStateChangeEvent) -> None:
        async with self._storage.session() as s:
            existing = await s.get(ProcessedJiraEvent, event.dedup_key)
            if existing is not None:
                return
            s.add(
                ProcessedJiraEvent(
                    dedup_key=event.dedup_key,
                    jira_key=event.jira_key,
                    to_status=event.to_status,
                    delivery_channel=event.delivery_channel,
                    received_at=event.observed_at,
                )
            )
            await s.commit()

    async def _mark_processed(self, event: JiraStateChangeEvent) -> None:
        async with self._storage.session() as s:
            row = await s.get(ProcessedJiraEvent, event.dedup_key)
            if row is None:
                return
            row.processed_at = _utcnow()
            await s.commit()

    # -----------------------------------------------------------------
    # ToolContext factory
    # -----------------------------------------------------------------

    def _tool_context(self) -> ToolContext:
        return ToolContext(
            config=self._config,
            session_id=None,
            conversation_id=None,
            invocation_id=uuid4().hex,
            dry_run=self._dry_run,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_tool_result(content: str) -> dict[str, Any] | None:
    """Tool results are typically JSON-encoded; the orchestrator calls
    read-only tools directly so the content is the JSON dump or an [ERROR]
    string. Returns None on parse failure.
    """
    if content.startswith(("[ERROR]", "[TIMEOUT]", "[REFUSED]")):
        return None
    try:
        return json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return None


def _default_body(summary: str) -> OperationLogBody:
    """Used when a handler did not provide a body (e.g., retryable failure)."""
    return OperationLogBody(
        what_was_done=summary,
        impact="_(none)_",
        what_i_could_not_do=summary,
        engineering_decisions="_(none)_",
        next_step="Re-run the stage on the next Jira event.",
    )
