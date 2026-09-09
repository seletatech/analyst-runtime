# Analyst Runtime

<!-- project-nav
{"entrypoints":["Dockerfile","docker-compose.yml","pyproject.toml","uv.lock",".gitignore"],"owners":[],"truth":{},"production":"analyst-runtime-image"}
-->

## 目录职责

`analyst-runtime` 的第一方项目内容与导航边界。它属于 **analyst-runtime** 边界。当前目录不替代父级架构说明，也不接管其他 Module 的数据或安全决策。

生产属性：本目录由 Analyst Runtime 项目维护；是否进入最终 image/profile 由 Dockerfile、package 配置和 workspace 选择显式决定。

## 从这里开始

| 你要做的事情 | 下一步 |
|---|---|
| 查看主要入口或配置 | 打开 [`Dockerfile`](<Dockerfile>) |
| 继续查找实现或资料 | 进入 [`analyst_runtime/`](<analyst_runtime/README.md>) |
| 运行该区域验证 | 从 Runtime 仓库根执行测试与 build；依赖入口见 [`pyproject.toml`](<pyproject.toml>) |

## 子目录

| 子目录 | 内容 | 什么时候进入 |
|---|---|---|
| [`analyst_runtime/`](<analyst_runtime/README.md>) | `analyst-runtime/analyst_runtime` 的第一方项目内容与导航边界 | 修改或核验该职责时 |
| [`bridge/`](<bridge/README.md>) | `analyst-runtime/bridge` 的第一方项目内容与导航边界 | 修改或核验该职责时 |
| [`tests/`](<tests/README.md>) | Analyst Runtime 的恢复、工具、安全与端到端行为验证 | 修改或核验该职责时 |
| [`vendor/`](<vendor/README.md>) | `analyst-runtime/vendor` 的第一方项目内容与导航边界 | 修改或核验该职责时 |
| [`workspaces/`](<workspaces/README.md>) | `analyst-runtime/workspaces` 的第一方项目内容与导航边界 | 修改或核验该职责时 |

## 关键文件

| 文件 | 用途 |
|---|---|
| [`Dockerfile`](<Dockerfile>) | 构建或部署配置入口 |
| [`docker-compose.yml`](<docker-compose.yml>) | 构建或部署配置入口 |
| [`pyproject.toml`](<pyproject.toml>) | 构建、依赖和版本配置 |
| [`uv.lock`](<uv.lock>) | 构建、依赖和版本配置 |
| [`.gitignore`](<.gitignore>) | 本目录的主要实现、配置或受控资料 |
| [`Dockerfile.mvp`](<Dockerfile.mvp>) | 本目录的主要实现、配置或受控资料 |
| [`LICENSE`](<LICENSE>) | 本目录的主要实现、配置或受控资料 |
| [`SECURITY.md`](<SECURITY.md>) | 本目录的主要实现、配置或受控资料 |

## 依赖方向

Runtime 通过稳定 channel、provider、tool 和 workspace Interface 工作；profile 不得绕过工具权限或把未验证输出升级为外部事实。

## 修改规则

未验证 Agent 输出不得写成机器事实、指标或控制命令。

## 验证方式

从 Analyst Runtime 仓库根执行 `uv sync --frozen --extra dev`、`uv run pytest -q -m "not e2e"` 和 `uv build`；e2e 只在所需外部依赖已受控配置时运行。

## 相关文档

本目录是 Analyst Runtime 上游仓库根；集成仓库应从自己的 gitlink/consumer contract 导航到这里。

Analyst Runtime is our private, product-neutral agent execution engine for
enterprise applications. It owns model routing, tool execution, conversation
state, workspace prompts, optional runtime profiles, and authenticated product
gateway integration.

The public integration name is **Analyst Runtime**. The Python package is
`analyst_runtime`, the command is `analyst-runtime`, and product-specific
identity and behavior live in a supplied workspace rather than in the core.

See [Prompt caching and bounded context](PROMPT_CACHING.md) for provider cache
behavior, tool-result retention and verification evidence.

## Origin and attribution

Analyst Runtime began as a deeply modified derivative of
[HKUDS/nanobot](https://github.com/HKUDS/nanobot), an open-source,
self-hosted personal AI agent framework. We are grateful to the NanoBot
maintainers and contributors for the original architecture and implementation.

This repository preserves the original MIT license and copyright notice. It is
an independently maintained private derivative and is not affiliated with or
endorsed by the NanoBot project.

## Runtime boundary

- `analyst_runtime/` contains the generic Python runtime.
- `workspaces/` contains copyable product prompt and runtime-profile templates.
- `bridge/` contains optional channel bridges.
- `tests/` contains the runtime contract and regression suite.

Analyst Runtime is the only agent orchestrator in consuming products. It owns every model
call, reasoning step, tool decision/execution, retry, session update, workspace read, and
final message. UI frameworks may adapt its events for display but must not wrap it in a
second agent loop.

Run-scoped `steer_request` control messages are accepted only while that run is active. The
Runtime queues the instruction and applies it after the current tool-call batch (or before a
text-only response becomes final), persists it as user input, acknowledges `steer_applied`,
and then continues the same agent loop. The session also persists a bounded set of applied
`steer_id` values so retries and status lookups are idempotent after transport disconnects. It
reports a received command as `pending` until the next safe boundary and never starts a parallel
run for steering.

Vercel AI SDK (`ai` and `@ai-sdk/*`) is forbidden in this repository, including the optional
Node channel bridges. Python provider access remains behind `LLMProvider`; product model
selection arrives as a trusted profile ID and is resolved here to its provider/model. BYOK
verification and model calls both remain behind the Runtime provider port. BYOK secrets must
be removed from inbound metadata before any log, session event, trace, or outbound message.

### Provider architecture

`analyst_runtime/providers/registry.py` is the single provider catalog. Each `ProviderSpec`
owns the provider identity, endpoint/environment metadata, model-ID normalization, sandbox
default, and whether request-scoped credentials are accepted. CLI configuration and the
LiteLLM adapter derive their supported providers from that catalog; they must not introduce
their own provider allowlists or endpoint maps.

`LLMProvider` is the stable Runtime port. Add a separate adapter file only when a provider has
a genuinely different protocol or authentication lifecycle (for example OAuth or Bedrock).
OpenAI-compatible vendors remain declarative `ProviderSpec` entries and use the shared
adapter. Switching a product profile is therefore a routing/configuration change inside the
same agent loop, never a new Runtime implementation.

The active product workspace is supplied by the consuming repository. Its
`SOUL.md` defines who the agent is and `AGENTS.md` defines how it works. Runtime state,
customer data, prompts, and artifacts do not belong in this repository.

Linghui runs the `trusted-analysis` tool profile. It exposes workspace-scoped file and exec
tools, Web/Firecrawl retrieval, and `message`. It does not expose MCP, Composio, external
business integrations, audio/vision tools, `spawn`, `cron`, or manufacturing-semantics tools.
Semantic confirmation is an ordinary system-prompt rule and
continues through the same Analyst Runtime conversation.

## Development

```bash
uv sync
uv run pytest
```

Build from a consuming repository whose root contains both `analyst-runtime/`
and `workspace/`:

```bash
docker build -f analyst-runtime/Dockerfile -t analyst-runtime:latest .
```
