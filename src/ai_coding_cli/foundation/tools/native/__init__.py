"""Native tools registered into the global ToolRegistry.

Importing this package registers every tool via the @tool decorator.
Per ADR-0013, native tools cover: Jira, GitHub, git, repo, tests, plus
a small set of cross-cutting helpers (operation logs, escalation).

This module imports each tool file for its side effects (registration).
Do NOT `from native import *` — use the registry instead.
"""

from __future__ import annotations

# Importing for side effects: @tool decorators register on import.
from . import jira_tools  # noqa: F401
from . import github_tools  # noqa: F401
from . import git_tools  # noqa: F401
from . import repo_tools  # noqa: F401
from . import test_tools  # noqa: F401
