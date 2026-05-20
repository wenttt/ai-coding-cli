"""OperationLogWriter + Reader tests. See ADR-0005."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import AsyncIterator

import pytest
import yaml

from ai_coding_cli.application.operation_log import (
    OperationLogBody,
    OperationLogReader,
    OperationLogWriter,
)
from ai_coding_cli.foundation.errors import (
    OperationLogIntegrityError,
    OperationLogValidationError,
)
from ai_coding_cli.foundation.storage import BASE, StorageEngine


@pytest.fixture
async def writer_reader(tmp_path: Path) -> AsyncIterator[tuple[OperationLogWriter, OperationLogReader, Path]]:
    engine = StorageEngine(tmp_path / "oplog.db")
    async with engine._async_engine.begin() as conn:  # noqa: SLF001
        await conn.run_sync(BASE.metadata.create_all)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    yield (
        OperationLogWriter(engine, workspace),
        OperationLogReader(engine, workspace),
        workspace,
    )
    await engine.close()


def _body() -> OperationLogBody:
    return OperationLogBody(
        what_was_done="- Read ticket PROJ-1.\n- Drafted design.",
        impact="Design Issue PROJ/repo#42 opened.",
        what_i_could_not_do="_(none)_",
        engineering_decisions="- Treated ticket as brownfield.",
        next_step="Reviewers should evaluate the Design Issue.",
    )


async def test_write_creates_file_and_db_row(writer_reader) -> None:
    writer, reader, workspace = writer_reader
    written = await writer.write(
        jira_key="PROJ-1",
        stage="design",
        status="completed",
        agent="direct",
        skill_invoked=None,
        duration_seconds=12.5,
        inputs={"from_status": "DESIGN_DRAFTING"},
        outputs={"design_issue_url": "https://github.com/org/repo/issues/42"},
        body=_body(),
    )
    assert written.sequence_number == 1
    assert written.revision == 1
    assert written.file_path.exists()
    assert written.relative_path == "docs/operations/PROJ-1/01-design-v1.md"

    # Frontmatter roundtrips
    text = written.file_path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    end = text.find("\n---\n", 4)
    fm = yaml.safe_load(text[4:end])
    assert fm["jira_key"] == "PROJ-1"
    assert fm["stage"] == "design"
    assert fm["status"] == "completed"

    # SHA matches file
    assert (
        hashlib.sha256(text.encode("utf-8")).hexdigest() == written.sha256
    )

    # DB row exists
    summaries = await reader.list_for_ticket("PROJ-1")
    assert len(summaries) == 1
    assert summaries[0].sequence_number == 1
    assert summaries[0].revision == 1


async def test_retry_bumps_revision_same_sequence(writer_reader) -> None:
    writer, reader, _ = writer_reader
    first = await writer.write(
        jira_key="PROJ-2",
        stage="design",
        status="failed",
        agent="direct",
        skill_invoked=None,
        duration_seconds=2.0,
        inputs={},
        outputs={},
        body=_body(),
    )
    second = await writer.write(
        jira_key="PROJ-2",
        stage="design",
        status="completed",
        agent="direct",
        skill_invoked=None,
        duration_seconds=3.0,
        inputs={},
        outputs={},
        body=_body(),
    )
    assert first.sequence_number == 1
    assert first.revision == 1
    assert second.sequence_number == 1
    assert second.revision == 2

    count = await reader.count_for_stage("PROJ-2", "design")
    assert count == 2  # both excluded only if escalated; both are completed/failed


async def test_new_stage_increments_sequence(writer_reader) -> None:
    writer, _, _ = writer_reader
    await writer.write(
        jira_key="PROJ-3",
        stage="design",
        status="completed",
        agent="direct",
        skill_invoked=None,
        duration_seconds=1.0,
        inputs={},
        outputs={},
        body=_body(),
    )
    next_stage = await writer.write(
        jira_key="PROJ-3",
        stage="implement",
        status="completed",
        agent="direct",
        skill_invoked=None,
        duration_seconds=1.0,
        inputs={},
        outputs={},
        body=_body(),
    )
    assert next_stage.sequence_number == 2
    assert next_stage.revision == 1


async def test_escalation_uses_ESCALATED_suffix(writer_reader) -> None:
    writer, _, workspace = writer_reader
    written = await writer.write(
        jira_key="PROJ-4",
        stage="design",
        status="escalated",
        agent="direct",
        skill_invoked=None,
        duration_seconds=1.0,
        inputs={},
        outputs={},
        body=_body(),
        escalation_reason="retry budget exhausted",
    )
    assert written.file_path.name == "01-design-ESCALATED.md"


async def test_empty_body_section_rejected(writer_reader) -> None:
    writer, _, _ = writer_reader
    bad_body = OperationLogBody(
        what_was_done="",
        impact="x",
        what_i_could_not_do="x",
        engineering_decisions="x",
        next_step="x",
    )
    with pytest.raises(OperationLogValidationError):
        await writer.write(
            jira_key="PROJ-5",
            stage="design",
            status="completed",
            agent="direct",
            skill_invoked=None,
            duration_seconds=1.0,
            inputs={},
            outputs={},
            body=bad_body,
        )


async def test_read_full_detects_tampered_file(writer_reader) -> None:
    writer, reader, _ = writer_reader
    written = await writer.write(
        jira_key="PROJ-6",
        stage="design",
        status="completed",
        agent="direct",
        skill_invoked=None,
        duration_seconds=1.0,
        inputs={},
        outputs={},
        body=_body(),
    )
    # Tamper the file.
    text = written.file_path.read_text(encoding="utf-8")
    written.file_path.write_text(text + "\n[malicious append]", encoding="utf-8")

    with pytest.raises(OperationLogIntegrityError):
        await reader.read_full(written.db_row_id)


async def test_read_full_roundtrips_body_sections(writer_reader) -> None:
    writer, reader, _ = writer_reader
    written = await writer.write(
        jira_key="PROJ-7",
        stage="design",
        status="completed",
        agent="direct",
        skill_invoked=None,
        duration_seconds=1.0,
        inputs={"from_status": "DESIGN_DRAFTING"},
        outputs={"design_issue_url": "https://example.com/issues/1"},
        body=_body(),
    )
    full = await reader.read_full(written.db_row_id)
    assert full.frontmatter.jira_key == "PROJ-7"
    assert full.body.what_was_done.startswith("- Read ticket")
    assert full.body.next_step.startswith("Reviewers")
