"""Pydantic-Settings models. See ADR-0016 + ADR-0030."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import EmailStr, Field, HttpUrl, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Subsystem configs
# ---------------------------------------------------------------------------


class AdapterConfig(BaseSettings):
    """One LLM provider entry. Used as nested config in LLMConfig.primary, etc."""

    kind: Literal["openai-compat", "anthropic-native", "mock"] = "openai-compat"
    model_name: str = "gpt-4o"
    base_url: HttpUrl | None = None
    api_key: SecretStr | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class LLMEmbeddingConfig(BaseSettings):
    """Embedding model. Defaults to text-embedding-3-small on the primary LLM endpoint."""

    model_name: str = "text-embedding-3-small"
    base_url: HttpUrl | None = None
    api_key: SecretStr | None = None
    batch_size: int = 100


class LLMConfig(BaseSettings):
    primary: AdapterConfig = Field(default_factory=AdapterConfig)
    fallback: AdapterConfig | None = None
    compaction: AdapterConfig | None = None
    embedding: LLMEmbeddingConfig = Field(default_factory=LLMEmbeddingConfig)
    request_timeout_seconds: float = 300.0
    rate_limit_retry_max: int = 3
    rate_limit_retry_base_seconds: float = 2.0

    model_config = SettingsConfigDict(env_prefix="LLM_", env_nested_delimiter="__")


class AgentConfig(BaseSettings):
    max_turns: int = 20
    max_tokens_per_turn: int = 8_000
    max_total_tokens: int = 200_000
    max_parallel_tool_calls: int = 5
    tool_call_timeout_seconds: float = 60.0
    turn_timeout_seconds: float = 300.0

    model_config = SettingsConfigDict(env_prefix="AGENT_")


class StorageConfig(BaseSettings):
    db_path: Path = Field(
        default_factory=lambda: Path.home() / ".ai-coding-cli" / "state.db"
    )
    # Reserved for Standard profile. Lite ignores postgres_dsn + neo4j_*.
    postgres_dsn: SecretStr | None = None
    neo4j_uri: str | None = None
    neo4j_user: str | None = None
    neo4j_password: SecretStr | None = None
    enable_neo4j: bool = False

    model_config = SettingsConfigDict(env_prefix="STORAGE_")


class JiraConfig(BaseSettings):
    base_url: HttpUrl = HttpUrl("https://jira.example.com")
    auth_kind: Literal["api_token", "pat"] = "pat"
    email: EmailStr | None = None
    api_token: SecretStr = SecretStr("")
    request_timeout_seconds: float = 30.0
    poll_active_seconds: int = 60
    poll_idle_seconds: int = 300

    model_config = SettingsConfigDict(env_prefix="JIRA_")

    @model_validator(mode="after")
    def _check_cloud_auth_has_email(self) -> "JiraConfig":
        if self.auth_kind == "api_token" and self.email is None:
            raise ValueError(
                "JIRA_AUTH_KIND=api_token requires JIRA_EMAIL to also be set "
                "(Atlassian Cloud auth uses email + API token)."
            )
        return self


class GitHubConfig(BaseSettings):
    base_url: HttpUrl = HttpUrl("https://api.github.com")
    token: SecretStr = SecretStr("")
    default_owner: str | None = None
    default_repo: str | None = None
    request_timeout_seconds: float = 30.0

    model_config = SettingsConfigDict(env_prefix="GITHUB_")


class DaemonConfig(BaseSettings):
    http_host: str = "127.0.0.1"
    http_port: int = 8080
    webhook_secret: SecretStr = SecretStr("")
    enable_polling: bool = True
    shutdown_timeout_seconds: float = 10.0

    model_config = SettingsConfigDict(env_prefix="DAEMON_")


class WebDashboardConfig(BaseSettings):
    enabled: bool = True
    open_browser_on_start: bool = True
    light_auth_enabled: bool = False
    tailwind_mode: Literal["cdn", "local_build"] = "cdn"

    model_config = SettingsConfigDict(env_prefix="WEB_")


class GuardrailConfig(BaseSettings):
    input_check_enabled: bool = True
    output_check_enabled: bool = True
    output_secret_block: bool = True
    output_sensitive_file_redact: bool = True
    output_pii_detection_enabled: bool = False
    action_confirmation_mode: Literal["never", "destructive_only", "always"] = (
        "destructive_only"
    )
    action_confirmation_timeout_seconds: int = 300
    action_headless: bool = False
    prompt_injection_threshold: float = 0.8
    prompt_injection_threshold_tool_result: float = 0.6
    prompt_injection_threshold_rag: float = 0.7
    sensitive_files_list_path: Path | None = None
    secret_patterns_extra: list[str] = Field(default_factory=list)

    model_config = SettingsConfigDict(env_prefix="GUARDRAIL_")


class SkillConfig(BaseSettings):
    auto_preload_enabled: bool = True
    max_skill_tokens_warn: int = 10_000
    max_unique_skills_per_conversation: int = 5  # cycle-prevention guard from review

    model_config = SettingsConfigDict(env_prefix="SKILL_")


class ObservabilityConfig(BaseSettings):
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "console"
    log_file_path: Path | None = None  # default resolved in load_config()
    log_file_max_bytes: int = 100_000_000
    log_file_backup_count: int = 10
    event_bus_queue_size: int = 10_000

    model_config = SettingsConfigDict(env_prefix="OBSERVABILITY_")


class MCPBridgesConfig(BaseSettings):
    bridges_path: Path | None = None  # default ~/.config/ai-coding-cli/mcp_bridges.yaml
    enabled: bool = True

    model_config = SettingsConfigDict(env_prefix="MCP_")


# ---------------------------------------------------------------------------
# Top-level Config
# ---------------------------------------------------------------------------


class Config(BaseSettings):
    """Top-level configuration. Composes every subsystem config."""

    workspace_path: Path
    user_id: str = Field(
        default_factory=lambda: os.environ.get("USER", os.environ.get("USERNAME", "developer"))
    )

    agent: AgentConfig = Field(default_factory=AgentConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    jira: JiraConfig = Field(default_factory=JiraConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    web: WebDashboardConfig = Field(default_factory=WebDashboardConfig)
    guardrail: GuardrailConfig = Field(default_factory=GuardrailConfig)
    skill: SkillConfig = Field(default_factory=SkillConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    mcp: MCPBridgesConfig = Field(default_factory=MCPBridgesConfig)

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        # env_file is configured in load_config() based on resolved paths
        env_file_encoding="utf-8",
        extra="ignore",  # forbid is too strict for tests; ignore unknown env vars
        case_sensitive=False,
    )

    @model_validator(mode="after")
    def _check_workspace_path(self) -> "Config":
        # Resolve workspace_path; existence check is in load_config to keep the
        # model usable for test fixtures that don't have a real workspace.
        self.workspace_path = self.workspace_path.expanduser()
        return self
