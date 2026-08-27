# Providers

This directory contains model, speech, and provider integrations. It is the main anti-corruption layer between runtime logic and vendor-specific APIs.

## What Lives Here
- `__init__.py` - current entrypoint or supporting file in this directory
- `base.py` - current entrypoint or supporting file in this directory
- `custom_provider.py` - current entrypoint or supporting file in this directory
- `litellm_provider.py` - current entrypoint or supporting file in this directory
- `openai_codex_provider.py` - current entrypoint or supporting file in this directory
- `registry.py` - current entrypoint or supporting file in this directory
- `transcription.py` - current entrypoint or supporting file in this directory
- `tts.py` - current entrypoint or supporting file in this directory

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
