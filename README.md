# Analyst Runtime

An open-source Python runtime for running tool-using AI agents inside real products.

Analyst Runtime owns the complete execution path: model routing, reasoning, tool calls and
results, retries, sessions, workspace context, progress events, steering, and final delivery.
Product gateways and user interfaces stay thin; product-specific prompts and data stay in a
workspace outside the runtime.

> **Status:** Alpha. The runtime is already used as an application component, but its public
> APIs and configuration may still change before 1.0.

## Why Analyst Runtime

- **One agent loop:** every model call and tool decision follows the same observable path.
- **Provider-neutral:** providers live behind `LLMProvider`; OpenAI-compatible vendors are
  declarative catalog entries rather than parallel implementations.
- **Product isolation:** identity, instructions, memory, and artifacts live in a supplied
  workspace instead of being compiled into the runtime.
- **Controlled execution:** tool profiles, workspace-scoped file access, authenticated gateway
  metadata, request-scoped credentials, and ordered progress events are runtime concerns.
- **Deployable:** use the CLI directly, run the long-lived gateway, or embed the Python package
  behind your own authenticated application gateway.

## Quick start

Prerequisites: Python 3.11 or 3.12, [uv](https://docs.astral.sh/uv/), and an API key for a
supported model provider.

```bash
git clone https://github.com/seletatech/analyst-runtime.git
cd analyst-runtime
uv sync --extra dev
uv run analyst-runtime onboard
```

`onboard` creates `~/.analyst-runtime/config.json` and a starter workspace. Add a provider key
and set `agents.defaults.model` in that generated file. For example, an OpenRouter setup uses:

```json
{
  "agents": {
    "defaults": {
      "model": "openrouter/openai/gpt-4o-mini"
    }
  },
  "providers": {
    "openrouter": {
      "apiKey": "YOUR_OPENROUTER_KEY"
    }
  }
}
```

These are fragments of the generated configuration; keep its other fields unchanged. Then run
one request:

```bash
uv run analyst-runtime agent -m "Summarize the files in my workspace"
```

Or start an interactive session:

```bash
uv run analyst-runtime agent
```

Run `uv run analyst-runtime --help` to see gateway, channel, provider, and cron commands.

## Architecture

```text
CLI / product gateway / chat channel
                 │
                 ▼
           message bus
                 │
                 ▼
           agent loop ───── session + workspace context
            │     │
            │     └──────── tool registry ── files / exec / web / MCP / integrations
            ▼
         LLMProvider ────── provider catalog ── model APIs
                 │
                 ▼
      ordered progress + final reply
```

| Path | Responsibility |
| --- | --- |
| `analyst_runtime/agent/` | Agent loop, context, routing, steering, memory, and tool profiles |
| `analyst_runtime/providers/` | Stable provider port, shared adapters, and provider catalog |
| `analyst_runtime/agent/tools/` | Tool contracts and built-in tool implementations |
| `analyst_runtime/channels/` | CLI, web-gateway, and chat transport adapters |
| `analyst_runtime/session/` | Durable conversation state |
| `analyst_runtime/config/` | Configuration schema and loading |
| `workspaces/` | Copyable example personas and runtime policy |
| `bridge/` | Optional Node.js channel bridges |
| `tests/` | Contract, regression, security-boundary, and staging end-to-end tests |

The critical dependency direction is:

```text
product UI/gateway → Analyst Runtime → LLMProvider/tool ports → external services
```

Model selection changes routing for a run; it does not create another agent architecture.
Product prompts, customer data, and product-specific semantics belong in the consuming
workspace, not this repository.

## Workspaces and extension points

An active workspace can contain:

- `SOUL.md` — agent identity and communication style
- `AGENTS.md` — operating instructions and domain rules
- `workspace.json` — trusted gateway identity and runtime policy
- `skills/<name>/SKILL.md` — workspace-specific capabilities
- `memory/` and `sessions/` — runtime-owned state

See [`workspaces/`](workspaces/README.md) for examples. Built-in skills live under
[`analyst_runtime/skills/`](analyst_runtime/skills/README.md).

Add an OpenAI-compatible model vendor as a `ProviderSpec` in
[`analyst_runtime/providers/registry.py`](analyst_runtime/providers/registry.py). Add a provider
adapter only when the wire protocol or authentication lifecycle is genuinely different.

## Development

```bash
uv sync --frozen --extra dev
uv run ruff check .
uv run pytest -q -m "not e2e"
uv build
```

Staging end-to-end tests need an authenticated consuming application and are documented in
[`tests/e2e/README.md`](tests/e2e/README.md). See [`CONTRIBUTING.md`](CONTRIBUTING.md) before
opening a pull request and [`SECURITY.md`](SECURITY.md) for vulnerability reporting and safe
deployment notes.

## Security model

This runtime can call models, access files, execute commands, and contact external services.
Those capabilities are intentionally powerful. Use the narrowest tool profile, enable
workspace restrictions, isolate production processes, restrict channel senders, and never put
request-scoped credentials into prompts, logs, sessions, or progress events.

The repository is MIT licensed, but that is not a security guarantee. Review the threat model
for your deployment before exposing any gateway or chat channel.

## Origin and attribution

Analyst Runtime began as a deeply modified derivative of
[HKUDS/nanobot](https://github.com/HKUDS/nanobot), an open-source, self-hosted AI agent
framework. We are grateful to its maintainers and contributors for the original architecture
and implementation.

This repository preserves the original MIT copyright notice. Analyst Runtime is independently
maintained and is not affiliated with or endorsed by the nanobot project.

## License

[MIT](LICENSE)
