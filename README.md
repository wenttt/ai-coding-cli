# ai-coding-cli

> **Status: Design Phase (v0.2 rewrite).** No code yet.
>
> v0.1 prototype (a 1-day ReAct-loop hello world) has been archived at the tag [`v0.1-prototype-archive`](https://github.com/wenttt/ai-coding-cli/tree/v0.1-prototype-archive). It is **not** representative of the intended design.

A production-grade AI Coding Agent that drives software development from a Jira ticket through to deploy, designed to operate in restricted corporate environments without depending on third-party IDE Agents.

This rewrite is being done with the rigor required for real production deployment: every architectural decision documented in an ADR, designed before implemented, reviewed before built.

## Where we are right now

```
Phase 0 — Design (current)
└── 24 ADRs to write, review, accept before any code is written
    Estimated 4 weeks.

Phase 1 — Storage Foundation
Phase 2 — Agent Foundation MVP
Phase 3 — Context + Compactor
Phase 4 — Memory + RAG + Graph + Governance
Phase 5 — Skill Loader
Phase 6 — Guardrail
Phase 7 — Business Pipeline (migrate from ai-coding-workflow)
Phase 8 — Production readiness

Total runway: ~5.5 months for production-ready v0.2.
```

## Documents to read

| Document | Status |
|---|---|
| [docs/adr/](./docs/adr/) | ADR index — start here |
| [docs/adr/0001-project-vision-scope-constraints.md](./docs/adr/0001-project-vision-scope-constraints.md) | First ADR — vision + scope + constraints |
| docs/architecture/ | (later — architecture diagrams + module specs) |
| docs/api/ | (later — Phase 1+) |

## Related repos

- [ai-coding-workflow](https://github.com/wenttt/ai-coding-workflow) — the v0.1 business-pipeline reference implementation. Its tools, schemas, and templates will be migrated into this repo during Phase 7.
- v0.1 prototype tag: [`v0.1-prototype-archive`](https://github.com/wenttt/ai-coding-cli/tree/v0.1-prototype-archive)

## License

MIT.
