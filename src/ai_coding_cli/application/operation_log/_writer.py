"""OperationLogWriter: atomic file + DB row writes. See ADR-0005."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from sqlalchemy import select

from ...foundation.errors import (
    OperationLogConflictError,
    OperationLogValidationError,
)
from ...foundation.storage import OperationLogIndex, StorageEngine
from ._schema import (
    OperationLogBody,
    OperationLogFrontmatter,
    RetryContext,
    WrittenOperationLog,
)

# Filenames: NN-stage-slug-v{N}.md OR NN-stage-slug-ESCALATED.md
_FILENAME_RE = re.compile(
    r"^(?P<seq>\d{2})-(?P<stage>[a-z][a-z\-]*[a-z])-(?:v(?P<rev>\d+)|ESCALATED)\.md$"
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OperationLogWriter:
    """Writes operation logs to disk + SQLite atomically.

    File layout: `{workspace_root}/docs/operations/{JIRA_KEY}/{NN}-{stage}-v{N}.md`.
    DB row in `operation_logs_index` with (jira_key, sequence_number, stage,
    revision) UNIQUE constraint.
    """

    def __init__(self, storage: StorageEngine, workspace_root: Path) -> None:
        self._storage = storage
        self._workspace_root = workspace_root

    async def write(
        self,
        *,
        jira_key: str,
        stage: str,
        status: Literal["completed", "failed", "escalated"],
        agent: str,
        skill_invoked: str | None,
        duration_seconds: float,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
        body: OperationLogBody,
        retry_context: RetryContext | None = None,
        escalation_reason: str | None = None,
    ) -> WrittenOperationLog:
        """Compute the next (sequence, revision), write the file + DB row.

        Raises:
            OperationLogValidationError: when the body has empty sections.
            OperationLogConflictError: on a UNIQUE constraint race (multi-daemon).
        """
        # Validate body.
        try:
            body.validate_nonempty()
        except ValueError as exc:
            raise OperationLogValidationError(
                str(exc),
                missing_section=_extract_section_name(str(exc)),
            ) from exc

        # Determine sequence + revision by looking at the DB index.
        sequence_number, revision = await self._next_sequence_and_revision(
            jira_key=jira_key,
            stage=stage,
            status=status,
        )

        # Construct frontmatter (this also validates stage/agent/etc).
        frontmatter = OperationLogFrontmatter(
            jira_key=jira_key,
            stage=stage,
            revision=revision,
            status=status,
            skill_invoked=skill_invoked,
            agent=agent,
            timestamp=_utcnow(),
            duration_seconds=duration_seconds,
            inputs=inputs,
            outputs=outputs,
            retry_context=retry_context,
            escalation_reason=escalation_reason,
        )

        # Write file (atomic rename) + compute SHA.
        target_path = self._target_path(
            jira_key=jira_key,
            stage=stage,
            sequence_number=sequence_number,
            revision=revision,
            status=status,
        )
        rendered = _render_markdown(frontmatter, body)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        sha256 = _atomic_write(target_path, rendered)

        # Insert DB row.
        relative_path = str(target_path.relative_to(self._workspace_root))
        body_summary = body.what_was_done.strip()[:500]
        try:
            row_id = await self._insert_index_row(
                frontmatter=frontmatter,
                sequence_number=sequence_number,
                file_path=relative_path,
                sha256=sha256,
                body_summary=body_summary,
            )
        except Exception as exc:  # noqa: BLE001
            # Best-effort cleanup: remove the orphan file on insert failure
            # so the next attempt sees clean state.
            try:
                target_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise OperationLogConflictError(
                f"Failed to insert operation log row for {jira_key}#{sequence_number}v{revision}: {exc}",
                jira_key=jira_key,
                sequence_number=sequence_number,
                revision=revision,
            ) from exc

        return WrittenOperationLog(
            file_path=target_path,
            relative_path=relative_path,
            sequence_number=sequence_number,
            revision=revision,
            sha256=sha256,
            db_row_id=row_id,
        )

    # -----------------------------------------------------------------
    # Sequence + revision computation
    # -----------------------------------------------------------------

    async def _next_sequence_and_revision(
        self,
        *,
        jira_key: str,
        stage: str,
        status: str,
    ) -> tuple[int, int]:
        """Per ADR-0005 §Sequence number rules.

        - First entry for this stage on this ticket: seq = max(any) + 1, rev = 1
        - Retry of same stage: seq = unchanged, rev = max(rev) + 1
        - Escalation: seq = unchanged, rev = max(rev) + 1
        """
        async with self._storage.session() as s:
            # Highest sequence_number for any stage on this ticket.
            result = await s.execute(
                select(OperationLogIndex.sequence_number, OperationLogIndex.stage, OperationLogIndex.revision)
                .where(OperationLogIndex.jira_key == jira_key)
            )
            rows = result.all()

        if not rows:
            return 1, 1

        per_stage: dict[str, list[tuple[int, int]]] = {}
        max_seq = 0
        for seq, st, rev in rows:
            per_stage.setdefault(st, []).append((seq, rev))
            max_seq = max(max_seq, seq)

        if stage in per_stage:
            existing_pairs = per_stage[stage]
            seq = existing_pairs[0][0]  # all share the same sequence
            max_rev = max(rev for _, rev in existing_pairs)
            return seq, max_rev + 1

        # New stage for this ticket.
        return max_seq + 1, 1

    # -----------------------------------------------------------------
    # File path computation
    # -----------------------------------------------------------------

    def _target_path(
        self,
        *,
        jira_key: str,
        stage: str,
        sequence_number: int,
        revision: int,
        status: str,
    ) -> Path:
        suffix = "ESCALATED" if status == "escalated" else f"v{revision}"
        filename = f"{sequence_number:02d}-{stage}-{suffix}.md"
        return (
            self._workspace_root
            / "docs"
            / "operations"
            / jira_key
            / filename
        )

    # -----------------------------------------------------------------
    # DB insert
    # -----------------------------------------------------------------

    async def _insert_index_row(
        self,
        *,
        frontmatter: OperationLogFrontmatter,
        sequence_number: int,
        file_path: str,
        sha256: str,
        body_summary: str,
    ) -> int:
        async with self._storage.session() as s:
            row = OperationLogIndex(
                jira_key=frontmatter.jira_key,
                session_id=None,
                stage=frontmatter.stage,
                revision=frontmatter.revision,
                sequence_number=sequence_number,
                status=frontmatter.status,
                skill_invoked=frontmatter.skill_invoked,
                agent=frontmatter.agent,
                timestamp=frontmatter.timestamp,
                duration_seconds=frontmatter.duration_seconds,
                inputs_json=json.dumps(
                    frontmatter.inputs, default=str, ensure_ascii=False
                ),
                outputs_json=json.dumps(
                    frontmatter.outputs, default=str, ensure_ascii=False
                ),
                retry_context_json=(
                    None
                    if frontmatter.retry_context is None
                    else frontmatter.retry_context.model_dump_json()
                ),
                escalation_reason=frontmatter.escalation_reason,
                file_path=file_path,
                file_sha256=sha256,
                body_summary=body_summary,
                body_embedding=None,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row.id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_markdown(
    frontmatter: OperationLogFrontmatter,
    body: OperationLogBody,
) -> str:
    """Render the file as YAML frontmatter + Markdown body."""
    fm_dict = json.loads(frontmatter.model_dump_json())
    yaml_block = yaml.safe_dump(fm_dict, sort_keys=False, allow_unicode=True).rstrip()
    return f"---\n{yaml_block}\n---\n\n{body.to_markdown()}\n"


def _atomic_write(target: Path, content: str) -> str:
    """Write content to target via a temp file + rename. Returns SHA-256 hex."""
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return digest


def _extract_section_name(error_message: str) -> str:
    """Best-effort: pull the section name out of `validate_nonempty` errors."""
    match = re.search(r"section '([^']+)'", error_message)
    return match.group(1) if match else "unknown"
