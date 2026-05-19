# ADR-0007: Template Library

## Status

Proposed

## Date

2026-05-19

## Context

Specify the design-document templates used by Stage 1 handlers: directory layout, file format, content scaffold per template, loading order, customization hook.

## Decision

### Directory layout

Templates ship with the package at `src/ai_coding_cli/application/pipeline/templates/`:

```
templates/
├── brownfield/
│   ├── user_story.md
│   ├── task.md
│   ├── sub_task.md
│   ├── epic.md
│   └── cross_project.md
├── greenfield/
│   ├── new_project.md
│   ├── new_module.md
│   └── new_service.md
└── shared/
    ├── footer.md                       # "How to review" footer included in every Issue body
    └── frontmatter_examples.md         # reference for agents
```

Templates are loaded via `importlib.resources` so they remain accessible after `pip install` and inside a packaged wheel.

### File format

Each template is a Markdown file with three regions in order:

```markdown
---
# region 1: frontmatter scaffold (YAML, with placeholders)
jira_key: "{{ jira_key }}"
mode: brownfield
ticket_type: user_story
risk_level: "{{ risk_level | default('medium') }}"
ac: []
affected_modules: []
---

# region 2: body scaffold (Markdown, with section anchors and guidance comments)

# Design: {{ ticket.summary }}

> Jira: [{{ jira_key }}]({{ ticket.url }})

## Background

<!-- guidance: 2-3 paragraphs on what the system does today around this feature.
     Pull from find_relevant_modules results. -->

## Story

<!-- guidance: "As a {persona}, I want {capability}, so that {value}".
     For non-user_story templates, replace with "Goal" + problem statement. -->

## Acceptance criteria

<!-- guidance: GIVEN/WHEN/THEN form. Mirror to frontmatter `ac` array. -->

1. **GIVEN** … **WHEN** … **THEN** …

## Affected modules

<!-- guidance: List paths under workspace_root. Mirror to frontmatter `affected_modules`. -->

## Design

<!-- guidance: The technical content. Diagrams, data flow, API shape,
     persistence, edge cases, error handling. -->

## Test plan

<!-- guidance: What Stage 4 should write. Brief; full plan is in Stage 4 output. -->

## Open questions

<!-- guidance: Anything that requires reviewer input. -->
```

The guidance comments (`<!-- guidance: … -->`) are read by the agent and stripped before the body is written to the Issue. The agent fills sections by drawing on Jira ticket data + retrieved code + prior operation logs.

### Templating language

**Jinja2** with strict undefined handling (`StrictUndefined`). Placeholders use `{{ var }}`. Conditional sections use `{% if %}` / `{% endif %}`. Loops use `{% for %}` / `{% endfor %}`.

Available variables when rendering:

| Variable | Type | Source |
|---|---|---|
| `jira_key` | str | StageContext.jira_key |
| `ticket` | dict | full Jira ticket JSON (read-only) |
| `mode` | str | "brownfield" \| "greenfield" |
| `ticket_type` | str | normalized issuetype |
| `is_cross_project` | bool | from TicketRouting |
| `affected_projects` | list | from TicketRouting (cross_project template only) |
| `workspace_root` | Path | resolved absolute path |
| `repo_context` | list[FileContext] | output of find_relevant_modules + read_repo_file |
| `linked_issues` | list | linked Jira tickets' summaries |
| `prior_operation_logs` | list | last 3 logs in this stage chain |
| `today` | date | UTC date |

Templates do NOT escape Markdown (Markdown is the target). Jinja autoescape is off.

### Template contents (per type)

#### brownfield/user_story.md

Sections:
- Background
- Story (as/want/so that)
- Acceptance criteria (GIVEN/WHEN/THEN, mirrors `ac`)
- Affected modules
- Design
- Test plan
- Open questions

#### brownfield/task.md

Sections:
- What (concrete one-paragraph description)
- Why (motivation; bug repro link if applicable)
- Current state (file:line references)
- Proposed change (diff-level outline)
- Acceptance criteria
- Affected modules
- Risk + rollback
- Test plan
- Open questions

#### brownfield/sub_task.md

Sections:
- Scope (in / out)
- Inherited context from parent (reference parent design Issue)
- Implementation outline
- Acceptance criteria
- Affected modules
- Open questions

Frontmatter includes `parent_jira_key` field.

#### brownfield/epic.md

Sections:
- Goal
- Current architecture (relevant portions)
- Proposed architectural change
- Sub-task breakdown (table; mirrored to frontmatter `sub_tasks`)
- Acceptance criteria (Epic-level)
- Cross-cutting concerns (migrations, feature flags, telemetry, security, performance)
- Risk + phasing
- Open questions

#### brownfield/cross_project.md

Sections:
- Goal (user-visible capability)
- Affected projects (table; mirrors frontmatter `affected_projects`)
- Implementation order (rationale)
- **Contract** (mandatory; OpenAPI / Protobuf / GraphQL fragment)
  - Endpoints + schemas
  - Error codes
  - Versioning
  - Source of truth file path (e.g., `contracts/{KEY}.yaml`)
- Per-repo implementation outline (one subsection per affected project)
- Risk + rollback (with feature flag pattern)
- Acceptance criteria
- Cross-project test plan (unit / contract / E2E)
- Open questions

#### greenfield/new_project.md

Sections:
- Project goal
- Requirements summary (functional, NFR, constraints, out-of-scope)
- **Tech stack decision** (mandatory matrix; mirrored to frontmatter `proposed_stack`)
  - Language, Framework, Database+ORM, Cache, API style, Auth, Deployment, CI/CD, Testing, Observability
  - Each row: Recommended + Alternatives + Rationale
- High-level architecture (mermaid or ASCII)
- Module breakdown
- Project skeleton (target directory tree)
- Phasing / sub-task breakdown
- Acceptance criteria (project-level)
- Open decisions to defer

#### greenfield/new_module.md

Sections:
- Why a new module
- Module placement (parent repo layout)
- Module structure (target tree)
- Boundaries / interfaces (inbound, outbound, shared types)
- Acceptance criteria
- Risks (coupling pull, test isolation)
- Open questions

#### greenfield/new_service.md

Sections:
- Service summary
- Why a service (independent deployment / operational ownership / tech requirements)
- API surface (table or HTTP/event listing)
- Data model
- Tech stack decision (matrix)
- Deployment shape (runtime, scale, dependencies)
- Observability (logs, metrics, alerts)
- Phasing
- Acceptance criteria
- Open questions

### Footer

`shared/footer.md` is appended to every rendered Issue body:

```markdown
---

> **How to review**
>
> - Comment in this Issue with concerns or questions.
> - Approve by transitioning the Jira ticket to **DESIGN_APPROVED**.
> - Request changes by transitioning to **DESIGN_REWORK**; the agent will read your comments and revise.
> - If this design is fundamentally wrong, transition to **DESIGN_REJECTED** (Jira workflow has no such status; use `state_reason=not_planned` and discuss in Jira).
>
> Up to 3 revision rounds are auto-handled. After that, the pipeline escalates and a human must take over.
>
> _Generated by ai-coding-cli at {{ today }}. Operation log: `{{ operation_log_path }}`._
```

### Loading order

`TemplateLoader.load(name: str) -> Template`:

1. **User override** (highest priority): `{workspace_root}/.ai-coding-cli/templates/{name}.md` (per-project customization checked into the repo).
2. **User home override**: `~/.config/ai-coding-cli/templates/{name}.md`.
3. **Package default**: `importlib.resources.files("ai_coding_cli.application.pipeline.templates") / name`.

The first hit wins. Templates resolve fully (no merging); if a user provides a custom `brownfield/user_story.md`, it replaces the default entirely.

### Rendering

`TemplateRenderer.render(template_name, context) -> RenderedTemplate`:

```python
@dataclass(frozen=True)
class RenderedTemplate:
    frontmatter: dict[str, Any]          # parsed YAML
    body: str                            # Markdown (footer NOT yet appended)
    body_with_footer: str                # body + shared/footer.md rendered
    template_name: str
    template_version: str                # from package metadata or git SHA
```

Render steps:

1. Load template via `TemplateLoader`.
2. Pre-render Jinja with context.
3. Split frontmatter from body.
4. Parse frontmatter as YAML; validate against `DesignFrontmatter` Pydantic model from ADR-0004.
5. Strip `<!-- guidance: … -->` comments from the body.
6. Append rendered footer.

If frontmatter validation fails, the renderer raises `TemplateRenderError`. The agent's caller receives this and may either retry with an updated prompt or escalate.

### Customization workflow

A team customizes a template:

1. `ai-coding templates eject brownfield/user_story.md` copies the package default to `{workspace_root}/.ai-coding-cli/templates/brownfield/user_story.md`.
2. Team edits the local copy.
3. Commits it.
4. Future renders use the local version.

The CLI `ai-coding templates list` shows which templates are currently customized (with paths) and which are using the package default.

### Versioning

Each template ships a `template_version` field (top-level YAML key, ignored in frontmatter validation):

```yaml
---
template_version: "1.0"
jira_key: ...
---
```

When the package adds a non-backward-compatible change to a template (e.g., new mandatory section), the `template_version` increments. The renderer logs a warning if a user-customized template uses an older `template_version` than the package's current default — surfacing the drift without blocking.

### Template authoring rules

(For the project's own template authors; not enforced by code.)

1. Sections are H2 (`##`) and consistent across templates within a category (brownfield vs greenfield).
2. Body MUST mirror frontmatter for any structured field (e.g., `ac`, `affected_modules`, `proposed_stack`).
3. Guidance comments use the exact prefix `<!-- guidance: ` so the stripper regex is reliable.
4. Templates do NOT include footnotes about why the section exists; the section title is enough.
5. Optional sections are explicit in the template (commented placeholder) rather than absent.

## Consequences

- Templates are static text files, easy for non-engineers to review.
- Jinja with `StrictUndefined` catches missing context at render time (better than silent empty sections).
- Three-level loading (workspace / home / package) lets teams customize without forking the project.
- Frontmatter validation at render time prevents downstream stages from receiving malformed inputs.
- Template versioning gives a hook for breaking-change communication, without hard-blocking older customizations.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | How `template_version` is enforced when a customized template falls multiple versions behind (warning vs error vs auto-migrate) | Phase 1 implementation |
| Q2 | Whether templates should support partial includes (e.g., a shared `risk_section.md` referenced from multiple templates) | Phase 1 implementation; defer until a real reuse case appears |
| Q3 | How non-English templates (Chinese-language Jira tickets producing Chinese-language designs) are organized — separate template files per language vs templates that are language-agnostic | Phase 7 (Application) implementation |
| Q4 | Whether templates can declare their own Pydantic validation rules beyond DesignFrontmatter (e.g., a contract template enforcing contract.type is set) | Phase 7 implementation |

## References

- ADR-0001 System Overview
- ADR-0004 Stage 1 Design Flow (consumes rendered templates)
- ADR-0005 Operation Log Schema (operation_log_path appears in rendered footer)

## Reviewers

- [ ] Taven
