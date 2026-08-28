# Runtime manufacturing

<!-- project-nav
{"entrypoints":["__init__.py","intents.py","memory.py","profile.py","tools.py"],"owners":[],"truth":{},"production":"analyst-runtime-image"}
-->

## 目录职责

Analyst Runtime 的 manufacturing 子系统，受 Runtime profile 和工具权限约束。它属于 **Runtime manufacturing** 边界。当前目录不替代父级架构说明，也不接管其他 Module 的数据或安全决策。

生产属性：本目录由 Analyst Runtime 项目维护；是否进入最终 image/profile 由 Dockerfile、package 配置和 workspace 选择显式决定。

## 从这里开始

| 你要做的事情 | 下一步 |
|---|---|
| 查看主要入口或配置 | 打开 [`__init__.py`](<__init__.py>) |
| 运行该区域验证 | 从 Runtime 仓库根执行测试与 build；依赖入口见 [`pyproject.toml`](<../../../pyproject.toml>) |

## 子目录

本目录没有需要继续下钻的项目子目录。

## 关键文件

| 文件 | 用途 |
|---|---|
| [`__init__.py`](<__init__.py>) | 本目录的主要实现、配置或受控资料 |
| [`intents.py`](<intents.py>) | 本目录的主要实现、配置或受控资料 |
| [`memory.py`](<memory.py>) | 本目录的主要实现、配置或受控资料 |
| [`profile.py`](<profile.py>) | 本目录的主要实现、配置或受控资料 |
| [`tools.py`](<tools.py>) | 本目录的主要实现、配置或受控资料 |

## 依赖方向

Runtime 通过稳定 channel、provider、tool 和 workspace Interface 工作；profile 不得绕过工具权限或把未验证输出升级为外部事实。

## 修改规则

未验证 Agent 输出不得写成机器事实、指标或控制命令。

## 验证方式

从 Analyst Runtime 仓库根执行 `uv sync --frozen --extra dev`、`uv run pytest -q -m "not e2e"` 和 `uv build`；e2e 只在所需外部依赖已受控配置时运行。

## 相关文档

- [返回父目录 README](../README.md)
- [Analyst Runtime 根 README](<../../../README.md>)
