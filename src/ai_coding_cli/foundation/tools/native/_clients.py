"""Cached Jira + GitHub clients.

Built lazily from Config so unit tests that never touch Jira/GitHub don't
pay the import cost. SSL CA bundle is respected via REQUESTS_CA_BUNDLE.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from atlassian import Jira
from github import Auth, Github

if TYPE_CHECKING:
    from ...config import Config


@lru_cache(maxsize=1)
def get_jira_client(config: "Config") -> Jira:
    """Construct a Jira client honoring auth_kind + base_url.

    `lru_cache` is keyed on the Config object's identity, which is stable for
    the daemon's lifetime. Tests using build_test_config get a fresh client.
    """
    base_url = str(config.jira.base_url).rstrip("/")
    if config.jira.auth_kind == "api_token":
        if config.jira.email is None:
            raise ValueError("JIRA_AUTH_KIND=api_token requires JIRA_EMAIL.")
        return Jira(
            url=base_url,
            username=config.jira.email,
            password=config.jira.api_token.get_secret_value(),
            cloud=True,
        )
    # PAT (Server / Data Center): Bearer token
    return Jira(
        url=base_url,
        token=config.jira.api_token.get_secret_value(),
        cloud=False,
    )


@lru_cache(maxsize=1)
def get_github_client(config: "Config") -> Github:
    """Construct a PyGithub client; configures base_url for GHES."""
    auth = Auth.Token(config.github.token.get_secret_value())
    base_url = str(config.github.base_url).rstrip("/")
    if base_url and base_url != "https://api.github.com":
        # GHES uses a different base_url
        return Github(base_url=base_url, auth=auth, timeout=int(config.github.request_timeout_seconds))
    return Github(auth=auth, timeout=int(config.github.request_timeout_seconds))


def reset_clients() -> None:
    """Tests only. Clear cached clients."""
    get_jira_client.cache_clear()
    get_github_client.cache_clear()
