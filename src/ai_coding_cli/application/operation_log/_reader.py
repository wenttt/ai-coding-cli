"""OperationLogReader: query + parse operation logs. See ADR-0005."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml
from sqlalchemy import func as sa_func
from sqlalchemy import select

from ...foundation.errors import OperationLogIntegrityError
from ...foundation.storage import OperationLogIndex, StorageEngine
from ._schema import OperationLogBody, OperationLogFrontmatter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OperationLogSummary:
    """One row from operation_logs_index, plus the file path for read-back."""

    db_row_id: int
    jira_key: str
    stage: str
    revision: int
    sequence_number: int
    status: str
    timestamp: datetime
    duration_seconds: float
    file_path: str          # relative to workspace_root
    file_sha256: str
    body_summary: str


@dataclass(frozen=True)
class OperationLogFull:
    """Fully parsed log: frontmatter + 5 body sections."""

    frontmatter: OperationLogFrontmatter
    body: OperationLogBody


class OperationLogReader:
    """Read operation logs by ticket / stage. SQLite-backed for metadata,
    filesystem for full body. Tamper detection via SHA-256.
    """

    def __init__(self, storage: StorageEngine, workspace_root: Path) -> None:
        self._storage = storage
        self._workspace_root = workspace_root

    async def list_for_ticket(self, jira_key: str) -> list[OperationLogSummary]:
        async with self._storage.session() as s:
            result = await s.execute(
                select(OperationLogIndex)
                .where(OperationLogIndex.jira_key == jira_key)
                .order_by(
                    OperationLogIndex.sequence_number.asc(),
                    OperationLogIndex.revision.asc(),
                )
            )
            return [_to_summary(r) for r in result.scalars().all()]

    async def count_for_stage(self, jira_key: str, stage: str) -> int:
        """Number of attempts at this stage on this ticket. Excludes escalated."""
        async with self._storage.session() as s:
            result = await s.execute(
                select(sa_func.count())
                .select_from(OperationLogIndex)
                .where(
                    OperationLogIndex.jira_key == jira_key,
                    OperationLogIndex.stage == stage,
                    OperationLogIndex.status != "escalated",
                )
            )
            return int(result.scalar_one())

    async def latest_for_stage(
        self, jira_key: str, stage: str
    ) -> OperationLogSummary | None:
        async with self._storage.session() as s:
            result = await s.execute(
                select(OperationLogIndex)
                .where(
                    OperationLogIndex.jira_key == jira_key,
                    OperationLogIndex.stage == stage,
                )
                .order_by(OperationLogIndex.revision.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return _to_summary(row) if row else None

    async def read_full(self, log_id: int) -> OperationLogFull:
        async with self._storage.session() as s:
            row = await s.get(OperationLogIndex, log_id)
        if row is None:
            raise FileNotFoundError(f"No operation log with id {log_id}")

        abs_path = self._workspace_root / row.file_path
        text = abs_path.read_text(encoding="utf-8")

        # Tamper detection (best-effort; warn but do not block).
        actual_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if actual_sha != row.file_sha256:
            logger.warning(
                "operation_log.integrity_mismatch path=%s expected=%s actual=%s",
                row.file_path,
                row.file_sha256,
                actual_sha,
            )
            # Surface as a typed error the orchestrator can catch.
            raise OperationLogIntegrityError(
                f"SHA-256 mismatch on {row.file_path}.",
                file_path=row.file_path,
            )

        frontmatter, body = _parse_markdown(text)
        return OperationLogFull(frontmatter=frontmatter, body=body)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


_SECTION_TITLES = (
    "What was done",
    "Impact",
    "What I could not do",
    "Engineering decisions",
    "Next step",
)


def _parse_markdown(text: str) -> tuple[OperationLogFrontmatter, OperationLogBody]:
    if not text.startswith("---\n"):
        raise ValueError("Operation log missing YAML frontmatter")
    end_marker = text.find("\n---\n", 4)
    if end_marker == -1:
        raise ValueError("Operation log frontmatter not closed")

    yaml_block = text[4:end_marker]
    body_text = text[end_marker + 5 :].lstrip()
    fm_data = yaml.safe_load(yaml_block)
    frontmatter = OperationLogFrontmatter.model_validate(fm_data)

    sections: dict[str, str] = {}
    current_title: str | None = None
    buffer: list[str] = []
    for line in body_text.splitlines():
        if line.startswith("## "):
            if current_title:
                sections[current_title] = "\n".join(buffer).strip()
            current_title = line[3:].strip()
            buffer = []
        else:
            buffer.append(line)
    if current_title:
        sections[current_title] = "\n".join(buffer).strip()

    missing = [t for t in _SECTION_TITLES if t not in sections]
    if missing:
        raise ValueError(
            f"Operation log body missing sections: {missing}"
        )

    body = OperationLogBody(
        what_was_done=sections["What was done"],
        impact=sections["Impact"],
        what_i_could_not_do=sections["What I could not do"],
        engineering_decisions=sections["Engineering decisions"],
        next_step=sections["Next step"],
    )
    return frontmatter, body


def _to_summary(row: OperationLogIndex) -> OperationLogSummary:
    return OperationLogSummary(
        db_row_id=row.id,
        jira_key=row.jira_key,
        stage=row.stage,
        revision=row.revision,
        sequence_number=row.sequence_number,
        status=row.status,
        timestamp=row.timestamp,
        duration_seconds=row.duration_seconds,
        file_path=row.file_path,
        file_sha256=row.file_sha256,
        body_summary=row.body_summary,
    )
