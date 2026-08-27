# Cli

This directory holds CLI entrypoints for developers or operators. It should remain a thin shell over runtime primitives instead of re-implementing runtime logic.

## What Lives Here
- `__init__.py` - current entrypoint or supporting file in this directory
- `commands.py` - current entrypoint or supporting file in this directory

## Dependency Notes
- `api/` for gateway-authenticated tool access and sandbox lifecycle integration
- `workspace/` for product-owned prompt, memory, and skill overlays
- LLM or media providers declared under `nanobot/nanobot/providers/`
- `docs/runbooks/` or product scripts when runtime behavior needs operator support

## Cleanup Guidance
- Use this README to keep the boundary crisp; if neighboring directories feel interchangeable, that is a signal to rename, merge, or archive something.

## Limits And Extension Notes
- The runtime stays approachable only if provider logic, channel adapters, and core agent behavior remain clearly separated.
- Workspace state directories can easily blur template vs. generated data, so naming and cleanup discipline matters here.
