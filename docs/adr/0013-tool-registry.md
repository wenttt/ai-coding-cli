# ADR-0013: Tool Registry

## Status

Accepted

## Date

2026-05-19

## Context

Specify the tool subsystem: tool definition, schema generation, registration, dispatch, MCP bridge, dry-run, visibility, error handling.

## Decision

### Tool protocol

A Tool is an async Python callable with typed inputs and a typed output, exposed to the LLM as an OpenAI-style function.

```python
class Tool(Protocol):
    name: str
    description: str
    input_model: type[BaseModel]               # Pydantic model for arguments
    output_model: type[BaseModel] | None       # optional; if None, output is raw JSON
    side_effects: SideEffectClass               # see "Side-effect classes" below
    requires_confirmation: bool                 # for Action Guardrail
    timeout_seconds: float | None               # per-call timeout override

    async def call(self, args: BaseModel, ctx: ToolContext) -> Any: ...


@dataclass(frozen=True)
class ToolContext:
    session: Session
    conversation_id: ConversationId
    dry_run: bool                               # when True, side-effecting tools simulate
    invocation_id: str                          # unique per call (for tracing)
```

A concrete tool is most commonly written with the `@tool` decorator on an async function:

```python
class ReadRepoFileArgs(BaseModel):
    path: str = Field(description="Path relative to workspace_root")
    max_bytes: int = Field(default=200_000, ge=1, le=2_000_000)


@tool(
    name="read_repo_file",
    description="Read a file from the workspace. Returns the file content as a string. "
                "Caps at max_bytes; larger files return a notice.",
    side_effects=SideEffectClass.READ_ONLY,
)
async def read_repo_file(args: ReadRepoFileArgs, ctx: ToolContext) -> str:
    path = ctx.session.workspace_root / args.path
    # ... read + validate within workspace boundary ...
    return content
```

The decorator registers the function with the global `ToolRegistry` and generates the OpenAI function schema from `ReadRepoFileArgs`.

### Side-effect classes

```python
class SideEffectClass(StrEnum):
    READ_ONLY = "read_only"              # no state change anywhere
    LOCAL_WRITE = "local_write"          # writes to workspace files
    EXTERNAL_READ = "external_read"      # reads from Jira / GitHub / network
    EXTERNAL_WRITE = "external_write"    # writes to Jira / GitHub / sends emails / etc.
    DESTRUCTIVE = "destructive"          # irreversible: git push --force, schema migration, deploy
```

Each class drives different policy:

| Class | Action Guardrail | Dry-run behavior |
|---|---|---|
| READ_ONLY | always allowed | runs normally |
| LOCAL_WRITE | allowed; logged | simulates: returns "would write N bytes to X" |
| EXTERNAL_READ | always allowed | runs normally (reads are cheap to replay) |
| EXTERNAL_WRITE | confirmation required (if `requires_confirmation=True`) | simulates: returns the request payload that would be sent |
| DESTRUCTIVE | always requires confirmation | simulates; never executes in dry-run |

`requires_confirmation` is a per-tool override; the default is True for EXTERNAL_WRITE + DESTRUCTIVE, False otherwise. Action Guardrail (ADR-0025) consumes this.

### ToolRegistry

```python
class ToolRegistry:
    def register(self, tool: Tool) -> None: ...
    def get(self, name: str) -> Tool: ...
    def all(self) -> list[Tool]: ...

    def schemas_for_llm(
        self,
        *,
        allow_only: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> list[dict]:
        """Generate OpenAI function-calling schemas for the LLM.
        Filters: allow_only / exclude / hidden tools."""

    async def call(
        self,
        name: str,
        arguments: dict | BaseModel,
        ctx: ToolContext,
    ) -> ToolResult:
        """Dispatch a call. Validates args via Pydantic, runs the tool, wraps result."""


@dataclass(frozen=True)
class ToolResult:
    tool_name: str
    invocation_id: str
    status: Literal["success", "error", "timeout", "refused"]
    content: str                       # JSON-serialized output or error message
    raw_value: Any | None              # untruncated; used for in-process consumers (not LLM)
    duration_seconds: float
    side_effects_recorded: list[SideEffectRecord]
```

The registry is a singleton per daemon process. Native tools are registered at import time (the `@tool` decorator does it). MCP-bridged tools are registered dynamically when their bridges connect.

### Native tools (v0.2 default set)

Registered at package import. Listed by category:

**Jira** (from ADR-0006 routing):
- `read_jira_ticket` (EXTERNAL_READ)
- `list_my_tickets` (EXTERNAL_READ)
- `create_jira_ticket` (EXTERNAL_WRITE)
- `create_jira_sub_task` (EXTERNAL_WRITE)
- `update_jira_ticket` (EXTERNAL_WRITE)
- `transition_jira_status` (EXTERNAL_WRITE; orchestrator-only by convention, hidden from agents)
- `add_jira_comment` (EXTERNAL_WRITE)
- `find_design_issue_for_jira` (EXTERNAL_READ)
- `lookup_project_for_ticket` (READ_ONLY)
- `affected_projects_for_ticket` (READ_ONLY)
- `check_workspace_matches` (READ_ONLY)

**GitHub**:
- `read_github_issue` (EXTERNAL_READ)
- `create_design_issue` (EXTERNAL_WRITE)
- `update_design_issue` (EXTERNAL_WRITE)
- `add_issue_comment` (EXTERNAL_WRITE)
- `close_issue` (EXTERNAL_WRITE)
- `list_issue_comments` (EXTERNAL_READ)
- `get_pr_state` (EXTERNAL_READ)
- `create_pr` (EXTERNAL_WRITE)
- `list_pr_review_comments` (EXTERNAL_READ)
- `find_pr_by_branch` (EXTERNAL_READ)
- `create_implementation_issue` (EXTERNAL_WRITE)

**Git** (local):
- `git_status` (READ_ONLY)
- `git_diff` (READ_ONLY)
- `git_log` (READ_ONLY)
- `git_changed_files` (READ_ONLY)
- `git_create_branch` (LOCAL_WRITE)
- `git_checkout` (LOCAL_WRITE)
- `git_add` (LOCAL_WRITE)
- `git_commit` (LOCAL_WRITE)
- `git_push` (EXTERNAL_WRITE; requires_confirmation=False — pushing to feature branch is routine, but `git_push_force` is DESTRUCTIVE)
- `git_push_force` (DESTRUCTIVE)

**Repo / filesystem**:
- `read_repo_file` (READ_ONLY)
- `list_repo_files` (READ_ONLY)
- `write_repo_file` (LOCAL_WRITE)
- `find_relevant_modules` (READ_ONLY)
- `analyze_repo_state` (READ_ONLY)

**Tests**:
- `discover_test_framework` (READ_ONLY)
- `discover_test_files` (READ_ONLY)
- `run_tests` (LOCAL_WRITE — test runners may create cache files; results not destructive)

**Operation logs** (ADR-0005):
- `write_operation_log` (LOCAL_WRITE; the orchestrator calls this via internal API, but the agent can also invoke it if a stage handler delegates)
- `read_operation_logs` (READ_ONLY)
- `get_retry_count` (READ_ONLY)

**Skill** (ADR-0012):
- `load_skill` (READ_ONLY — loads from local files)

**Deployment**:
- `trigger_deployment` (EXTERNAL_WRITE + DESTRUCTIVE depending on target)
- `verify_deployment` (EXTERNAL_READ)

**Cross-cutting**:
- `escalate` (LOCAL_WRITE + EXTERNAL_WRITE — writes an ESCALATED log + posts Jira comment + adds label)

### Hidden tools

Some tools are registered but not exposed to the LLM:

- `transition_jira_status` — the orchestrator owns transitions, per ADR-0003. Exposing it to the agent risks the agent skipping orchestrator-side logging.
- `write_operation_log` — same reason; the orchestrator owns log writes.
- `escalate` — orchestrator owns escalation.

Hidden tools are flagged with `_visible_to_agent = False` on the Tool object. `schemas_for_llm()` filters them out.

### MCP bridge

The Tool Registry can register tools from external MCP servers. Configuration:

```yaml
# ~/.config/ai-coding-cli/mcp_bridges.yaml
bridges:
  - name: company-mcp-tools
    transport: stdio
    command: /usr/local/bin/company-mcp-server
    args: ["--config", "/etc/company-mcp/config.yaml"]
    env:
      COMPANY_API_TOKEN: "${env:COMPANY_API_TOKEN}"
    tools_namespace: company       # bridged tools are prefixed: "company.foo_bar"
    auto_start: true
    timeout_seconds: 30
```

At daemon startup, `MCPBridgeManager` launches each enabled bridge as a subprocess (or connects to a configured URL), performs the MCP `initialize` handshake, lists the bridge's tools, and registers each one in the local `ToolRegistry` with the configured `tools_namespace` prefix.

Bridged tools have:
- `side_effects = EXTERNAL_WRITE` by default (conservative; MCP doesn't expose effect class).
- `requires_confirmation = True` by default.
- `timeout_seconds` = bridge config.
- `_visible_to_agent = True`.

If a bridge process exits unexpectedly, `MCPBridgeManager` logs the failure, marks its tools as unavailable (calls to them return `ToolResult(status="error", content="bridge offline")`), and retries the connection per a backoff policy.

### Schema generation

For native tools, OpenAI-style schemas are generated from `input_model`:

```python
class ReadRepoFileArgs(BaseModel):
    path: str = Field(description="Path relative to workspace_root")
    max_bytes: int = Field(default=200_000, ge=1, le=2_000_000)

# generated schema:
{
    "type": "function",
    "function": {
        "name": "read_repo_file",
        "description": "Read a file from the workspace...",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to workspace_root"},
                "max_bytes": {"type": "integer", "default": 200_000, "minimum": 1, "maximum": 2_000_000}
            },
            "required": ["path"]
        }
    }
}
```

Generation uses Pydantic's `model_json_schema()` followed by post-processing to fit OpenAI's expected shape (removing `$defs`, inlining nested models, stripping fields OpenAI doesn't honor).

For MCP-bridged tools, the bridge returns JSON Schema directly (per MCP spec); we wrap it in the OpenAI function envelope.

### Dispatch

```python
async def call(self, name: str, arguments: dict | BaseModel, ctx: ToolContext) -> ToolResult:
    tool = self.get(name)
    started = monotonic()

    # 1. Validate arguments
    if isinstance(arguments, dict):
        try:
            args_model = tool.input_model(**arguments)
        except ValidationError as exc:
            return ToolResult.error(name, ctx.invocation_id, f"Invalid arguments: {exc}")
    else:
        args_model = arguments

    # 2. Side-effect recording start
    side_effect_recorder = SideEffectRecorder.for_class(tool.side_effects)
    side_effect_recorder.start()

    # 3. Execute (with timeout)
    timeout = tool.timeout_seconds or 60.0
    try:
        raw_value = await asyncio.wait_for(tool.call(args_model, ctx), timeout=timeout)
    except asyncio.TimeoutError:
        return ToolResult.timeout(name, ctx.invocation_id, duration=monotonic() - started)
    except Exception as exc:
        return ToolResult.error(name, ctx.invocation_id, str(exc), duration=monotonic() - started)

    # 4. Serialize output
    if tool.output_model is not None:
        content = tool.output_model.model_validate(raw_value).model_dump_json()
    else:
        content = json.dumps(raw_value, ensure_ascii=False, default=str)

    # 5. Return ToolResult
    return ToolResult(
        tool_name=name,
        invocation_id=ctx.invocation_id,
        status="success",
        content=content,
        raw_value=raw_value,
        duration_seconds=monotonic() - started,
        side_effects_recorded=side_effect_recorder.records,
    )
```

The Agent Core (ADR-0009) is the typical caller. Stage handlers may call tools directly for non-LLM-mediated work.

### Dry-run mode

When `ToolContext.dry_run = True`:

- READ_ONLY + EXTERNAL_READ tools: execute normally.
- LOCAL_WRITE tools: simulate. Implementation pattern:

  ```python
  async def write_repo_file(args, ctx):
      if ctx.dry_run:
          return f"[DRY-RUN] Would write {len(args.content)} bytes to {args.path}"
      # ... real write ...
  ```

- EXTERNAL_WRITE + DESTRUCTIVE tools: return the payload that would be sent, without performing the operation.

Replay (ADR-0009) uses dry-run when re-running a Conversation against a mock LLM, ensuring side-effect-free replay.

Tools opt in to dry-run by checking `ctx.dry_run` at the start of their implementation. The decorator can wrap a tool with `@dry_run_supported` to enforce this check at registration time (optional; for stricter codebases).

### Side-effect records

`SideEffectRecord` captures what actually happened in a side-effecting tool:

```python
@dataclass(frozen=True)
class SideEffectRecord:
    class_: SideEffectClass
    summary: str                       # human-readable: "wrote 1247 bytes to src/auth/login.py"
    details: dict[str, Any]            # structured: {"path": "...", "bytes": 1247, "previous_sha256": "...", "new_sha256": "..."}
    timestamp: datetime
```

Records are collected on the `ToolResult` and surfaced in operation logs. They power:

- Operation log "Impact" section auto-population
- Dashboard side-effect timeline per ticket
- Audit replay verification

### Visibility filtering for the LLM

When the Agent Core asks for `schemas_for_llm()`:

```python
schemas_for_llm(
    allow_only=tool_set_for_current_stage,    # from the active Skill's tools_allowed
    exclude=session.locked_tools,              # tools temporarily disabled
)
```

The active Skill's `tools_allowed` (ADR-0012) filters the visible tool set. `session.locked_tools` is populated by Guardrail (ADR-0025) when a tool was used incorrectly and needs cooling-off.

Hidden tools (`_visible_to_agent=False`) are always excluded regardless of filters.

### Concurrency

- Multiple tools may run concurrently within one Agent turn, bounded by `max_parallel_tool_calls` (ADR-0009).
- Each tool's `call()` MUST be re-entrant: two simultaneous invocations with different args should not corrupt state. Filesystem tools serialize writes to the same path via per-path asyncio locks owned by the registry.
- MCP-bridged tools: the bridge subprocess handles its own concurrency; we issue concurrent requests up to a per-bridge limit (default 4).

### Error handling

| Error | Result status | Notes |
|---|---|---|
| Pydantic argument validation fails | `error` | LLM sees the validation error and retries with corrected args |
| Tool raises a known exception | `error` | message includes exc type + str(exc) |
| Tool times out | `timeout` | LLM sees `[TIMEOUT]` and decides next step |
| Action Guardrail refuses | `refused` | LLM sees refusal reason; can ask user for confirmation |
| MCP bridge offline | `error` | content explains bridge unavailability |
| Tool raises FatalError | re-raised, Agent halts | rare; used by tools that detect unrecoverable misconfig |

### Observability

Tool dispatch emits on the EventBus (ADR-0015):

```
tool.dispatched   { name, invocation_id, args_preview, side_effects_class }
tool.completed    { name, invocation_id, status, duration_seconds, summary }
```

These power the Dashboard's tool-call timeline.

### Testing

Tools have two test surfaces:

1. **Unit**: call the tool function directly with constructed `ToolContext`, assert on `raw_value` / side effects.
2. **Registry**: call `registry.call("tool_name", {...}, ctx)` and assert `ToolResult`.

A `MockToolRegistry` is provided for Agent-level tests: pre-program `name -> ToolResult` mappings; calls return the canned result without invoking real tools.

### CLI commands

```
ai-coding tools list                       # all registered tools + their side-effect class
ai-coding tools schema <name>              # print the OpenAI schema
ai-coding tools call <name> --args '{...}' # invoke a tool directly (for debugging)
ai-coding bridges list                     # MCP bridges + their status
```

## Consequences

- Tools are typed end-to-end (Pydantic in / out), so misuse is caught at validation, not at runtime deep inside the tool body.
- Side-effect classification drives Guardrail policy, dry-run behavior, and operation log auto-population without each tool needing to opt in to each subsystem.
- MCP bridge gives v0.2 access to existing MCP ecosystems (Claude Code's tool servers, vendor MCP servers) without rewriting them.
- Hidden tools (orchestrator-only) prevent the LLM from bypassing the orchestrator's audit logging.
- The `MockToolRegistry` makes Agent-level tests fast and deterministic.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Output truncation policy for large tool results (currently caller's responsibility) — should the registry enforce a default cap? | Implementation tuning |
| Q2 | MCP bridge security: how to sandbox a bridge subprocess that has more permissions than the daemon should grant tools? | ADR-0025 (Guardrail) + bridge admin docs |
| Q3 | Tool versioning: when a tool's input_model changes shape, how do replay-from-history work for old Conversations? | Phase 5 implementation; likely store schema with each Conversation |
| Q4 | Concurrency limits per side-effect class (e.g., max 1 destructive call in flight per Session) | Phase 5; possibly via Action Guardrail |

## References

- ADR-0008 Session + Conversation Model (ToolContext.session)
- ADR-0009 Agent Core (consumes ToolRegistry.schemas_for_llm + dispatches calls)
- ADR-0010 Context Layer (tool results appended as Tier 3 messages)
- ADR-0012 Skill Loader (tools_required / tools_allowed)
- ADR-0014 LLM Adapter (consumes generated schemas)
- ADR-0015 Observability (tool events)
- ADR-0025 Guardrail Layer (consumes side_effects class + requires_confirmation)

## Reviewers

- [ ] Taven
