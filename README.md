# Analyst Runtime

<!-- heineda-nav
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

The active product workspace is supplied by the consuming repository. Its
`SOUL.md` defines who the agent is, `AGENTS.md` defines how it works, and
`workspace.json` selects optional runtime capabilities. Runtime state, customer
data, prompts, and artifacts do not belong in this repository.

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
