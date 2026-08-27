---
name: search
description: Use when users ask to search, compare, verify, or collect information from the web, especially when scrape-first Firecrawl retrieval may be enough.
always: true
---

# Search

Use Firecrawl for web research. Default flow is:

1. `firecrawl_search`
2. `firecrawl_scrape` on the best hits
3. Upgrade to `firecrawl_browser` only if scrape is not enough

## Trigger phrases

"search", "look up", "find", "compare", "what are the options", "帮我搜", "查一下", "帮我找", "比较一下"

## Use `firecrawl_scrape` when

- The user wants facts, summaries, comparisons, or evidence.
- The target page should already contain the answer after normal page load.
- No login, click, pagination, filter, or hidden-panel interaction is required.
- You can likely answer from readable page content alone.

## Upgrade to `firecrawl_browser` when

- The user explicitly asks to open, click, continue, paginate, expand, or filter.
- Scrape misses required fields or returns incomplete content.
- The page is JS-heavy and needs interaction or waiting.
- A cookie-backed logged-in page is required.
- The task needs multi-step navigation before the content becomes visible.

## Do not execute browser when

- The site, object, or success condition is unclear.
- Cookies are missing for an authenticated page.
- The action could be high-risk and intent is not fully specific.
- Search + scrape already answer the user well enough.

## Required intent slots before browser

Before `firecrawl_browser`, make sure you can state:

- `objective`
- `required_output`
- `allowed_side_effects`
- `needs_auth`
- `done_when`

If the task is interactive, authenticated, or high-risk and any slot is missing, ask first instead of guessing.

## Response rules

- Tell the user briefly before tool use. Include this text **in the same response as the tool call** — do not send a standalone text message that announces a tool call and then call the tool separately. The announcement text and the tool call must be part of the same response.
- Return structured evidence and mention if the result is partial.
- If Firecrawl returns `status="not_configured"`, tell the user Firecrawl is not configured for this sandbox.
- Reuse one `task_id` across search, scrape, and browser for the same user request.
- If you upgrade from scrape to browser, tell the user that static extraction was not enough and you need interaction.

## Edge cases

- If search fails, explain that the task is incomplete and name the failure instead of pretending no results exist.
- If scrape fails on one result, try another promising hit before escalating browser.
- If the user says the page is wrong, stop the current path and start a new `task_id`.
