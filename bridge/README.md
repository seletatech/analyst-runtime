# Bridge Package

<!-- heineda-nav
{"entrypoints":["package.json","tsconfig.json"],"owners":[],"truth":{},"production":"analyst-runtime-image"}
-->

## 目录职责

`analyst-runtime/bridge` 的第一方项目内容与导航边界。它属于 **bridge** 边界。当前目录不替代父级架构说明，也不接管其他 Module 的数据或安全决策。

生产属性：本目录由 Analyst Runtime 项目维护；是否进入最终 image/profile 由 Dockerfile、package 配置和 workspace 选择显式决定。

## 从这里开始

| 你要做的事情 | 下一步 |
|---|---|
| 查看主要入口或配置 | 打开 [`package.json`](<package.json>) |
| 继续查找实现或资料 | 进入 [`src/`](<src/README.md>) |
| 运行该区域验证 | 从 Runtime 仓库根执行测试与 build；依赖入口见 [`pyproject.toml`](<../pyproject.toml>) |

## 子目录

| 子目录 | 内容 | 什么时候进入 |
|---|---|---|
| [`src/`](<src/README.md>) | `analyst-runtime/bridge/src` 的第一方项目内容与导航边界 | 修改或核验该职责时 |

## 关键文件

| 文件 | 用途 |
|---|---|
| [`package.json`](<package.json>) | 构建、依赖和版本配置 |
| [`tsconfig.json`](<tsconfig.json>) | 本目录的主要实现、配置或受控资料 |

## 依赖方向

Runtime 通过稳定 channel、provider、tool 和 workspace Interface 工作；profile 不得绕过工具权限或把未验证输出升级为外部事实。

## 修改规则

未验证 Agent 输出不得写成机器事实、指标或控制命令。

## 验证方式

从 Analyst Runtime 仓库根执行 `uv sync --frozen --extra dev`、`uv run pytest -q -m "not e2e"` 和 `uv build`；e2e 只在所需外部依赖已受控配置时运行。

## 相关文档

- [返回父目录 README](../README.md)
- [Analyst Runtime 根 README](<../README.md>)

This directory contains the bridge package used to connect runtime behavior with external adapters. It should stay small and explicit about how TypeScript glue differs from the Python runtime.

## What Lives Here
- `src/` - src
- `package.json` - current entrypoint or supporting file in this directory
- `tsconfig.json` - current entrypoint or supporting file in this directory

## Dependency Notes
- `api/` for gateway-authenticated tool access and sandbox lifecycle integration
- `workspace/` for product-owned prompt, memory, and skill overlays
- LLM or media providers declared under `analyst_runtime/analyst_runtime/providers/`
- `docs/runbooks/` or product scripts when runtime behavior needs operator support

## Cleanup Guidance
- Use this README to keep the boundary crisp; if neighboring directories feel interchangeable, that is a signal to rename, merge, or archive something.

## Limits And Extension Notes
- The runtime stays approachable only if provider logic, channel adapters, and core agent behavior remain clearly separated.
- Workspace state directories can easily blur template vs. generated data, so naming and cleanup discipline matters here.
