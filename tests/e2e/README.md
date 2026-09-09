# E2E Test Protocol

<!-- project-nav
{"entrypoints":["test_cron_reminder.py","test_email_calendar.py","test_exec_commands.py","test_investor_research.py","test_local_discovery.py"],"owners":[],"truth":{},"production":"test-only"}
-->

## 目录职责

Analyst Runtime 的恢复、工具、安全与端到端行为验证。它属于 **Runtime e2e tests** 边界。当前目录不替代父级架构说明，也不接管其他 Module 的数据或安全决策。

生产属性：本目录只用于测试，不进入生产运行时或 Edge release。

## 从这里开始

| 你要做的事情 | 下一步 |
|---|---|
| 查看主要入口或配置 | 打开 [`test_cron_reminder.py`](<test_cron_reminder.py>) |
| 运行该区域验证 | 从 Runtime 仓库根执行测试与 build；依赖入口见 [`pyproject.toml`](<../../pyproject.toml>) |

## 子目录

本目录没有需要继续下钻的项目子目录。

## 关键文件

| 文件 | 用途 |
|---|---|
| [`test_cron_reminder.py`](<test_cron_reminder.py>) | 行为或合同测试入口 |
| [`test_email_calendar.py`](<test_email_calendar.py>) | 行为或合同测试入口 |
| [`test_exec_commands.py`](<test_exec_commands.py>) | 行为或合同测试入口 |
| [`test_investor_research.py`](<test_investor_research.py>) | 行为或合同测试入口 |
| [`test_local_discovery.py`](<test_local_discovery.py>) | 行为或合同测试入口 |
| [`test_multi_step_chain.py`](<test_multi_step_chain.py>) | 行为或合同测试入口 |
| [`test_social_media.py`](<test_social_media.py>) | 行为或合同测试入口 |
| [`test_video_content.py`](<test_video_content.py>) | 行为或合同测试入口 |

## 依赖方向

Runtime 通过稳定 channel、provider、tool 和 workspace Interface 工作；profile 不得绕过工具权限或把未验证输出升级为外部事实。

## 修改规则

未验证 Agent 输出不得写成机器事实、指标或控制命令。

## 验证方式

从 Analyst Runtime 仓库根执行 `uv sync --frozen --extra dev`、`uv run pytest -q -m "not e2e"` 和 `uv build`；e2e 只在所需外部依赖已受控配置时运行。

## 相关文档

- [返回父目录 README](../README.md)
- [Analyst Runtime 根 README](<../../README.md>)

Staging end-to-end tests for real user behavior. Run these after any change to the agent loop,
tools, or skills to verify the full stack works against a live LLM.

## Prerequisites

- `ops/docker/.env.staging` filled in (copy from `.env.staging.example`)
- Staging stack running: `scripts/staging/start.sh -d`
- Local Supabase running: `supabase start`
- `FIRECRAWL_API_KEY` set in `.env.staging` (required for web scrape + chain tests)

---

## Step 1 — Rebuild images from source

Always rebuild when analyst_runtime or API source has changed.

```bash
# Rebuild analyst_runtime (always do this before running e2e tests)
scripts/staging/build-analyst_runtime.sh

# Rebuild API only if api/ source changed
scripts/staging/build-api.sh
```

After rebuilding the API, force-recreate the API container so the new image is used:

```bash
docker compose -f ops/docker/docker-compose.yml -f ops/docker/docker-compose.staging.yml \
  --env-file ops/docker/.env.staging up -d --force-recreate api
```

Verify it's healthy:

```bash
curl -s http://localhost:8000/health
# expect: {"status":"ok",...}
```

---

## Step 2 — Get or create a test sandbox

```bash
# Create a test user + project (idempotent — safe to run again if already exists)
scripts/staging/seed.sh
```

The seed script prints `Project ID: <uuid>`. Use that to start a sandbox container:

```bash
source ops/docker/.env.staging
# Sign in to get a JWT
ACCESS_TOKEN=$(curl -s -X POST "http://localhost:54321/auth/v1/token?grant_type=password" \
  -H "apikey: $SUPABASE_PUBLISHABLE_KEY" -H "Content-Type: application/json" \
  -d '{"email":"test@staging.local","password":"Staging1234!"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Start the sandbox container (replace PROJECT_ID with value from seed.sh)
curl -s -X POST "http://localhost:8000/api/projects/$PROJECT_ID/sandbox" \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json"
# expect: {"sandbox_id":"...","status":"running","reused":false}
```

Wait ~5 seconds for the container to come up, then look up its details:

```bash
curl -s http://localhost:8000/internal/sandboxes/status \
  -H "Authorization: Bearer $SANDBOX_INTERNAL_SECRET" \
  | python3 -c "
import sys,json
for s in json.load(sys.stdin)['sandboxes']:
    print(s['sandbox_id'], s['project_id'], s['status'])
"
```

Note the `sandbox_id` and `project_id` — you need both below.

**Important:** After rebuilding the analyst_runtime image, stop the old container and respawn:

```bash
docker stop analyst_runtime-<old_sandbox_id_first_12_chars>
# Then repeat the curl POST above to spawn a fresh container with the new image
```

---

## Step 3 — Run the tests

Tests **must** be run from the `analyst_runtime/` directory so `pyproject.toml` is loaded:

```bash
cd analyst_runtime

source ../ops/docker/.env.staging
export E2E_SANDBOX_ID="<sandbox_id from step 2>"
export E2E_SANDBOX_INTERNAL_SECRET="$SANDBOX_INTERNAL_SECRET"
export E2E_API_URL="http://localhost:8000"
export E2E_WORKSPACE_PATH="/data/sandboxes/<project_id>/workspace"
export E2E_CONTAINER_NAME="analyst_runtime-<first 12 chars of sandbox_id>"
export FIRECRAWL_API_KEY="<key from .env.staging>"

pytest tests/e2e/ -m e2e -v --timeout=180
```

Or use the runner script which does the sandbox discovery automatically:

```bash
# Optional: set E2E_PROJECT_ID to pin to a specific project
E2E_PROJECT_ID=<project_id> scripts/staging/run-e2e.sh
```

To run a single test:

```bash
pytest tests/e2e/test_cron_reminder.py -m e2e -v --timeout=120
```

---

## Key paths

| Thing | Path |
|-------|------|
| Session JSONL files | `/data/sandboxes/<project_id>/workspace/sessions/` |
| Cron jobs store | `/data/sandboxes/<project_id>/workspace/.analyst-runtime/cron/jobs.json` |
| Container logs | `docker logs analyst_runtime-<sandbox_id[:12]>` |
| Session naming | Web-channel sessions: `web_<session_id>.jsonl` · Cron sessions: `cron_<job_id>.jsonl` |

Session files are written by the analyst_runtime container. `E2E_WORKSPACE_PATH` must point to
`/data/sandboxes/<project_id>/workspace` on the **host** — not `$STAGING_SANDBOX_PATH` which
is the bind-mount source alias used by the API container.

---

## Inspecting a test conversation

After a test runs, read the session JSONL to see the full tool call trace:

```bash
python3 << 'EOF'
import json, sys
session_id = "e2e-chain-xxxxxxxx"   # from test output
path = f"/data/sandboxes/<project_id>/workspace/sessions/web_{session_id}.jsonl"
for line in open(path):
    ev = json.loads(line)
    t = ev.get("type")
    if t == "user_input":
        print(f"\nUSER: {ev['content']}")
    elif t == "llm_response":
        for tc in ev.get("tool_calls", []):
            print(f"  → {tc['function']['name']}({tc['function']['arguments'][:120]})")
    elif t == "tool_result":
        print(f"  ← [{ev['tool_name']}]: {ev['content'][:200]}")
    elif t == "final_response":
        print(f"\nFINAL: {ev['content']}")
EOF
```

---

## Common failure modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Session JSONL never appears | Wrong `E2E_WORKSPACE_PATH` | Use `/data/sandboxes/…` not `$HOME/.staging-sandboxes/…` |
| `wait_for_final_response` times out | Container not running / old image | `docker ps` to check; rebuild + respawn |
| Firecrawl tests skip | `FIRECRAWL_API_KEY` empty in `.env.staging` | Set the key and rebuild API + respawn sandbox |
| Firecrawl key not in container env | API wasn't rebuilt after adding env.py fix | `scripts/staging/build-api.sh` + force-recreate API |
| API crash-loop on startup | Stale import of deleted service file | Check `docker logs docker-api-1` for `ModuleNotFoundError` |
| Cron test: `PermissionError` unlinking session files | Files owned by container root | Use snapshot-then-diff pattern (already in conftest) |
| Wrong session returned by `/send` | Outbound queue has no correlation | Tests use fire-and-forget + poll JSONL (already fixed in conftest) |
| `pytest: not in e2e mark` | Running from repo root, not `analyst_runtime/` | `cd analyst_runtime` before running pytest |

---

## What the tests cover

Scenarios derived from real user session analysis (178 sessions, 22 users).
See `/home/mark/.gstack/projects/mesu-ai-studio-website/data/user-scenarios-wip.md` for full scenario catalogue.

After each test, a markdown result file is written to `tests/e2e/results/YYYY-MM-DD/<test_name>.md`.
This is the primary artifact for reviewing whether behavior is consistent with expectations.

### Cron Reminders (`test_cron_reminder.py`)
| Test | Scenario | Validates |
|------|----------|-----------|
| `test_reminder_creates_cron_job` | "10秒后提醒我喝水" | cron tool called, `.analyst-runtime/cron/jobs.json` written |
| `test_reminder_fires_and_agent_runs` | 8s reminder | cron fires within interval, cron session JSONL appears |

### Web Scraping (`test_web_scrape.py`)
| Test | Scenario | Validates |
|------|----------|-----------|
| `test_luma_url_does_not_use_web_fetch_first` | lu.ma URL | firecrawl used first, not web_fetch (JS SPA) |

### Research Chains (`test_multi_step_chain.py`)
| Test | Scenario | Validates |
|------|----------|-----------|
| `test_multi_step_research_uses_multiple_tools` | "搜索AI新闻，找2篇" | ≥3 tool calls, inline results across turns |

### Social Media (`test_social_media.py`) — requires FIRECRAWL_API_KEY
| Test | Scenario | Validates |
|------|----------|-----------|
| `test_twitter_x_url_uses_firecrawl` | X.com tweet URL | firecrawl used, not web_fetch (SPA) |
| `test_twitter_x_url_response_contains_content` | X.com tweet URL | response has actual content |

### Local Discovery (`test_local_discovery.py`) — requires FIRECRAWL_API_KEY
| Test | Scenario | Validates |
|------|----------|-----------|
| `test_local_venue_search_uses_search_tools` | "伦敦有什么开着的店" | firecrawl_search or google_maps used |
| `test_local_venue_response_is_actionable` | London bars/clubs | response has venue names |

### Investor Research (`test_investor_research.py`) — requires FIRECRAWL_API_KEY
| Test | Scenario | Validates |
|------|----------|-----------|
| `test_accelerator_research_is_multi_step` | SF accelerator search | ≥2 firecrawl calls (search + scrape) |
| `test_vc_firm_research_scrapes_website` | YC research | firecrawl used (not training data) |
| `test_luma_demo_day_evaluation` | lu.ma demo day | firecrawl used, not web_fetch |

### Exec / Dev Environment (`test_exec_commands.py`)
| Test | Scenario | Validates |
|------|----------|-----------|
| `test_exec_simple_command_is_called` | "run echo hello" | exec tool called, output in response |
| `test_exec_file_listing_uses_output` | "ls sessions dir" | exec called, result used |
| `test_exec_multi_step_inspect_output` | create/read/delete file | ≥2 exec calls |

### Email & Calendar (`test_email_calendar.py`)
| Test | Scenario | Validates |
|------|----------|-----------|
| `test_email_query_attempts_gmail_tool` | "what's in my email" | gateway_auth/gmail attempted OR auth guidance given |
| `test_calendar_query_attempts_google_calendar` | "read my calendar" | gateway_auth/calendar attempted OR guidance given |
| `test_email_delete_requires_confirmation` | "delete 100 emails" | result saved for review (no silent destruction) |

### Video Content (`test_video_content.py`)
| Test | Scenario | Validates |
|------|----------|-----------|
| `test_bilibili_video_extraction_attempts_tools` | BiliBili URL | exec or firecrawl attempted, not just refused |
| `test_youtube_url_extraction_attempt` | YouTube URL | tools attempted or explanation given |

---

## Reviewing results

After running, check `tests/e2e/results/YYYY-MM-DD/` for markdown summaries:

```bash
ls analyst_runtime/tests/e2e/results/$(date +%Y-%m-%d)/
cat analyst_runtime/tests/e2e/results/$(date +%Y-%m-%d)/test_twitter_x_url_uses_firecrawl.md
```

Each file shows: message sent, tools called (in order), final response.
Review for behavior consistency — does the agent do what real users expect?
