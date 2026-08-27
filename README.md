# Nanobot Runtime

`nanobot/` contains the lightweight agent runtime used by the gateway to power per-user sandboxes.

This repository carries the integrated runtime used in production, not just a standalone demo package.

## Run Locally

```bash
uv sync
uv run pytest
```

From the repository root, build the runtime image with the product workspace:

```bash
docker build -f nanobot/Dockerfile -t mesu/nanobot:latest .
```

After changing runtime code that is baked into sandbox images, use:

```bash
./scripts/samantha/local/rebuild-nanobot-and-upgrade.sh
```

## Key Directories

- `nanobot/` - core runtime package
- [`workspaces/`](workspaces/) - copyable product prompt and runtime-profile templates
- `tests/` - automated tests
- [`../workspace/`](../workspace/) - product-owned sandbox prompt, data, and runtime state
- `bridge/` - TypeScript bridge package

The active workspace builds the system prompt in layers: NanoBot's generic
runtime prompt, then `SOUL.md` (who the agent is), then `AGENTS.md` (how it
works). Optional domain behavior is selected by `workspace.json`; an absent or
empty `runtime_profiles` list leaves the generic runtime unchanged.

## Related Directories

- [`api/`](../api/)
- [`scripts/samantha/local/rebuild-nanobot-and-upgrade.sh`](../scripts/samantha/local/rebuild-nanobot-and-upgrade.sh)
