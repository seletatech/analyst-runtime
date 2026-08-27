---
name: browser-use
description: Use when users need stepwise browser interaction, authenticated cookie-backed browsing, or page actions that scrape alone cannot complete.
always: true
---

# Browser Use

Use `firecrawl_browser` for page interaction that scrape cannot complete.

`firecrawl_browser` is bash-first. It runs Firecrawl browser automation with the preinstalled `agent-browser` CLI, not legacy Playwright `steps`.

## Trigger phrases

"open it", "click through", "continue", "go into the page", "log in and check", "fill this in", "submit this", "帮我点进去", "继续操作", "登录后看看", "帮我完成流程"

## Execution policy

- Browser runs only after you understand:
  - the exact goal
  - the target site or page
  - what counts as done
  - whether side effects are allowed
- If any of those are missing, ask a short clarification instead of guessing.
- Default user requests like “帮我搜个东西” to search + scrape, not browser.
- If the page is a calendar, list, search results page, or dashboard with multiple plausible targets, do not choose one yourself.
- Do not choose a specific event from a list page unless the user has already named it or the page has a single obvious primary CTA that matches the request.

## Command format

- Use `commands` with `agent-browser` command fragments.
- Do not include the `agent-browser` prefix.
- Do not include the initial `open` command. The tool opens `start_url` for you.
- Prefer semantic commands such as:
  - `find role button click --name "Continue"`
  - `find label "Email" fill "user@example.com"`
  - `press Enter`
  - `get text "text=Confirmation" --json`
- Prefer `find role`, `find label`, `fill`, `click`, `press`, `get text`, and screenshots over CSS guessing.
- Do not use legacy selector patterns like `:contains(...)`.

## Evidence rule

- Never claim “I submitted it”, “I subscribed you”, “I sent the code”, or similar unless the tool output contains evidence such as:
  - a final snapshot showing the confirmation state
  - confirmation text
  - a confirmation URL
  - a saved screenshot
- `liveView` URLs are debugging artifacts, not something you can see directly.
- If the result is partial or includes an `incomplete_reason`, say exactly what was verified and what was not.

## High-risk actions

For send/post/pay/delete/authorize/submit actions, continue only when the user’s intent is specific about:

- the target object
- the exact irreversible action
- the expected destination or recipient

Otherwise stop and clarify.

## Cookie-backed browsing

- If a page requires login, use `cookie_profile` with `firecrawl_browser`.
- If the cookie profile is missing or invalid, stop and tell the user what is needed.
- Treat `auth_required` style browser failures the same way the Google Workspace skill treats missing auth: explain what credential is missing, stop immediately, and wait for fresh user input or a refreshed cookie file.

## Verification code / OTP flows

- If the user explicitly asked to register or sign up and already gave the email or phone number, it is allowed to submit that contact info and trigger the verification code send.
- After triggering the code send, stop immediately and ask the user for the code.
- Do not guess or fabricate the verification result.

## Failure handling

- If browser output is partial, explain what was completed and what is still missing.
- If the user corrects your intent, stop the current path and restart with the new instruction.
- If page state and user goal do not match, do not continue clicking blindly.
- If the result includes an `incomplete_reason`, surface it to the user instead of hiding it.

## Edge cases

### (a) Task failure handling

- Stop after repeated interaction failures instead of retrying forever.
- If browser fails after scrape already found partial information, return the partial information.
- If browser execution fails before any evidence is captured, say that the action was not verified.

### (b) Execute vs do-not-execute standard

- Execute only when the next browser step has a clear purpose tied to the user goal.
- Do not execute exploratory but risky actions.

### (c) Handling user feedback

- If the user says “not that one” or “you misunderstood”, stop the current path and rebuild the plan.
- Do not continue with stale assumptions after the user corrects you.

### (d) Understanding user intent accurately

- Classify the task as `lookup`, `research`, `interactive`, `authenticated`, or `high_risk_action`.
- Upgrade to browser only for `interactive`, `authenticated`, or `high_risk_action`.
- For `high_risk_action`, make sure the exact irreversible action is explicit in the user request.
- When the page contains multiple candidate actions, ask the user to choose before clicking.
