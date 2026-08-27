# Workspace templates

Each folder is a complete product-level system-prompt configuration. Copy its
`SOUL.md`, `AGENTS.md`, and `workspace.json` into the active product
`workspace/`. Keep the template directories immutable; Analyst Runtime writes
memory and session state into the active workspace at runtime.

```bash
cp analyst-runtime/workspaces/data-analyst/{SOUL.md,AGENTS.md,workspace.json} workspace/
```

- `SOUL.md` defines who the agent is, such as a data analyst or an executive.
- `AGENTS.md` defines how that role works and delivers answers.
- `workspace.json` enables optional runtime profiles. An absent or empty list
  means the generic Analyst Runtime only.

Choose runtime profiles when a product workspace is created. If a profile has
already persisted owned state, Analyst Runtime fails closed when that profile is
removed; migrate or remove that state explicitly before changing profiles.

`trusted_gateway` is optional. Set its `runtime` and `project_id` only when an
authenticated product gateway must append request-scoped system policy. If it
is absent, Analyst Runtime rejects all such metadata; the normal `SOUL.md` and
`AGENTS.md` prompt still works.

Runtime state (`memory/`, `sessions/`, data and artifacts) belongs to the
active product workspace and must not be copied into these templates.
