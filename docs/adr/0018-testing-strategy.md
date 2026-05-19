# ADR-0018: Testing Strategy

## Status

Accepted

## Date

2026-05-19

## Context

Specify the testing approach: layers, mocking, fixtures, CI gates, replay-based regression, performance smoke tests.

## Decision

### Test pyramid

```
                  ┌──────────────────┐
                  │   E2E (few)      │   real PG/Neo4j, mock LLM, full pipeline
                  └──────────────────┘
              ┌──────────────────────────┐
              │    Integration (many)    │   subsystem boundaries; some real DB
              └──────────────────────────┘
          ┌────────────────────────────────────┐
          │           Unit (many)              │   one module, all collaborators mocked
          └────────────────────────────────────┘
```

Counts indicative; not enforced. Unit tests fast and numerous; E2E tests slow and few.

### Layer 1: Unit tests

Scope: one module / one class. All collaborators mocked or stubbed.

Location: `tests/unit/` mirrors `src/ai_coding_cli/` structure.

Tooling: `pytest`, `pytest-asyncio`, `pytest-mock`, `pytest-cov`.

Conventions:

- One test file per module: `src/ai_coding_cli/foundation/agent/core.py` → `tests/unit/foundation/agent/test_core.py`
- Test class per public class; test function per behavior
- Fixtures local to file unless reused → promoted to `tests/conftest.py`
- No I/O, no real network, no real DB

Coverage gate: **≥ 80% line coverage on `foundation/`**; ≥ 60% on `application/`. Enforced by CI (`pytest-cov --cov-fail-under`).

### Layer 2: Integration tests

Scope: a subsystem talking to its real dependencies (DB, file system) OR a chain of two-three subsystems.

Location: `tests/integration/`.

Categories:

| Category | What's real | What's mocked |
|---|---|---|
| `storage/` | PostgreSQL + Neo4j | nothing (these tests verify DB layer behavior) |
| `llm_provider/` | nothing — uses `MockLLMAdapter` | LLM |
| `pipeline_stages/` | local file system, in-process orchestrator | LLM (Mock), GitHub (Mock), Jira (Mock) |
| `webhook/` | local HTTP server | LLM (Mock), Jira (Mock) |
| `cli/` | full CLI invocation via subprocess | LLM (Mock); writes to a temp workspace |

Real PostgreSQL + Neo4j run as Docker containers via `pytest-docker`. The first run is slow (image pulls); subsequent runs reuse the containers via session-scoped fixtures.

For developers without Docker, integration tests requiring DB skip with a clear message; the corresponding CI matrix entry is mandatory.

### Layer 3: E2E tests

Scope: full pipeline from a synthetic Jira state change through to operation log + Jira transition.

Location: `tests/e2e/`.

A single E2E test exercises:

```python
async def test_full_design_stage(test_config, populated_jira, mock_llm_for_design):
    # 1. Synthesize a Jira ticket in the mock Jira client
    # 2. Inject a JiraStateChangeEvent (TODO → DESIGN_DRAFTING)
    # 3. Daemon's PipelineOrchestrator picks it up
    # 4. Agent runs (LLM responses pre-programmed in mock_llm_for_design)
    # 5. Assert: design Issue created in mock GitHub
    # 6. Assert: operation log written to filesystem + PostgreSQL
    # 7. Assert: Jira transitioned to DESIGN_REVIEW
```

E2E tests use the Mock LLM Adapter throughout — never real LLM calls in CI. Recording-based replay (see "Replay tests" below) closes the gap between mocked and real.

Each Stage gets at least one E2E test. The cross-project flow gets its own (multi-repo workspace, fan-out, Contract section).

### Mock LLM Adapter

`MockLLMAdapter` (from ADR-0014) is the workhorse of every Agent-touching test.

Setup pattern:

```python
mock_llm = MockLLMAdapter()
mock_llm.queue_response(
    when=ResponseMatcher.user_message_contains("start working on"),
    response=LLMResponse(
        content=None,
        tool_calls=[ToolCall("read_jira_ticket", {"jira_key": "PROJ-1"})],
        finish_reason="tool_calls",
        prompt_tokens=1500, completion_tokens=50, total_tokens=1550,
    ),
)
mock_llm.queue_response(
    when=ResponseMatcher.tool_result_contains("PROJ-1 summary"),
    response=LLMResponse(
        content="Done. Design Issue at https://github.com/...",
        tool_calls=[],
        finish_reason="stop",
        ...
    ),
)
```

The `ResponseMatcher` predicates support:

- `user_message_contains(s)`
- `tool_result_for_tool(name)`
- `turn_index(n)`
- `any()` — default fallback

Pattern matching is documented in `tests/conftest.py`. Tests that need bespoke matching write their own predicate.

### Mock Tool Registry

`MockToolRegistry` (from ADR-0013) replaces the real registry in Agent-level tests:

```python
mock_tools = MockToolRegistry()
mock_tools.register_canned(
    name="read_jira_ticket",
    response={"key": "PROJ-1", "summary": "Add OAuth", "description": "...", ...},
)
mock_tools.register_canned(
    name="create_design_issue",
    response={"number": 42, "url": "https://github.com/.../issues/42"},
)
```

For integration tests that use the real Tool Registry, individual tools' external dependencies are mocked at the HTTP / DB layer:

- Jira: `respx` intercepts HTTPS calls to the Jira base URL
- GitHub: `respx` intercepts GitHub API calls
- Git: tests use a temp Git repo (real git binary, real filesystem)

### Fixtures

`tests/conftest.py` exposes the standard fixtures:

```python
@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """A throwaway workspace with .ai-coding-cli/ scaffolding."""
    ...

@pytest.fixture
def test_config(tmp_workspace) -> Config:
    """A valid Config with all required fields populated by safe test values."""
    return build_test_config(WORKSPACE_PATH=str(tmp_workspace))

@pytest.fixture
def mock_llm() -> MockLLMAdapter: ...

@pytest.fixture
def mock_tools() -> MockToolRegistry: ...

@pytest.fixture
def mock_event_bus() -> MockEventBus: ...

@pytest.fixture
async def real_postgres(docker_postgres) -> AsyncConnection:
    """A clean PostgreSQL connection with the schema migrated, for integration tests."""
    ...

@pytest.fixture
async def real_neo4j(docker_neo4j) -> AsyncDriver: ...

@pytest.fixture
def populated_jira() -> MockJiraClient: ...

@pytest.fixture
def populated_github() -> MockGitHubClient: ...
```

Most unit tests use `test_config + mock_llm + mock_tools + mock_event_bus`; that combo isolates the agent code under test.

### Replay tests

A replay test takes a recorded Conversation + recorded LLM responses + recorded tool results, and re-runs the Agent against `MockLLMAdapter` primed with those responses. The test asserts that the new run produces the same sequence of tool calls and the same final outcome.

Recording format (one file per Conversation):

```
tests/replay/fixtures/conv-{conversation_id}.json
{
    "session": {...},
    "conversation": {...},
    "turns": [
        {"index": 0, "llm_response": {...}, "tool_calls": [...]},
        ...
    ]
}
```

Recordings live under `tests/replay/fixtures/`. A small recorder helper captures them from live runs:

```python
async with Recorder(conversation_id) as recorder:
    result = await agent.run("...")
# writes tests/replay/fixtures/conv-{id}.json
```

Replay tests cover deterministic regression: when refactoring Agent internals, the same recorded inputs must produce the same observable outputs. Tools must be in dry-run mode during replay (ADR-0013).

Recordings are versioned. When a schema change makes old recordings invalid, the corresponding test is regenerated explicitly (commit notes the regeneration).

### CI matrix

```yaml
# .github/workflows/ci.yml
jobs:
  unit:
    strategy:
      matrix:
        python: ["3.11", "3.12"]
        os: [ubuntu-latest, windows-latest, macos-latest]
    steps:
      - run: uv sync --extra dev
      - run: uv run pytest tests/unit/ --cov --cov-fail-under=80

  integration:
    services:
      postgres: pgvector/pgvector:pg16
      neo4j: neo4j:5
    steps:
      - run: uv sync --extra dev
      - run: uv run pytest tests/integration/

  e2e:
    services: [postgres, neo4j]
    steps:
      - run: uv run pytest tests/e2e/

  lint:
    steps:
      - run: uv run ruff check .
      - run: uv run mypy --strict src/ai_coding_cli/foundation
      - run: uv run mypy src/ai_coding_cli/application
```

PR merges require: unit + integration + e2e green on Linux Python 3.12 (gate matrix). Windows + macOS unit tests are advisory (informational; not blocking) to keep CI throughput acceptable.

### Test-data conventions

- Workspace fixtures use `tmp_path` so each test is isolated.
- Database fixtures truncate / re-migrate per test (slower) OR use transactions rolled back (faster, preferred where supported).
- Jira / GitHub mock clients seed minimal data; tests add their own.
- Time is controllable: tests use `freezegun` or pass `now()` injection.
- IDs are deterministic in tests: UUIDs derived from `uuid5(NAMESPACE, "test:" + identifier)` rather than random.

### Performance smoke tests

A small `tests/perf/` set runs as a separate (non-blocking) CI job to track regression:

- `test_agent_loop_latency` — measure wall time for a 10-turn synthetic Agent run with `MockLLMAdapter` returning instantly. Target: < 500ms.
- `test_operation_log_write_throughput` — write 1000 logs to PostgreSQL; target: < 10s.
- `test_context_layer_build_time` — for a large session, ContextBuilder.build < 50ms.

Numbers are tracked over commits; failures emit a warning, not a block. Real performance work happens in Phase 8.

### Assertion conventions

- Prefer specific assertions over `assert mock.called`. Test the side-effect, not the call.
- Use `assert_called_once_with` for tools that should be invoked exactly once.
- Custom matchers (e.g., `assert_jira_transitioned_to`) live in `tests/util/matchers.py`.
- Snapshot tests (via `syrupy`) for stable artifacts: rendered templates, generated frontmatter, etc.

### What's NOT tested

| Item | Reason |
|---|---|
| Real OpenAI / Anthropic API calls | Cost + flakiness; covered by manual smoke before releases |
| Real Jira / GitHub against company instance | Not portable; covered by manual smoke |
| Web Dashboard UI interaction | Manual + small Playwright suite in `tests/e2e/web/` (optional in v0.2) |
| LLM output quality | Tracked via human review of real runs + an offline eval set (post-v0.2) |

### Test naming

```
test_<unit-under-test>_<scenario>_<expected-outcome>
```

Examples:

```
test_agent_core_when_llm_returns_tool_calls_then_dispatches_each
test_operation_log_writer_when_unique_constraint_hit_then_raises_conflict
test_compactor_micro_when_no_droppable_tools_then_returns_unchanged
```

Verbose names trade typing for grep-ability. Worth it.

### Test order independence

Tests must pass in any order. Verified by `pytest-randomly` (random seed per run; fixed seed in CI for reproducibility on failure).

### Local dev: which tests run when

```bash
# fast feedback loop
pytest tests/unit/                         # ~30 sec full suite

# subsystem changes
pytest tests/unit/foundation/agent/        # one subdirectory

# before push
pytest tests/unit/ tests/integration/      # ~5 min

# before merge (mirror CI)
pytest                                     # full suite ~10 min with E2E

# coverage report
pytest --cov --cov-report=html
open htmlcov/index.html
```

A `Makefile` exposes shortcuts: `make test-unit`, `make test-integration`, `make test-e2e`, `make test`.

## Consequences

- Unit tests run fast → developers run them frequently; the coverage gate creates pressure to keep modules testable.
- Mocking standardized via `MockLLMAdapter`, `MockToolRegistry`, `MockEventBus`, `MockJiraClient`, `MockGitHubClient` — reduces test boilerplate.
- Integration tests against real PostgreSQL + Neo4j catch real DB-quirk bugs without paying for real LLM calls.
- Replay tests catch regressions in Agent loop behavior when refactoring; the recording-based approach scales with use.
- CI matrix tolerates Windows + macOS unit test flake (advisory) so PRs aren't blocked on platform-specific transient issues.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Property-based testing with hypothesis for state machine + retry counting | Phase 2-3 implementation; defer until property surface is large |
| Q2 | Mutation testing coverage for `foundation/` (mutmut / cosmic-ray) | Post-v0.2 quality push |
| Q3 | Eval set for LLM output quality (separate from unit test framework) | Phase 7 alongside business pipeline migration |
| Q4 | Flaky test policy — quarantine vs auto-retry vs require fix-before-merge | Phase 1 implementation |

## References

- ADR-0009 Agent Core (MockLLMAdapter, replay support)
- ADR-0013 Tool Registry (MockToolRegistry)
- ADR-0014 LLM Adapter (MockAdapter capabilities)
- ADR-0015 Observability (MockEventBus)
- ADR-0016 Configuration management (build_test_config)
- ADR-0017 Error handling taxonomy (error registry + uniqueness test)

## Reviewers

- [ ] Taven
