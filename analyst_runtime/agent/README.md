# Agent

This directory owns the core agent loop, memory hookup, and subagent behavior. It sits at the center of the runtime, so names here need to stay especially direct.

## What Lives Here
- `tools/` - tool definitions and handlers
- `__init__.py` - current entrypoint or supporting file in this directory
- `context.py` - current entrypoint or supporting file in this directory
- `loop.py` - current entrypoint or supporting file in this directory
- `memory.py` - current entrypoint or supporting file in this directory
- `skills.py` - current entrypoint or supporting file in this directory
- `subagent.py` - current entrypoint or supporting file in this directory

## Dependency Notes
- `api/` for gateway-authenticated tool access and sandbox lifecycle integration
- `workspace/` for product-owned prompt, memory, and skill overlays
- LLM or media providers declared under `analyst_runtime/analyst_runtime/providers/`
- `docs/runbooks/` or product scripts when runtime behavior needs operator support

## Cleanup Guidance
- Use this README to keep the boundary crisp; if neighboring directories feel interchangeable, that is a signal to rename, merge, or archive something.

## Limits And Extension Notes
- The runtime stays approachable only if provider logic, channel adapters, and core agent behavior remain clearly separated.
- Workspace state directories can easily blur template vs. generated data, so naming and cleanup discipline matters here.
