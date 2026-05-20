"""Foundation layer: generic Agent runtime, reusable across applications.

Submodules:
- agent: ReAct loop, lifecycle hooks
- config: Pydantic-Settings models
- context: three-tier Context Layer
- compactor: conversation compaction
- errors: Agent error taxonomy
- guardrail: input / output / action guardrails
- llm: provider-agnostic LLM adapter
- memory: (deferred to Standard profile)
- observability: events, logging, metrics
- session: Session + Conversation persistence
- skills: skill discovery + loader
- storage: SQLite + sqlite-vec
- tools: tool registry + native tools + MCP bridge

Per ADR-0002: `foundation/` MUST NOT import from `application/` or `web/`.
"""
