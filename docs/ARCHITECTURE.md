# Architecture

## Three layers

```
┌─────────────────────────────────────────────────────────┐
│  cli.py    (Typer)                                      │
│  - Parse user instruction                               │
│  - Load .env + config                                   │
│  - Call agent.run_sync()                                │
└─────────────────────────────────────────────────────────┘
                       |
                       v
┌─────────────────────────────────────────────────────────┐
│  agent.py    (ReAct loop)                               │
│  - Open MCP session                                     │
│  - Discover tools                                       │
│  - Loop:                                                │
│      llm.complete(messages, tools)                      │
│      -> if tool_calls: dispatch + append, continue      │
│      -> else: return final content                      │
│  - Bounded by agent_max_turns                           │
└─────────────────────────────────────────────────────────┘
                |                              |
                v                              v
┌─────────────────────────────┐  ┌─────────────────────────┐
│  llm.py    (OpenAI SDK)     │  │  tools/mcp_client.py    │
│  - Pointed at any            │  │  - Subprocess MCP        │
│    OpenAI-compat endpoint    │  │    server over stdio     │
│  - Streaming-aware           │  │  - list_tools ->         │
│  - Tool calling              │  │    OpenAI format         │
│                              │  │  - call_tool -> string   │
└─────────────────────────────┘  └─────────────────────────┘
                                            |
                                            v
                                ┌─────────────────────────┐
                                │  ai-coding-workflow      │
                                │  MCP server              │
                                │  (separate process)      │
                                │  ~35 tools               │
                                └─────────────────────────┘
```

## Why ReAct loop instead of single-turn

The pipeline tasks are inherently multi-step:

```
User: "start working on KAN-4"

Agent must:
  1. read_jira_ticket("KAN-4")            <- tool call 1
  2. analyze_repo_state()                  <- tool call 2 (reads result of #1)
  3. find_design_issue_for_jira("KAN-4")  <- tool call 3
  4. find_relevant_modules([...])          <- tool call 4 (keywords from #1)
  5. <reason about design>
  6. create_design_issue(...)             <- tool call 5
  7. write_operation_log(...)             <- tool call 6
  8. <final answer to user>
```

One LLM call can return multiple parallel tool calls (steps 1+2+3 can sometimes be parallel), but logically dependent steps need sequential reasoning. The ReAct loop accommodates both.

## Why OpenAI-compatible (not native Anthropic)

The OpenAI chat completions protocol is the de facto standard. Most enterprise LLM gateways implement it:

- Internal company "Copilot" backends
- Open-source serving stacks (vLLM, TGI, Ollama, LM Studio)
- All major providers behind OpenAI-compat shims (Anthropic, Together, Groq, Fireworks)

Targeting OpenAI-compat means we work everywhere with one client library.

Trade-off: tool calling protocol details differ slightly across providers. We rely on the OpenAI SDK's normalized shape; this works against the major providers, but edge cases on niche gateways may need patches.

## Why spawn MCP server as subprocess (not HTTP)

The MCP spec has two transports:
- **stdio** — server is a child process, communication via stdin/stdout
- **SSE / HTTP** — server is a long-running HTTP service

`ai-coding-workflow` already exposes the stdio transport (that's how Claude Code / Cursor / Roo Code connect to it). Reusing it means:

- No HTTP server deployment needed
- Same operational model as other MCP clients
- Subprocess lifecycle is bounded by the CLI invocation
- One config path (the MCP server's env vars come from this CLI's `.env` via `MCP_SERVER_ENV_*`)

When this CLI grows into a long-lived daemon (REPL mode, web UI), we'll add HTTP transport as an option. For now stdio is the right cost/value.

## Configuration philosophy

**One .env file**. The CLI's `.env` holds everything: LLM credentials, agent settings, and (via `MCP_SERVER_ENV_*` prefix) the env vars to forward to the spawned MCP server. No parallel config files to maintain.

## Comparison to other Agent runtimes

| Runtime | Where it runs | LLM control | MCP support | Custom system prompt | Status in this project |
|---|---|---|---|---|---|
| GitHub Copilot Chat | VS Code | Company / public | Some versions | `.github/copilot-instructions.md` | Tried — blocked in corp env |
| Claude Code | Desktop / CLI | Anthropic | Native | `CLAUDE.md` | Can't install in corp |
| Cursor | Cursor IDE | Configurable | Yes | `.cursorrules` | Can't install in corp |
| Roo Code / Cline | VS Code extension | Configurable | Yes | `.roorules` | Tried — SSL + reliability issues |
| **ai-coding-cli (this)** | **anywhere Python runs** | **OpenAI-compat — anything** | **client** | **bundled system.md** | **the current approach** |

## What's deliberately NOT in v0.1

- **Context window management** — large conversations will hit the model's context limit. Adding MicroCompact / AutoCompact style truncation is on the roadmap but not required for one-shot use.
- **Memory** — no persistence between invocations. Each `ai-coding chat "..."` starts fresh.
- **Skill loading** — the system prompt is monolithic. A proper skill loader (`load_skill("mcp-design-brownfield")`) is on the roadmap.
- **Human-in-the-loop** — every tool runs without confirmation. For destructive ops (push, deploy) we'll add an interactive guard.
- **Streaming output** — agent waits for the full LLM response before printing. Streaming improves perceived latency but complicates tool-call dispatch; deferred.

## Open architecture questions (decide with usage)

- **Tool result truncation policy** — we cap each tool result at 50,000 chars. For very long file reads, this loses content. Better: ranked summarization.
- **Tool call parallelism** — when the LLM emits multiple tool_calls in one response, do we run them concurrently or sequentially? Currently sequential (simpler, less surprising). Concurrent is faster but introduces race conditions if two tools touch the same file.
- **Multi-turn conversation** — should the CLI keep a session log on disk and let you `--continue` from last invocation? Useful, but requires resolving how to handle stale tool results and changed external state.
