# Analyst Runtime

Analyst Runtime is our private, product-neutral agent execution engine for
enterprise applications. It owns model routing, tool execution, conversation
state, workspace prompts, optional runtime profiles, and authenticated product
gateway integration.

The public integration name is **Analyst Runtime**. The Python package is
`analyst_runtime`, the command is `analyst-runtime`, and product-specific
identity and behavior live in a supplied workspace rather than in the core.

## Origin and attribution

Analyst Runtime began as a deeply modified derivative of
[HKUDS/nanobot](https://github.com/HKUDS/nanobot), an open-source,
self-hosted personal AI agent framework. We are grateful to the NanoBot
maintainers and contributors for the original architecture and implementation.

This repository preserves the original MIT license and copyright notice. It is
an independently maintained private derivative and is not affiliated with or
endorsed by the NanoBot project.

## Runtime boundary

- `analyst_runtime/` contains the generic Python runtime.
- `workspaces/` contains copyable product prompt and runtime-profile templates.
- `bridge/` contains optional channel bridges.
- `tests/` contains the runtime contract and regression suite.

Analyst Runtime is the only agent orchestrator in consuming products. It owns every model
call, reasoning step, tool decision/execution, retry, session update, workspace read, and
final message. UI frameworks may adapt its events for display but must not wrap it in a
second agent loop.

Run-scoped `steer_request` control messages are accepted only while that run is active. The
Runtime queues the instruction and applies it after the current tool-call batch (or before a
text-only response becomes final), persists it as user input, acknowledges `steer_applied`,
and then continues the same agent loop. The session also persists a bounded set of applied
`steer_id` values so retries and status lookups are idempotent after transport disconnects. It
reports a received command as `pending` until the next safe boundary and never starts a parallel
run for steering.

Vercel AI SDK (`ai` and `@ai-sdk/*`) is forbidden in this repository, including the optional
Node channel bridges. Python provider access remains behind `LLMProvider`; product model
selection arrives as a trusted profile ID and is resolved here to its provider/model. BYOK
verification and model calls both remain behind the Runtime provider port. BYOK secrets must
be removed from inbound metadata before any log, session event, trace, or outbound message.

The active product workspace is supplied by the consuming repository. Its
`SOUL.md` defines who the agent is and `AGENTS.md` defines how it works. Runtime state,
customer data, prompts, and artifacts do not belong in this repository.

Linghui runs the `trusted-analysis` tool profile. It exposes workspace-scoped file and exec
tools, Web/Firecrawl retrieval, and `message`. It does not expose MCP, Composio, external
business integrations, audio/vision tools, `spawn`, `cron`, or manufacturing-semantics tools.
Semantic confirmation is an ordinary system-prompt rule and
continues through the same Analyst Runtime conversation.

## Development

```bash
uv sync
uv run pytest
```

Build from a consuming repository whose root contains both `analyst-runtime/`
and `workspace/`:

```bash
docker build -f analyst-runtime/Dockerfile -t analyst-runtime:latest .
```
