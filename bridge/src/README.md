# Bridge Source

This directory holds the implementation code for the bridge package. If the bridge grows beyond adapter glue, splitting by protocol or channel would keep it understandable.

## What Lives Here
- `index.ts` - current entrypoint or supporting file in this directory
- `server.ts` - current entrypoint or supporting file in this directory
- `types.d.ts` - current entrypoint or supporting file in this directory
- `whatsapp.ts` - current entrypoint or supporting file in this directory

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
