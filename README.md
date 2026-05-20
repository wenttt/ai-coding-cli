# ai-coding-cli

> **v0.2 Lite — single-user local deployment**
>
> Phase 0 design complete (27 ADRs Accepted as Standard profile reference for v0.3+).
> Lite profile (ADR-0030) is the v0.2 ship target: one developer, one Python process, one SQLite file.

A self-contained AI Coding Agent that drives software development from a Jira ticket through to deploy. Runs locally on the developer's own machine; no centralized service.

## What v0.2 Lite ships

| Surface | |
|---|---|
| **CLI** | `ai-coding chat "start KAN-4"` — issue commands; reads + writes everything |
| **Local Web Dashboard** | `ai-coding web` — read-only monitoring on `127.0.0.1:8080` |
| **Pipeline** | 6 stages (Design → Implement → Self-Review → Test → Deploy → Doc Update), Jira-state driven |
| **Storage** | SQLite + sqlite-vec at `~/.ai-coding-cli/state.db`; operation logs as Markdown in workspace |
| **LLM** | Any OpenAI-compatible endpoint (company gateway / OpenAI / Anthropic shim) |
| **Tools** | Native Jira, GitHub, git, repo, tests; plus MCP bridge for external MCP servers |

## Roadmap

```
✅ Phase 0 — Design  (27 ADRs Standard + ADR-0030 Lite profile, all Accepted)
🔵 Phase 1 Week 1 — Foundation pieces                  ← in progress
⬜ Phase 1 Week 2 — Agent Core + Context + Compactor
⬜ Phase 1 Week 3 — Pipeline + Stage 1 + Jira polling
⬜ Phase 1 Week 4 — CLI + Web Dashboard + Skill Loader + Guardrails
⬜ Phase 1 Week 5 — MCP bridge
⬜ Phase 2+      — Standard profile features (PG, Neo4j, Memory, ...)
```

Lite end-to-end target: 5 weeks for a working pipeline on a real Jira ticket.

## Install (when v0.2 ships)

```bash
pip install ai-coding-cli
cp .env.example .env
# Edit .env: fill in 8 required fields (Jira, GitHub, LLM endpoints + tokens)
ai-coding init                    # scaffold workspace .ai-coding-cli/ + conventions.md
ai-coding daemon start            # background daemon (or run inline for one-shot)
ai-coding chat "start KAN-4"
```

## Documents to read in order

| Document | What it covers |
|---|---|
| [docs/adr/](./docs/adr/) | ADR index — every architectural decision |
| [ADR-0030](./docs/adr/0030-v0.2-lite-profile.md) | Lite profile (what v0.2 actually ships) |
| [ADR-0001](./docs/adr/0001-project-vision-scope-constraints.md) | System overview (Standard profile vision) |
| [ADR-0003](./docs/adr/0003-pipeline-business-model.md) | 6-stage pipeline state machine |
| [ADR-0028](./docs/adr/0028-jira-workflow-specification.md) | Reference Jira workflow (for admins) |

## Related repositories

- [ai-coding-workflow](https://github.com/wenttt/ai-coding-workflow) — v0.1 reference implementation. Tools, schemas, templates being ported into this repo during Week 1.

## License

MIT.
