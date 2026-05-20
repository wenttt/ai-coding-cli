"""Registry of all concrete error classes + uniqueness check (ADR-0017).

A test in tests/unit/foundation/errors/test_registry.py asserts
`check_code_uniqueness()` raises nothing on import.
"""

from __future__ import annotations

from . import _leaves
from ._base import AgentError


def _all_subclasses(cls: type[AgentError]) -> list[type[AgentError]]:
    seen: set[type[AgentError]] = set()
    out: list[type[AgentError]] = []
    stack = [cls]
    while stack:
        current = stack.pop()
        for sub in current.__subclasses__():
            if sub in seen:
                continue
            seen.add(sub)
            out.append(sub)
            stack.append(sub)
    return out


# Materialized on import so tests + tooling can iterate. Filter to concrete
# leaves (classes that are NOT themselves subclassed within this taxonomy).
def _is_leaf(cls: type[AgentError]) -> bool:
    return not any(
        issubclass(other, cls) and other is not cls
        for other in _all_subclasses(AgentError)
    )


# Make sure the leaves module imported above contributed its subclasses.
_ = _leaves

ALL_ERROR_CLASSES: list[type[AgentError]] = [
    cls for cls in _all_subclasses(AgentError) if _is_leaf(cls)
]


def check_code_uniqueness() -> None:
    """Raise ValueError if any two concrete error classes share a `code`.

    Called at module-import time by `__init__` to fail fast in CI / dev,
    and by tests/unit/foundation/errors/test_registry.py for explicit
    coverage.
    """
    seen: dict[str, type[AgentError]] = {}
    for cls in ALL_ERROR_CLASSES:
        existing = seen.get(cls.code)
        if existing is not None and existing is not cls:
            raise ValueError(
                f"Duplicate error code {cls.code!r}: "
                f"{existing.__name__} and {cls.__name__}"
            )
        seen[cls.code] = cls


# Run at import to surface duplicates immediately.
check_code_uniqueness()
