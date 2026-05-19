# ADR-0016: Configuration Management

## Status

Accepted

## Date

2026-05-19

## Context

Specify the configuration system: sources, precedence, schema, validation, secret handling, subsystem composition, reload behavior.

## Decision

### Library

`pydantic-settings` (>= 2.x). Every config block is a Pydantic `BaseSettings` model with strict typing and field validation. The library handles env-var parsing, type coercion, and nested settings out of the box.

### Sources, in precedence order (highest first)

```
1. CLI flags                                  (--llm-model, --workspace-path, etc.)
2. Process environment variables              (LLM_PRIMARY_MODEL=..., WORKSPACE_PATH=...)
3. Workspace .env                              {workspace_root}/.ai-coding-cli/.env
4. User .env                                   ~/.config/ai-coding-cli/.env
5. Built-in defaults                           coded into the Pydantic models
```

A value defined at a higher-precedence level overrides lower ones. The actual hierarchy is implemented by pydantic-settings' `SettingsConfigDict(env_file=(...))` with multiple files, processed left-to-right (last wins), with CLI overrides applied last by Typer.

### Top-level Config

```python
class Config(BaseSettings):
    # subsystem configs (composed)
    agent: AgentConfig
    context: ContextConfig
    compactor: CompactorConfig
    llm: LLMConfig
    storage: StorageConfig
    jira: JiraConfig
    github: GitHubConfig
    project_mapping: ProjectMappingConfig
    observability: ObservabilityConfig
    daemon: DaemonConfig
    guardrail: GuardrailConfig
    skill: SkillConfig
    web: WebDashboardConfig

    # top-level
    workspace_path: Path
    user_id: str = Field(default_factory=lambda: os.environ.get("USER", "developer"))

    model_config = SettingsConfigDict(
        env_prefix="",                          # children declare their own prefixes
        env_nested_delimiter="__",              # AGENT__MAX_TURNS=20
        env_file=_resolve_env_files(),          # workspace .env + user .env
        env_file_encoding="utf-8",
        extra="forbid",                         # unknown fields = error
        case_sensitive=False,
    )
```

`_resolve_env_files()` returns the env files in precedence order so pydantic-settings can layer them.

### Subsystem configs

Each subsystem owns its own `BaseSettings` model with an `env_prefix`:

```python
class AgentConfig(BaseSettings):
    max_turns: int = 20
    max_tokens_per_turn: int = 8_000
    max_total_tokens: int = 200_000
    max_parallel_tool_calls: int = 5
    tool_call_timeout_seconds: float = 60.0
    turn_timeout_seconds: float = 300.0

    model_config = SettingsConfigDict(env_prefix="AGENT_")


class LLMConfig(BaseSettings):
    primary: AdapterConfig
    fallback: AdapterConfig | None = None
    compaction: AdapterConfig | None = None
    request_timeout_seconds: float = 300.0
    rate_limit_retry_max: int = 3
    rate_limit_retry_base_seconds: float = 2.0

    model_config = SettingsConfigDict(env_prefix="LLM_", env_nested_delimiter="__")


class AdapterConfig(BaseSettings):
    kind: Literal["openai-compat", "anthropic-native", "mock"]
    model_name: str
    base_url: HttpUrl | None = None
    api_key: SecretStr | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class StorageConfig(BaseSettings):
    postgres_dsn: SecretStr = SecretStr("postgresql://localhost/ai_coding_cli")
    postgres_pool_size: int = 10
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr
    enable_neo4j: bool = True

    model_config = SettingsConfigDict(env_prefix="STORAGE_")


class JiraConfig(BaseSettings):
    base_url: HttpUrl
    auth_kind: Literal["api_token", "pat"] = "api_token"
    email: EmailStr | None = None                     # for api_token mode
    api_token: SecretStr
    request_timeout_seconds: float = 30.0
    poll_active_seconds: int = 60
    poll_idle_seconds: int = 300

    model_config = SettingsConfigDict(env_prefix="JIRA_")


class GitHubConfig(BaseSettings):
    base_url: HttpUrl = HttpUrl("https://api.github.com")  # GHES → set to your /api/v3
    token: SecretStr
    default_owner: str | None = None
    default_repo: str | None = None
    request_timeout_seconds: float = 30.0

    model_config = SettingsConfigDict(env_prefix="GITHUB_")


class DaemonConfig(BaseSettings):
    http_host: str = "127.0.0.1"
    http_port: int = 8080
    webhook_secret: SecretStr
    enable_polling: bool = True

    model_config = SettingsConfigDict(env_prefix="DAEMON_")


class WebDashboardConfig(BaseSettings):
    enabled: bool = True
    port: int = 8080                                  # shared with daemon HTTP
    open_browser_on_start: bool = True

    model_config = SettingsConfigDict(env_prefix="WEB_")


class GuardrailConfig(BaseSettings):
    input_check_enabled: bool = True
    output_check_enabled: bool = True
    action_confirmation_mode: Literal["always", "destructive_only", "never"] = "destructive_only"
    prompt_injection_threshold: float = 0.8

    model_config = SettingsConfigDict(env_prefix="GUARDRAIL_")


class SkillConfig(BaseSettings):
    auto_preload_enabled: bool = True
    max_skill_tokens_warn: int = 10_000

    model_config = SettingsConfigDict(env_prefix="SKILL_")


class ProjectMappingConfig(BaseSettings):
    mapping_file: Path = Path("~/.config/ai-coding-cli/project_mapping.yaml").expanduser()
    reload_on_sighup: bool = True

    model_config = SettingsConfigDict(env_prefix="PROJECT_MAPPING_")
```

### Env variable conventions

Subsystem env vars: `{PREFIX}_{FIELD}` for top-level fields, `{PREFIX}_{NESTED}__{FIELD}` for nested.

Examples:
- `AGENT_MAX_TURNS=20`
- `LLM_PRIMARY__KIND=openai-compat`
- `LLM_PRIMARY__BASE_URL=https://llm.company.com/v1`
- `LLM_PRIMARY__API_KEY=sk-...`
- `LLM_PRIMARY__MODEL_NAME=gpt-4o`
- `LLM_COMPACTION__MODEL_NAME=gpt-4o-mini`
- `STORAGE_POSTGRES_DSN=postgresql://localhost/db`
- `JIRA_BASE_URL=https://jira.company.com`
- `JIRA_AUTH_KIND=pat`
- `JIRA_API_TOKEN=NjQ...`
- `GITHUB_BASE_URL=https://github.company.com/api/v3`
- `GITHUB_TOKEN=ghp_...`

### .env example file shipped

`.env.example` at repo root, exhaustive. Users copy to `.env` and fill in.

```
# ──────────────────────────────────────────────────────────
# Required
# ──────────────────────────────────────────────────────────
WORKSPACE_PATH=/Users/me/workspaces/proj

JIRA_BASE_URL=https://jira.company.com
JIRA_AUTH_KIND=pat
JIRA_API_TOKEN=

GITHUB_BASE_URL=https://github.company.com/api/v3
GITHUB_TOKEN=
GITHUB_DEFAULT_OWNER=company

LLM_PRIMARY__KIND=openai-compat
LLM_PRIMARY__BASE_URL=https://llm.company.com/v1
LLM_PRIMARY__API_KEY=
LLM_PRIMARY__MODEL_NAME=gpt-4o

STORAGE_POSTGRES_DSN=postgresql://localhost/ai_coding_cli
STORAGE_NEO4J_PASSWORD=

DAEMON_WEBHOOK_SECRET=

# ──────────────────────────────────────────────────────────
# Optional (defaults in parentheses)
# ──────────────────────────────────────────────────────────
# LLM_COMPACTION__MODEL_NAME=gpt-4o-mini
# LLM_FALLBACK__KIND=anthropic-native
# LLM_FALLBACK__API_KEY=
# LLM_FALLBACK__MODEL_NAME=claude-3-5-sonnet-20241022
#
# AGENT_MAX_TURNS=20                        (20)
# AGENT_MAX_TOTAL_TOKENS=200000             (200000)
#
# STORAGE_ENABLE_NEO4J=true                 (true)
# STORAGE_POSTGRES_POOL_SIZE=10             (10)
#
# JIRA_POLL_ACTIVE_SECONDS=60               (60)
# JIRA_POLL_IDLE_SECONDS=300                (300)
#
# DAEMON_HTTP_PORT=8080                     (8080)
# WEB_OPEN_BROWSER_ON_START=true            (true)
#
# GUARDRAIL_ACTION_CONFIRMATION_MODE=destructive_only
# (always | destructive_only | never)
```

### Secret handling

All credential / token fields are `SecretStr` (pydantic). Properties:

- `repr()` returns `SecretStr('**********')`
- `str()` returns `'**********'`
- `.get_secret_value()` returns the underlying string (must be called explicitly)
- Auto-redacted in logs and `Config.model_dump_json()`

The CLI command `ai-coding config show` prints the full config with secrets redacted. To inspect secrets, `ai-coding config show --reveal-secrets` requires interactive confirmation.

### Validation

Pydantic validates on `Config()` construction:

- Required fields without defaults raise `ValidationError`.
- `HttpUrl` / `EmailStr` / `Literal[...]` enforce types.
- Custom validators (per field) catch known-bad values (e.g., webhook port < 1024 + non-root user = warning).
- Cross-field validation in `@model_validator(mode="after")`:
  - `JiraConfig.auth_kind == "api_token"` requires `email` set.
  - `LLMConfig.fallback` set requires both fallback fields filled (no half-config).
  - `StorageConfig.enable_neo4j == False` is acceptable but logged as a warning (graph features will be unavailable).

Validation failure at daemon startup writes a structured error to stderr listing every offending field and exits non-zero. The output is human-friendly:

```
Configuration error:
  - JIRA_API_TOKEN: field required
  - GITHUB_TOKEN: field required
  - LLM_PRIMARY__BASE_URL: invalid URL ("https//llm.company.com" — missing colon)

See .env.example for required fields.
```

### Loading flow

```python
async def load_config(
    cli_overrides: dict[str, Any] | None = None,
) -> Config:
    # 1. Determine workspace_path (from CLI or current working directory)
    # 2. Resolve env files in precedence order:
    #    [user_env_file, workspace_env_file]  (pydantic-settings loads left-to-right)
    # 3. Construct Config; pydantic-settings reads env files + process env vars
    # 4. Apply CLI overrides on top
    # 5. Run cross-field validators
    # 6. Return (or raise + format errors)
```

The daemon, CLI, and Web Dashboard share this loader. Config is constructed once per process and passed by reference.

### Reload behavior

In v0.2:

- **Daemon**: on SIGHUP, reloads `project_mapping.yaml` and re-evaluates `JIRA_POLL_*` cadences. Does NOT reload LLM / Storage / token settings (those would require restarting connection pools and HTTP clients, which complicates partial reload).
- **CLI** (one-shot mode): no reload concept; each invocation re-reads config.
- **Web Dashboard**: no reload; reads from the daemon's in-memory Config.

Full config reload on SIGHUP is post-v0.2.

### CLI integration

Typer commands accept `--config` to point at a specific `.env` file (overrides the default user/workspace lookup) and individual `--{field}` flags for common overrides:

```
ai-coding daemon start \
    --config /etc/ai-coding/prod.env \
    --workspace-path /var/lib/proj \
    --agent-max-turns 30

ai-coding chat "..." --llm-model gpt-4o-mini      # one-shot override
```

The `--{field}` map is hand-coded for the most common knobs (Typer can't auto-derive from arbitrary nested Pydantic models). The full set is documented in `ai-coding config flags`.

### Test fixtures

A `TestConfig` factory builds a Config with all required fields populated by safe test values:

```python
def build_test_config(**overrides) -> Config:
    base = {
        "WORKSPACE_PATH": tmp_path,
        "JIRA_BASE_URL": "https://jira.test",
        "JIRA_API_TOKEN": "test-token",
        "GITHUB_TOKEN": "test-token",
        "LLM_PRIMARY__KIND": "mock",
        "LLM_PRIMARY__MODEL_NAME": "mock-1",
        "STORAGE_POSTGRES_DSN": "postgresql://localhost:5432/test",
        "STORAGE_NEO4J_PASSWORD": "test-password",
        "DAEMON_WEBHOOK_SECRET": "test-secret",
    }
    base.update(overrides)
    with monkeypatch_env(base):
        return Config()
```

Used by every test that needs a Config.

### Configuration discovery in the workspace

When a developer runs `ai-coding init` in a workspace for the first time:

```
ai-coding init
    creates {workspace_root}/.ai-coding-cli/.env (empty stub with required fields commented in)
    creates {workspace_root}/.ai-coding-cli/conventions.md (template; ADR-0010)
    creates {workspace_root}/.ai-coding-cli/skills/ (empty)
    creates {workspace_root}/.ai-coding-cli/templates/ (empty; user overrides go here)
    appends .ai-coding-cli/.env to {workspace_root}/.gitignore
```

This is the standard onboarding step per workspace.

### Failure handling

| Failure | Behavior |
|---|---|
| Missing required field | `ValidationError` listing all missing fields; exit 1 with friendly message |
| Invalid value (wrong type, bad URL) | `ValidationError`; exit 1 |
| `.env` file unreadable (permissions) | Continue with what's available; log warning |
| Conflicting workspace + user .env | Workspace wins (per precedence); no error |
| `extra="forbid"` violation (unknown env field) | `ValidationError`; suggests typo correction via difflib |
| Secret field value is suspiciously short (< 10 chars) | Log WARN at startup; allow |

## Consequences

- One source of truth for what fields exist + their defaults: the Pydantic models.
- Type safety from config load through downstream code (no `os.environ.get(...) or default` scattered around).
- Secrets are uniformly redacted in logs and `config show`.
- Precedence is unambiguous (CLI > process env > workspace .env > user .env > defaults).
- `ai-coding init` makes new-workspace onboarding a single command.
- Partial reload (project mapping only) covers the v0.2 hot path; full reload is deferred.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Secrets backend (system keychain, HashiCorp Vault, AWS Secrets Manager) — when does the team need this | Post-v0.2; ship with .env + redaction as the v0.2 baseline |
| Q2 | Whether SIGHUP should reload more than project_mapping (e.g., LLM model name without DB reconnects) | Phase 8 implementation, depends on production feedback |
| Q3 | Auto-detection of GitHub Enterprise vs github.com from `GITHUB_BASE_URL` (currently explicit) | Phase 1 implementation polish |
| Q4 | Per-developer encrypted .env (so committed `.env.example` can include scaffolded secrets entries) | Post-v0.2 |

## References

- ADR-0001 System Overview
- ADR-0006 Multi-project + cross-project routing (project_mapping.yaml integration)
- ADR-0009 Agent Core (AgentConfig)
- ADR-0010 Context Layer (ContextConfig)
- ADR-0011 Compactor (CompactorConfig)
- ADR-0013 Tool Registry (consumes JiraConfig + GitHubConfig + StorageConfig)
- ADR-0014 LLM Adapter (LLMConfig)
- ADR-0015 Observability (ObservabilityConfig)
- ADR-0019 Storage Layer (StorageConfig schema)

## Reviewers

- [ ] Taven
