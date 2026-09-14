# Contributing

Thanks for helping improve Analyst Runtime.

## Before coding

- For a bug, open an issue with a minimal reproduction and sanitized logs.
- For a new feature or public API change, describe the use case before implementing it.
- Keep product prompts, customer data, and product-specific semantics in a workspace or
  consuming repository.
- Never include credentials in issues, fixtures, logs, traces, or commits.

## Development setup

```bash
git clone https://github.com/seletatech/analyst-runtime.git
cd analyst-runtime
uv sync --frozen --extra dev
```

Run the local checks:

```bash
uv run ruff check .
uv run pytest -q -m "not e2e"
uv build
```

End-to-end tests are staging tests and require an authenticated consuming application. See
[`tests/e2e/README.md`](tests/e2e/README.md).

## Design boundaries

- Keep all model access behind `LLMProvider`.
- Treat `analyst_runtime/providers/registry.py` as the provider catalog; do not copy provider
  allowlists or model-prefix routing into callers.
- Use the shared adapter for OpenAI-compatible providers. Add an adapter only for a real
  protocol or authentication difference.
- Preserve ordered, append-only progress events and separate tool calls from tool results.
- Strip request-scoped credentials before logging, persistence, tracing, or publication.
- Prefer one focused change with a regression test over speculative abstractions.

Pull requests should explain the user-visible behavior, the boundary being changed, and the
commands used to verify the change.
