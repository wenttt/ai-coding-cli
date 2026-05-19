# ai-coding-cli

> A self-contained AI Coding Agent CLI. No IDE Agent required.

Drives the [ai-coding-workflow](https://github.com/wenttt/ai-coding-workflow) pipeline (Jira → design → implement → review → test → deploy) from a single command line. ReAct loop + OpenAI-compatible LLM + MCP tool calling.

## Why this exists

The original architecture used VS Code Copilot / Claude Code / Roo Code as the Agent that talked to the `ai-coding-workflow` MCP server. That works on dev-friendly machines, but in restricted corporate environments (locked VS Code forks, no third-party extensions, custom Copilots that don't speak MCP), the Agent layer becomes the failure point.

`ai-coding-cli` is the Agent layer rebuilt as a standalone Python CLI:

- **Talks to ANY OpenAI-compatible LLM endpoint** — your company's internal LLM gateway, Anthropic's OpenAI shim, OpenAI direct, anything that speaks the standard.
- **Speaks MCP** — calls `ai-coding-workflow` as a subprocess and uses its tools.
- **No IDE dependency** — runs anywhere Python runs.
- **Same pipeline rules** — the system prompt embeds the same orchestration rules that `.github/copilot-instructions.md` and `.roorules` used. Stage 1 is Issue-only. Operation logs are mandatory. 3-strike escalation. Etc.

```
You (terminal)
   |  ai-coding "start working on KAN-4"
   v
[ai-coding-cli]
   ReAct loop -> LLM (OpenAI-compat) -> tool calls -> repeat -> final answer
   |
   v
[ai-coding-workflow MCP server]
   Jira / GitHub / git / repo / tests / operation logs
```

## Install

```bash
git clone https://github.com/wenttt/ai-coding-cli.git
cd ai-coding-cli
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

You also need `ai-coding-workflow` installed somewhere (separate clone + its own venv):

```bash
# In a different directory
git clone https://github.com/wenttt/ai-coding-workflow.git
cd ai-coding-workflow
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Note the path to `ai-coding-workflow/.venv/bin/python` — you'll point at it next.

## Configure

```bash
cp .env.example .env
# Edit .env
```

Required minimum:

```
OPENAI_BASE_URL=https://llm.your-company.com/v1
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4o
MCP_SERVER_COMMAND=/path/to/ai-coding-workflow/.venv/bin/python
MCP_SERVER_ARGS=-m ai_coding_workflow.server
MCP_SERVER_ENV_JIRA_BASE_URL=https://your.atlassian.net
MCP_SERVER_ENV_JIRA_EMAIL=...
MCP_SERVER_ENV_JIRA_API_TOKEN=...
MCP_SERVER_ENV_GITHUB_TOKEN=...
MCP_SERVER_ENV_GITHUB_DEFAULT_OWNER=...
MCP_SERVER_ENV_GITHUB_DEFAULT_REPO=...
MCP_SERVER_ENV_WORKSPACE_PATH=/path/to/your/sandbox/repo
```

Anything starting with `MCP_SERVER_ENV_` is forwarded to the MCP server subprocess (with the prefix stripped). This means you keep ALL config in this one `.env`.

## Use

```bash
ai-coding chat "start working on KAN-4"
```

The agent:
1. Loads the system prompt (pipeline rules — see `src/ai_coding_cli/prompts/system.md`)
2. Spawns the `ai-coding-workflow` MCP server, discovers its ~35 tools
3. Sends your instruction to the LLM with the tools attached
4. ReAct loop: LLM thinks → calls tools → reads results → repeats → final answer
5. Prints what was done

Other commands:

```bash
ai-coding show-prompt    # Print the bundled system prompt
ai-coding version
```

## How it follows the pipeline

The bundled system prompt embeds the same rules used by the Copilot/Roo Code variants:

- When the user mentions a Jira key → call `get_workflow_state` first to detect the current stage
- One stage per invocation. Stop after completing it.
- Stage 1 is Issue-only (no branches / PRs in design phase).
- Operation logs are mandatory.
- 3-strike retry → escalate.
- Cross-project tickets use contract-first design.

To customize, override with `--system /path/to/your-prompt.md`.

## Project layout

```
ai-coding-cli/
├── src/ai_coding_cli/
│   ├── cli.py              # Typer entry point
│   ├── agent.py            # ReAct loop
│   ├── llm.py              # OpenAI-compatible wrapper
│   ├── config.py           # .env loading
│   ├── tools/
│   │   └── mcp_client.py   # MCP subprocess + tool dispatch
│   └── prompts/
│       └── system.md       # Pipeline rules
├── tests/
├── docs/
│   └── ARCHITECTURE.md
└── pyproject.toml
```

## Status

Day-1 MVP. Working pieces:

- ✅ OpenAI-compatible LLM call with tool support
- ✅ MCP server spawning + tool discovery + tool dispatch
- ✅ ReAct loop with turn limit
- ✅ System prompt embedding the pipeline rules
- ✅ CLI entry point

Not yet:

- ❌ Context window management (long conversations may overflow — for now, one-shot only)
- ❌ Memory / persistence across invocations
- ❌ Skill loading on demand
- ❌ Human-in-the-loop confirmation for destructive operations
- ❌ Streaming output (Agent waits for full response before printing)

See `docs/ARCHITECTURE.md` for the longer-term shape.

## Related projects

- [ai-coding-workflow](https://github.com/wenttt/ai-coding-workflow) — the MCP server this CLI drives. The "what" of the pipeline (tools + skills + operation log schema).

## License

MIT.
