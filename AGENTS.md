# Analyst Runtime agent instructions

This repository is the sole agent runtime. It owns model calls, reasoning, tool calls/results,
retry policy, session/workspace context, trace, and final message delivery.

- Never add Vercel AI SDK, `ai`, or `@ai-sdk/*` to Python or Node manifests/source.
- Keep model providers behind `LLMProvider`; a model profile changes routing for one run, not
  the agent architecture. Product profile-to-provider/model resolution belongs here, never in
  the Web or FastAPI gateway.
- Accept model/credential overrides only from a configured trusted gateway.
- Verify BYOK through the Runtime provider port; upstream layers may store or forward secrets
  but must not contact model providers.
- BYOK values are request-scoped secrets. Remove them before logging, persistence, progress,
  trace, and outbound publication.
- Emit ordered append-only progress. Do not collapse Call Tool and Tool Result into one state.
- Apply run-scoped steering only inside the active agent loop at a safe step boundary. Preserve
  it as user input, acknowledge only after context insertion, and reject it if the target run
  is no longer active.
- Product prompts and data belong in the consuming workspace, not this generic runtime.
