# Prompt caching and bounded context

Verified 2026-09-07 against Nebius DeepSeek-V4-Flash-0731.

Nebius has returned cache-hit counts in real calls. Prefix reuse is server-managed:
do not send Claude `cache_control`, or assume OpenAI-specific TTL/key options are
accepted by Nebius Chat Completions. Cache hits alone do not establish a billing
discount; use Nebius's model-specific billing terms, never DeepSeek's direct API
rate. The previous two six-turn runs reported 2,023,680 cached input tokens out
of 2,145,984 total input tokens (94.3%). This is usage evidence, not a cost estimate.
Two sequential synthetic requests through the Runtime provider also confirmed
0/2,118 cached tokens on the first request and 2,048/2,118 on the second (96.7%),
without an explicit cache parameter. Both returned `OK`.

Runtime behavior:

- Put reusable system instructions before clock/session context. Keep the stable
  block's Claude cache breakpoint before the dynamic block.
- Preserve tool ordering and append messages during a tool loop. Do not rebuild
  or prune earlier messages on every call just to improve an apparent hit ratio.
- Archive long tool outputs once on insertion and expose a bounded 16,000-character
  preview in the active context. The archive path permits targeted recovery using
  `exec` to select a range or filter rows. It is partial evidence, not a full scan.
- Keep the full result for analysis binding and tool execution status. The
  conversation's approved analysis artifact remains the source for exact results.
- Existing threshold-triggered compaction preserves goals, exact numbers, decisions,
  identifiers, evidence locations, failures and pending work. It keeps recent
  messages and never splits tool calls from their responses.
- Normalize both `prompt_tokens_details.cached_tokens` and provider-specific
  cache usage fields. Do not add cached input on top of total prompt tokens.

Long outputs and unnecessary calls still cost money even with cache hits. Prefer
bounded queries, filtering, aggregation and range selection at the source. Tune
limits using task accuracy and actual billed cost, not cache-hit rate alone.

Sources:

- [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching):
  stable prefixes, model-specific controls, compaction/cache tradeoffs.
- [Claude prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching):
  explicit breakpoints, tools/system/messages ordering and cache lifetimes.
- [Anthropic effective tools](https://www.anthropic.com/engineering/writing-tools-for-agents):
  pagination, filtering, range selection and bounded responses.
- [Anthropic context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents):
  high-fidelity compaction, durable notes and retrieval by reference.
- [Nebius documentation index](https://docs.tokenfactory.nebius.com/llms.txt):
  no dedicated prompt-cache discount policy was found in the reviewed index.
- [Nebius pricing](https://nebius.com/token-factory/prices): verify model-specific
  cached-input prices in the account before reporting savings.
