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

The active product workspace is supplied by the consuming repository. Its
`SOUL.md` defines who the agent is, `AGENTS.md` defines how it works, and
`workspace.json` selects optional runtime capabilities. Runtime state, customer
data, prompts, and artifacts do not belong in this repository.

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
