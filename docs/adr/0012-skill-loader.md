# ADR-0012: Skill Loader

## Status

Proposed

## Date

2026-05-19

## Context

Specify the Skill subsystem: file format, discovery, three-level precedence, on-demand loading API, integration with Agent Core and Context Layer, Claude Code compatibility.

## Decision

### What is a Skill

A Skill is a named, versioned bundle of procedural instructions the agent can load on demand. Typical content: "how to write a backend feature in this codebase," "how to run a 6-pass code review," "how to triage a production bug." Skills are written in Markdown and stored as files.

Skills replace what would otherwise live in the System Prompt or be inlined by each StageHandler. By loading only what's needed, total tokens stay low.

### File format

```markdown
---
name: mcp-self-review
description: 6-pass code review covering design alignment, defects, engineering judgment, implementer flags, honesty, operability.
version: 1.2.0
scope: stage:self-review                # see "Scope expressions" below
tools_allowed: [git_diff, read_repo_file, find_relevant_modules]
tools_required: [git_diff]
max_skill_tokens: 8000                  # advisory; loader warns if exceeded
metadata:
  author: ai-coding-cli
  tags: [review, quality-gate]
---

# Self-review for the AI Coding Workflow pipeline

## When to use

This skill is invoked at Stage 3 (Self-Review)…

## Procedure

### Pass 1: Design alignment
Read the design Issue's frontmatter `affected_modules`. Run `git_diff(from_ref="main")` and verify each changed path is in the list…

### Pass 2: Defects (Sev-1 / Sev-2 / Sev-3)
…

## Output

Produce a Markdown report grouped by pass…
```

Frontmatter fields:

| Field | Type | Required | Purpose |
|---|---|---|---|
| `name` | str | yes | unique identifier (kebab-case) |
| `description` | str | yes | one-line summary used in the Skill index |
| `version` | semver str | yes | for compatibility / drift detection |
| `scope` | str | yes | scope expression (see below) |
| `tools_allowed` | list[str] | no | the LLM is restricted to these tools while this skill is the active focus (defaults to all available) |
| `tools_required` | list[str] | no | the loader rejects loading this skill if these tools aren't registered |
| `max_skill_tokens` | int | no | advisory cap on body size |
| `metadata` | dict | no | free-form |

Body is plain Markdown. The body becomes a Tier 2 Static Prefix section (if loaded at conversation start) or a Tier 3 system message (if loaded mid-loop), per ADR-0010.

### Scope expressions

The `scope` field declares when a skill is _eligible_ for automatic preloading by the PipelineOrchestrator. Expressions:

- `stage:design` — preload during the design stage
- `stage:self-review` — preload during self-review
- `stage:implement` + `language:python` — preload during implementation when the workspace is Python
- `mode:greenfield` — preload during greenfield design only
- `always` — preload for every Conversation in this Session
- `manual` — never auto-preloaded; only via explicit `load_skill` tool call

Scope is a boolean expression over `stage`, `mode`, `language` (detected from workspace), `is_cross_project`, custom labels in Jira ticket. Parsed by a small expression evaluator (no full DSL — labels separated by `+` for AND, `,` for OR).

### Three-level precedence

```
1. Workspace-level:    {workspace_root}/.ai-coding-cli/skills/{name}/SKILL.md   (highest precedence)
2. User-level:         ~/.config/ai-coding-cli/skills/{name}/SKILL.md
3. Package-bundled:    src/ai_coding_cli/foundation/skills/_builtin/{name}/SKILL.md  (lowest)
```

If the same `name` appears at multiple levels, the higher-precedence one wins entirely (no merging). The skill index records which level each loaded skill came from.

### Claude Code compatibility

The package also discovers skills from Claude Code's convention paths:

```
4. Claude Code project: {workspace_root}/.claude/skills/{name}/SKILL.md
5. Claude Code user:    ~/.claude/skills/{name}/SKILL.md
```

These are merged into the same name-space as the native paths. Claude Code skills MAY omit some frontmatter fields; the loader applies defaults (`scope: manual`, `version: 0.0.0` if missing).

Precedence:

```
workspace .ai-coding-cli > workspace .claude > user .ai-coding-cli > user .claude > package builtin
```

This lets teams adopt the project's skills alongside their existing Claude Code skill library without renaming.

### Skill index

`SkillLoader.scan()` runs at daemon startup and on workspace open. Output:

```python
@dataclass(frozen=True)
class SkillIndexEntry:
    name: str
    description: str
    version: str
    scope: str
    source_level: Literal["workspace", "workspace_claude", "user", "user_claude", "builtin"]
    file_path: Path
    body_token_estimate: int       # rough count from string length / 4

class SkillIndex:
    entries: list[SkillIndexEntry]
    by_name: dict[str, SkillIndexEntry]
```

The index is exposed to the LLM as part of Static Prefix:

```
[AVAILABLE SKILLS]
- mcp-design-brownfield: Brownfield design generator. Reads find_relevant_modules + reference files + template.
- mcp-design-greenfield: Greenfield design with tech-stack decision matrix.
- mcp-self-review: 6-pass code review.
- mcp-investigate: Systematic root-cause investigation with 4-step method.
- ... (one line per skill)
[/AVAILABLE SKILLS]
```

Only the `description` fields show — not the body. The LLM uses this list to decide whether to call `load_skill(name)`.

### Auto-preloading at Conversation start

When `PipelineOrchestrator` starts a Conversation:

```python
async def _select_skills_to_preload(stage, mode, ticket, workspace) -> list[str]:
    """Evaluate each indexed skill's scope expression against the current context.
    Return names matching scope (auto-preload candidates), in declared scope order."""

preload = await skill_loader._select_skills_to_preload(...)
for name in preload:
    await skill_loader.load_into_static_prefix(name, session)
```

The default preload set per stage:

| Stage | Preloaded skills |
|---|---|
| Design (brownfield) | `mcp-design-brownfield` |
| Design (greenfield) | `mcp-design-greenfield` |
| Design (rework) | `mcp-design-revise` |
| Implementation (backend) | `mcp-implement-backend` |
| Implementation (frontend) | `mcp-implement-frontend` |
| Implementation (db) | `mcp-implement-db` |
| Self-Review | `mcp-self-review` |
| Test-Write | `mcp-test-write` |
| Test-Run | `mcp-test-run` |
| Deploy | `mcp-deploy` |
| Doc-Update | `mcp-doc-update` |
| Investigate (cross-stage) | `mcp-investigate` |

Teams can override by creating same-named skills at workspace or user level. They can also add `scope: always` skills (e.g., a company-wide style guide) and those automatically preload for every Conversation.

### `load_skill` tool

```python
@tool(name="load_skill", description="Load the body of a Skill by name. Use this when the available-skills list suggests a skill that would help with the current task. Returns the skill's procedural instructions.")
async def load_skill(name: str) -> dict:
    """Returns {name, version, body, source_level, tokens}."""
```

The tool:

1. Looks up `name` in the Skill Index.
2. Reads and parses the file.
3. Validates `tools_required` are registered (else returns an error result).
4. Returns the body content (and metadata) as a structured tool result.

The Agent Core receives the tool result and calls `ContextBuilder.inject_loaded_skill(messages, loaded_skill)` to prepend a `[SKILL: name]` system message to Dynamic Context (per ADR-0010).

Mid-loop loaded skills do NOT modify Static Prefix (preserves cache validity, per ADR-0010).

### Idempotent loading

If a skill is already loaded (either pre-loaded at Conversation start or previously loaded mid-loop), calling `load_skill(name)` again returns a short message like `{name, version, status: "already_loaded", referenced_in_message_index: N}` without re-injecting the body. The LLM is reminded the content is already in context.

### Versioning + drift detection

When the LLM calls `load_skill`, the response includes the skill's `version`. Mismatch between the version in Static Prefix and the version in a customized file is logged as a warning (`skill.version_drift` event). Behavior:

- Workspace override of a built-in: drift is silent and expected; just logged at INFO.
- Workspace override falls behind a built-in by > 1 minor version: WARN level; surfaces in Dashboard.
- Workspace override falls behind by major version: ERROR level; Dashboard prompts review.

The loader does not block on drift; it informs.

### `tools_allowed` / `tools_required`

`tools_required`: list of tool names that MUST be registered in the ToolRegistry. If missing, the skill cannot be loaded — the load_skill tool returns an error, surfacing the misconfiguration.

`tools_allowed`: advisory — the loader does NOT actually restrict tool calls. The skill body is expected to say "you may call: …" in its procedure. Enforcement is out of scope for v0.2; the field exists to power Dashboard warnings when an LLM calls a tool outside the allowed set during a skill-driven turn.

### CLI commands

```
ai-coding skills list                              # show the index
ai-coding skills show <name>                       # print the full skill content
ai-coding skills validate                          # check all skills parse + tools_required exist
ai-coding skills eject <name>                      # copy builtin to workspace for editing
ai-coding skills which <name>                      # show which level the skill comes from
```

### Skill development conventions

(For project authors of built-in skills; not enforced.)

1. Skills have a single H1 + clear `## When to use`, `## Procedure`, `## Output` sections.
2. Bodies are < 8k tokens (~32KB).
3. Reference tools by name in backticks; do not invent tools.
4. Reference other skills with `[SKILL:name]` syntax — the LLM understands the cross-reference but the loader does not auto-load chained skills (the LLM chooses).
5. Skills do NOT have their own tools; they describe how to use the tools already in the registry.

### Failure handling

| Failure | Behavior |
|---|---|
| Skill file missing for a name in the index (e.g., deleted after scan) | Return error result from `load_skill`; rescan index in background |
| Skill frontmatter malformed | Skip during scan; log error; do not include in index |
| `tools_required` not satisfied | `load_skill` returns error with details |
| Body exceeds `max_skill_tokens` advisory cap | Load anyway; log warning; surface in Dashboard |
| Two skills with the same name at the SAME precedence level (multiple files in the same directory) | Skip later one; log error |

## Consequences

- Skills are versioned, scoped, and discoverable; the LLM can choose what to load.
- Three-level precedence + Claude Code compatibility lets teams adopt the project without throwing away their existing skill library.
- Auto-preloading per stage keeps the LLM focused; mid-loop loading covers cases where the LLM realizes it needs adjacent expertise.
- Dynamic Context placement of mid-loop skills preserves prompt cache, at the cost of less coherent message threading (an acceptable trade-off documented in ADR-0010).
- `tools_required` ensures skills fail fast when run against an under-provisioned ToolRegistry.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Whether `scope` should support priority ordering when multiple skills match (currently scope-order = declaration-order in the index) | Phase 5 implementation |
| Q2 | `tools_allowed` enforcement: hard-block tool calls outside the allowed set, or stay advisory | Phase 5 / ADR-0025 (Guardrail) |
| Q3 | Skill composition — can one skill `[INCLUDE: another-skill]` to inline content? | Phase 5; defer until needed |
| Q4 | Skill version negotiation when a workspace customization lags the built-in by major versions and breaks scope expressions | Phase 5 implementation |

## References

- ADR-0008 Session + Conversation Model
- ADR-0009 Agent Core (consumes load_skill tool)
- ADR-0010 Context Layer (Static Prefix vs Dynamic injection)
- ADR-0013 Tool Registry (where load_skill is registered; planned)
- ADR-0025 Guardrail Layer (may enforce tools_allowed)

## Reviewers

- [ ] Taven
