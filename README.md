# ai-coding-cli

> **Status: Phase 0 complete.** All 27 ADRs Accepted. Phase 1 (implementation) unblocked.
>
> v0.1 prototype is archived at the tag [`v0.1-prototype-archive`](https://github.com/wenttt/ai-coding-cli/tree/v0.1-prototype-archive). v0.2 is being built from scratch against the design in `docs/adr/`.

A production-grade AI Coding Agent that drives software development from a Jira ticket through to deploy, designed to operate in restricted corporate environments without depending on third-party IDE Agents.

Every architectural decision is documented in an ADR, designed before implemented, reviewed before built.

## Roadmap

```
✅ Phase 0 — Design  (27 ADRs, all Accepted)
⬜ Phase 1 — Storage Foundation                (PostgreSQL + Neo4j + migrations)
⬜ Phase 2 — Agent Foundation MVP              (Agent Core + LLM Adapter + Tool Registry)
⬜ Phase 3 — Context + Compactor               (three-tier Context, Micro/AutoCompact)
⬜ Phase 4 — Memory + RAG + Graph + Governance (4-layer Memory, hybrid retrieval)
⬜ Phase 5 — Skill Loader                      (auto-preload + load_skill tool)
⬜ Phase 6 — Guardrail                          (Input + Output + Action layers)
⬜ Phase 7 — Business Pipeline                 (migrate from ai-coding-workflow)
⬜ Phase 8 — Production readiness              (perf, monitoring, ops docs)
```

Total runway: ~5.5 months for production-ready v0.2.

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
