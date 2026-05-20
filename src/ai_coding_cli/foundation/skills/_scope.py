"""Scope expression evaluator. See ADR-0012 §Scope expressions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScopeContext:
    """Inputs the scope expression is evaluated against."""

    stage: str | None = None
    mode: str | None = None        # brownfield | greenfield
    language: str | None = None    # detected primary language
    is_cross_project: bool = False
    jira_labels: tuple[str, ...] = ()


def evaluate_scope(expression: str, ctx: ScopeContext) -> bool:
    """Evaluate a scope expression. Grammar:

    - `always` -> True (every conversation)
    - `manual` -> False (never auto-preloaded)
    - `key:value` -> match `getattr(ctx, key) == value` (case-insensitive)
    - `expr1 + expr2` -> AND
    - `expr1 , expr2` -> OR

    Examples: `stage:design`, `stage:implement + language:python`,
    `mode:greenfield, mode:brownfield`.
    """
    expression = expression.strip()
    if not expression:
        return False
    if expression == "always":
        return True
    if expression == "manual":
        return False

    # OR over comma-separated AND-groups.
    for or_group in [g.strip() for g in expression.split(",")]:
        if _evaluate_and_group(or_group, ctx):
            return True
    return False


def _evaluate_and_group(expr: str, ctx: ScopeContext) -> bool:
    if not expr:
        return False
    parts = [p.strip() for p in expr.split("+") if p.strip()]
    if not parts:
        return False
    return all(_evaluate_atom(p, ctx) for p in parts)


def _evaluate_atom(atom: str, ctx: ScopeContext) -> bool:
    if atom in ("always", "manual"):
        return atom == "always"
    if ":" not in atom:
        return False
    key, value = atom.split(":", 1)
    key, value = key.strip().lower(), value.strip().lower()
    ctx_value = _ctx_get(ctx, key)
    if ctx_value is None:
        return False
    if isinstance(ctx_value, (list, tuple)):
        return value in {str(v).lower() for v in ctx_value}
    return str(ctx_value).lower() == value


def _ctx_get(ctx: ScopeContext, key: str) -> Any:
    if key == "stage":
        return ctx.stage
    if key == "mode":
        return ctx.mode
    if key == "language":
        return ctx.language
    if key == "is_cross_project":
        return "true" if ctx.is_cross_project else "false"
    if key == "label":
        return ctx.jira_labels
    return None
