# Bridge Package

This directory contains the bridge package used to connect runtime behavior with external adapters. It should stay small and explicit about how TypeScript glue differs from the Python runtime.

## What Lives Here
- `src/` - src
- `package.json` - current entrypoint or supporting file in this directory
- `tsconfig.json` - current entrypoint or supporting file in this directory

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
