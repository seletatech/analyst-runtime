# nanobot Skills

This directory contains built-in skills that extend nanobot's capabilities.

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
