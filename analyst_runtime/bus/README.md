# Bus

<!-- heineda-nav
{"entrypoints":["__init__.py","delivery.py","events.py","queue.py"],"owners":[],"truth":{},"production":"analyst-runtime-image"}
-->

## 目录职责

Analyst Runtime 的 bus 子系统，受 Runtime profile 和工具权限约束。它属于 **Runtime bus** 边界。当前目录不替代父级架构说明，也不接管其他 Module 的数据或安全决策。

生产属性：本目录由 Analyst Runtime 项目维护；是否进入最终 image/profile 由 Dockerfile、package 配置和 workspace 选择显式决定。

## 从这里开始

| 你要做的事情 | 下一步 |
|---|---|
| 查看主要入口或配置 | 打开 [`__init__.py`](<__init__.py>) |
| 运行该区域验证 | 从 Runtime 仓库根执行测试与 build；依赖入口见 [`pyproject.toml`](<../../pyproject.toml>) |

## 子目录

本目录没有需要继续下钻的项目子目录。

## 关键文件

| 文件 | 用途 |
|---|---|
| [`__init__.py`](<__init__.py>) | 本目录的主要实现、配置或受控资料 |
| [`delivery.py`](<delivery.py>) | 本目录的主要实现、配置或受控资料 |
| [`events.py`](<events.py>) | 本目录的主要实现、配置或受控资料 |
| [`queue.py`](<queue.py>) | 本目录的主要实现、配置或受控资料 |

## 依赖方向

Runtime 通过稳定 channel、provider、tool 和 workspace Interface 工作；profile 不得绕过工具权限或把未验证输出升级为外部事实。

## 修改规则

未验证 Agent 输出不得写成机器事实、指标或控制命令。

## 验证方式

从 Analyst Runtime 仓库根执行 `uv sync --frozen --extra dev`、`uv run pytest -q -m "not e2e"` 和 `uv build`；e2e 只在所需外部依赖已受控配置时运行。

## 相关文档

- [返回父目录 README](../README.md)
- [Analyst Runtime 根 README](<../../README.md>)

This directory defines the message-bus primitives used to communicate between the runtime and outside systems. It should stay transport-focused and not absorb unrelated orchestration logic.

## What Lives Here
- `__init__.py` - current entrypoint or supporting file in this directory
- `events.py` - current entrypoint or supporting file in this directory
- `queue.py` - current entrypoint or supporting file in this directory

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
