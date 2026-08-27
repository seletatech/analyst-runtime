# Nanobot Runtime Package

This directory is the core Python runtime package. It should stay organized by runtime responsibility so new contributors can trace an agent request from entrypoint to provider call without guesswork.

## What Lives Here
- `agent/` - agent
- `bus/` - message-bus integration helpers
- `channels/` - channel adapters and routing
- `cli/` - command-line entrypoints
- `config/` - configuration and environment plumbing
- `cron/` - scheduled job support
- `heartbeat/` - liveness and heartbeat services
- `providers/` - provider integrations
- `session/` - session
- `skills/` - skills available to the agent runtime
- `utils/` - small helper utilities
- `__init__.py` - current entrypoint or supporting file in this directory
- `__main__.py` - current entrypoint or supporting file in this directory

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
