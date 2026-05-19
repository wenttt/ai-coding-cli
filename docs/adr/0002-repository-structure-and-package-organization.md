# ADR-0002: Repository Structure and Package Organization

## Status

Proposed

## Date

2026-05-19

## Context

Define the repository layout, Python package structure, dependency management, and platform support for `ai-coding-cli`.

## Decision

### Repository top-level layout

```
ai-coding-cli/
├── README.md
├── LICENSE
├── pyproject.toml              # project metadata + dependencies
├── uv.lock                     # locked dependency versions
├── docker-compose.yml          # local PostgreSQL + Neo4j
├── .env.example
├── .gitignore
├── .editorconfig
├── .python-version             # Python version pin
├── docs/
│   ├── adr/                    # Architecture Decision Records
│   ├── architecture/           # diagrams + module specs
│   ├── api/                    # generated API reference
│   └── jira/                   # reference workflow.json, screens.json, setup-checklist.md
├── migrations/
│   ├── postgres/               # Alembic migrations
│   └── neo4j/                  # Cypher migration scripts (versioned)
├── scripts/
│   ├── dev/                    # local dev helpers
│   └── ops/                    # operational scripts (backup, etc.)
├── examples/                   # walkthroughs + sample tickets
├── src/
│   └── ai_coding_cli/          # single mono-package
└── tests/
    ├── unit/                   # mirrors src/ai_coding_cli/ structure
    ├── integration/            # multi-module flows
    └── e2e/                    # full pipeline with mock LLM
```

### Package internal layout (src/ai_coding_cli/)

Two-layer organization. Foundation is generic Agent infrastructure. Application is the AI Coding Workflow business pipeline.

```
src/ai_coding_cli/
├── __init__.py                 # public API: re-exports Agent, AgentResult, Config
├── cli.py                      # Typer entry point
├── daemon.py                   # daemon process: HTTP server + Jira reaction loop
├── errors.py                   # error taxonomy (Retryable / Fatal / UserAbort)
│
├── foundation/                 # generic Agent runtime
│   ├── __init__.py
│   ├── session/                # Session + Conversation
│   ├── agent/                  # Agent Core + ReAct loop
│   ├── context/                # three-tier Context Layer
│   ├── compactor/              # MicroCompact + AutoCompact
│   ├── memory/                 # four-layer Memory + governance
│   ├── retrieval/              # RAG Engine (vector + graph + hybrid)
│   ├── skills/                 # Skill Loader + on-demand injection
│   ├── tools/                  # Tool Registry + native tool registration
│   │   ├── jira.py
│   │   ├── github.py
│   │   ├── git.py
│   │   ├── repo.py
│   │   ├── tests.py
│   │   └── ...
│   ├── guardrail/              # Input + Output + Action guardrails
│   ├── llm/                    # LLM Adapter (OpenAI-compat, Mock)
│   ├── storage/                # PostgreSQL + Neo4j clients
│   │   ├── postgres.py
│   │   ├── neo4j.py
│   │   └── sync.py             # outbox + CDC between PG and Neo4j
│   ├── observability/          # event bus, structured logging, metrics
│   └── config/                 # Pydantic-Settings models
│
├── application/                # AI Coding Workflow pipeline
│   ├── __init__.py
│   ├── pipeline/
│   │   ├── stages/             # Stage 1-6 handlers
│   │   ├── state_machine.py    # mapping Jira status → stage handler
│   │   └── orchestrator.py     # top-level pipeline coordinator
│   ├── jira_reaction/          # webhook + polling dispatcher
│   ├── operation_log/          # schema, read, write, query
│   ├── retry/                  # 3-strike counter + escalation
│   ├── templates/              # design doc templates (brownfield, greenfield, cross_project)
│   ├── routing/                # multi-project + cross-project
│   └── workflow_spec/          # generators for docs/jira/ artifacts
│
└── web/                        # local Web Dashboard
    ├── __init__.py
    ├── app.py                  # FastAPI app
    ├── routes/                 # read-only endpoints
    └── static/                 # frontend bundle (HTMX or minimal React, decided in ADR-0026)
```

### Import rules

- `foundation/` does NOT import from `application/` or `web/`.
- `application/` imports from `foundation/` only through public interfaces (Protocols defined in `foundation/*/protocols.py`).
- `web/` imports from `application/` and `foundation/` (read-only).
- Cyclic imports are rejected by `ruff --select=I` in CI.

### Tests directory mirrors source

```
tests/
├── unit/
│   ├── foundation/
│   │   ├── agent/
│   │   ├── context/
│   │   └── ...
│   └── application/
│       ├── pipeline/
│       └── ...
├── integration/
│   ├── llm_provider/
│   ├── storage/
│   └── pipeline_stages/
└── e2e/
    └── full_pipeline_with_mock_llm.py
```

Coverage targets (from ADR-0001): ≥ 80% on `foundation/`, ≥ 60% on `application/`.

### Public API surface

`src/ai_coding_cli/__init__.py` re-exports the small set of types intended for embedding use (e.g. a future hosted server using the same core):

```python
from .foundation.agent import Agent, AgentResult
from .foundation.config import Config
from .foundation.session import Session, Conversation
```

Everything else is internal.

### CLI entry point

`pyproject.toml`:

```toml
[project.scripts]
ai-coding = "ai_coding_cli.cli:app"
```

Subcommands:

```
ai-coding chat <message>
ai-coding pipeline status <KEY>
ai-coding tickets list
ai-coding skills list
ai-coding daemon start | stop | status
ai-coding web
ai-coding migrate [up | down | status]
ai-coding version
```

### Dependency management

- **`uv`** as the package manager (lockfile: `uv.lock`).
- `pyproject.toml` is the single source of dependency truth.
- Production dependencies in `[project.dependencies]`. Development tooling in `[project.optional-dependencies.dev]`. Documentation tooling in `[project.optional-dependencies.docs]`.
- Pin Python with `requires-python = ">=3.11,<3.14"`.

### Platform support

Windows, Linux, macOS as first-class targets in v0.2.

- File paths via `pathlib.Path` only. No raw `os.path` joins.
- Subprocess invocations pass argument lists, not shell strings.
- Newline handling defers to `pathlib.Path.read_text(encoding="utf-8")`.
- Daemon lifecycle is platform-specific (Windows Service / launchd / systemd); the abstraction lives in `foundation/daemon_supervisor/` (covered in ADR-0027).

### Tooling

- Lint + format: `ruff` (replaces black, isort, flake8).
- Type check: `mypy --strict` on `foundation/`; `mypy` on `application/`.
- Test runner: `pytest` with `pytest-asyncio` and `pytest-cov`.
- Pre-commit hooks via `.pre-commit-config.yaml` (covered in implementation; not a separate ADR).

### Documentation generators

- ADRs: plain Markdown in `docs/adr/`.
- Architecture diagrams: Mermaid in Markdown or PNG exports in `docs/architecture/`.
- API reference: `mkdocs` with `mkdocstrings` plugin, output to `docs/api/`.

## Consequences

- Cleanly separates generic Agent infrastructure from this specific business pipeline. A future application can reuse `foundation/` without rewriting it.
- Tests mirroring source structure makes navigation predictable.
- `uv` over `pip + venv` shrinks install time on corporate Windows machines (where venv builds are notably slow under AV scanning).
- Mono-package keeps `import ai_coding_cli.foundation.agent` as the canonical path; no per-subpackage namespace surgery later.
- Cross-platform path / subprocess discipline imposes a small ongoing cost but eliminates a class of Windows-specific bugs.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Need for `internal/` shared package between `application/` modules | Defer; revisit if pipeline grows multiple applications |
| Q2 | Documentation hosting (GitHub Pages, internal company hosting) | Defer; affects whether `docs/api/` is generated in CI |
| Q3 | Frontend bundling toolchain (vite / esbuild / no-build) | ADR-0026 (Dashboard) |

## References

- ADR-0001 System Overview (Foundation + Application layering)
- ADR-0027 Daemon lifecycle (platform-specific daemon supervisor details)

## Reviewers

- [ ] Taven
