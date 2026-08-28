# analyst_runtime Skills

<!-- heineda-nav
{"entrypoints":[],"owners":[],"truth":{},"production":"analyst-runtime-image"}
-->

## 目录职责

Analyst Runtime 的 skills 子系统，受 Runtime profile 和工具权限约束。它属于 **Runtime skills** 边界。当前目录不替代父级架构说明，也不接管其他 Module 的数据或安全决策。

生产属性：本目录由 Analyst Runtime 项目维护；是否进入最终 image/profile 由 Dockerfile、package 配置和 workspace 选择显式决定。

## 从这里开始

| 你要做的事情 | 下一步 |
|---|---|
| 继续查找实现或资料 | 进入 [`browser-use/`](<browser-use/README.md>) |
| 运行该区域验证 | 从 Runtime 仓库根执行测试与 build；依赖入口见 [`pyproject.toml`](<../../pyproject.toml>) |

## 子目录

| 子目录 | 内容 | 什么时候进入 |
|---|---|---|
| [`browser-use/`](<browser-use/README.md>) | Analyst Runtime 可发现的 `browser-use` 技能包；SKILL.md 是行为入口 | 修改或核验该职责时 |
| [`cron/`](<cron/README.md>) | Analyst Runtime 可发现的 `cron` 技能包；SKILL.md 是行为入口 | 修改或核验该职责时 |
| [`geo-content-writer/`](<geo-content-writer/README.md>) | Analyst Runtime 可发现的 `geo-content-writer` 技能包；SKILL.md 是行为入口 | 修改或核验该职责时 |
| [`memory/`](<memory/README.md>) | Analyst Runtime 可发现的 `memory` 技能包；SKILL.md 是行为入口 | 修改或核验该职责时 |
| [`search/`](<search/README.md>) | Analyst Runtime 可发现的 `search` 技能包；SKILL.md 是行为入口 | 修改或核验该职责时 |
| [`seo-geo-agent/`](<seo-geo-agent/README.md>) | Analyst Runtime 可发现的 `seo-geo-agent` 技能包；SKILL.md 是行为入口 | 修改或核验该职责时 |
| [`skill-creator/`](<skill-creator/README.md>) | Analyst Runtime 可发现的 `skill-creator` 技能包；SKILL.md 是行为入口 | 修改或核验该职责时 |
| [`summarize/`](<summarize/README.md>) | Analyst Runtime 可发现的 `summarize` 技能包；SKILL.md 是行为入口 | 修改或核验该职责时 |
| [`tmux/`](<tmux/README.md>) | Analyst Runtime 可发现的 `tmux` 技能包；SKILL.md 是行为入口 | 修改或核验该职责时 |

## 关键文件

本目录没有直属程序入口；内容通过上方子目录导航。

## 依赖方向

Runtime 通过稳定 channel、provider、tool 和 workspace Interface 工作；profile 不得绕过工具权限或把未验证输出升级为外部事实。

## 修改规则

未验证 Agent 输出不得写成机器事实、指标或控制命令。

## 验证方式

从 Analyst Runtime 仓库根执行 `uv sync --frozen --extra dev`、`uv run pytest -q -m "not e2e"` 和 `uv build`；e2e 只在所需外部依赖已受控配置时运行。

## 相关文档

- [返回父目录 README](../README.md)
- [Analyst Runtime 根 README](<../../README.md>)

This directory contains built-in skills that extend analyst_runtime's capabilities.

## Skill Format

Each skill is a directory containing a `SKILL.md` file with:
- YAML frontmatter (name, description, metadata)
- Markdown instructions for the agent

## Attribution

These skills are adapted from [OpenClaw](https://github.com/openclaw/openclaw)'s skill system.
The skill format and metadata structure follow OpenClaw's conventions to maintain compatibility.

## Available Skills

| Skill | Description |
|-------|-------------|
| `aura` | Aura design intelligence — UI design, design-to-code, component generation, design system |
| `browser-use` | Stepwise browser interaction, authenticated cookie-backed browsing, and page actions |
| `clawhub` | Search and install skills from ClawHub registry |
| `composio` | 100+ SaaS integrations via Composio — GitHub, Slack, Linear, Notion, Salesforce, and more |
| `cron` | Schedule reminders and recurring tasks |
| `eng-cleanup` | Code cleanup agent — dead code, unused imports, stale comments, directory violations |
| `eng-feature` | Feature implementation agent — picks up GitHub issues and implements end-to-end |
| `eng-maintainer` | Maintainer agent — bug fixes, regressions, test failures, codebase health |
| `eng-triage` | GitHub issue triage agent — labels, deduplicates, and dispatches issues |
| `eng-viz` | Harness pipeline visualization — live status diagram of engineering agents and PR activity |
| `figma` | Figma design analysis and asset export |
| `github` | Interact with GitHub using the `gh` CLI |
| `google-workspace` | Gmail and Google Calendar via gws CLI — send emails, read inbox, manage calendar events |
| `image` | Image generation prompting and tool guidance |
| `make-story` | Generate story chapters that continue a narrative |
| `make-website` | Expose a local site or service as a public URL via Cloudflare tunnel |
| `memory` | Two-layer memory system with grep-based recall |
| `notion` | Notion integration — search, read, create pages and blocks |
| `proactive` | Proactive agent — daily briefings, event alerts, task follow-ups, and idle nudges |
| `samantha` | Samantha personal AI assistant persona |
| `search` | Firecrawl-first web search with scrape-before-browser rules |
| `skill-creator` | Create or update agent skills |
| `summarize` | Summarize URLs, files, podcasts, and YouTube videos |
| `telegram-connect` | Bind Telegram accounts and verify sandbox routing |
| `tmux` | Remote-control tmux sessions |
| `video` | Generate videos via Kling, Sora, Seedance, Stable Diffusion, or Runway |
| `weather` | Current weather and forecasts (no API key required) |
| `whatsapp-summarize` | Summarize WhatsApp conversation history |
| `x-twitter` | Access X (Twitter) posts, profiles, and threads |

## Geo-Agent Skills

| Skill | Description |
|-------|-------------|
| `seo-geo-agent` | Daily SEO/GEO audit SOP — keyword tracking, AI traffic monitoring, CTR optimization, IndexNow. Achieves ~32K impressions/month. Source: Gingiris-1031/gingiris-seo-geo-agent |
| `geo-content-writer` | Turn Dageno GEO opportunities into a fanout backlog and produce publish-ready articles. Source: dageno-agents/geo-content-writer |

## Skill Priority

When the runtime loads skills, it follows this priority order (highest first):

1. **Workspace skills** — `<workspace>/skills/<name>/SKILL.md` (per-sandbox, highest priority)
2. **MeSu extension skills** — `mesu/skills/<name>/SKILL.md` (deployment-level overrides)
3. **Built-in skills** — this directory (available everywhere, lowest priority)

A workspace skill with the same name as a built-in skill will shadow the built-in.

## Adding A New Skill

1. Create `<skill-name>/SKILL.md` with YAML frontmatter (`name`, `description`, `always`, and optionally `metadata.requires`).
2. Add an entry to the table above.
3. Rebuild the container image for the skill to be available in new sandboxes. For immediate effect in a running sandbox, also copy the skill to that sandbox's `workspace/skills/` directory.
