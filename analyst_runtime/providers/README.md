# Providers

This directory contains model, speech, and provider integrations. It is the main anti-corruption layer between runtime logic and vendor-specific APIs.

## What Lives Here
- `base.py` - stable `LLMProvider` port used by the agent loop
- `registry.py` - single catalog of provider identity and execution capabilities
- `litellm_provider.py` - shared adapter for LiteLLM/OpenAI-compatible providers
- `custom_provider.py` - direct custom OpenAI-compatible protocol adapter
- `openai_codex_provider.py` - OAuth-specific adapter
- `transcription.py` - current entrypoint or supporting file in this directory
- `tts.py` - current entrypoint or supporting file in this directory

## Dependency Notes
- `api/` for gateway-authenticated tool access and sandbox lifecycle integration
- `workspace/` for product-owned prompt, memory, and skill overlays
- LLM or media providers declared under `analyst_runtime/analyst_runtime/providers/`
- `docs/runbooks/` or product scripts when runtime behavior needs operator support

## Cleanup Guidance
- Use this README to keep the boundary crisp; if neighboring directories feel interchangeable, that is a signal to rename, merge, or archive something.

## Limits And Extension Notes
- The runtime stays approachable only if provider logic, channel adapters, and core agent behavior remain clearly separated.
- Add an OpenAI-compatible vendor as one `ProviderSpec`; do not create an empty vendor
  subclass. Add a new adapter file only when wire protocol, authentication, or lifecycle
  behavior cannot be expressed by the shared adapter.
- Provider allowlists, endpoint environment maps, model prefixes, sandbox defaults, and BYOK
  capability checks must be derived from `registry.py`, never copied into callers.
- Workspace state directories can easily blur template vs. generated data, so naming and cleanup discipline matters here.
