# Runtime tests

<!-- heineda-nav
{"entrypoints":["test_adapter_registry.py","test_agent_loop_runtime.py","test_composio_agent_flow.py","test_cron_service.py","test_duplicate_message_in_context.py"],"owners":[],"truth":{},"production":"test-only"}
-->

## 目录职责

Analyst Runtime 的恢复、工具、安全与端到端行为验证。它属于 **Runtime tests** 边界。当前目录不替代父级架构说明，也不接管其他 Module 的数据或安全决策。

生产属性：本目录只用于测试，不进入生产运行时或 Edge release。

## 从这里开始

| 你要做的事情 | 下一步 |
|---|---|
| 查看主要入口或配置 | 打开 [`test_adapter_registry.py`](<test_adapter_registry.py>) |
| 继续查找实现或资料 | 进入 [`e2e/`](<e2e/README.md>) |
| 运行该区域验证 | 从 Runtime 仓库根执行测试与 build；依赖入口见 [`pyproject.toml`](<../pyproject.toml>) |

## 子目录

| 子目录 | 内容 | 什么时候进入 |
|---|---|---|
| [`e2e/`](<e2e/README.md>) | Analyst Runtime 的恢复、工具、安全与端到端行为验证 | 修改或核验该职责时 |

## 关键文件

| 文件 | 用途 |
|---|---|
| [`test_adapter_registry.py`](<test_adapter_registry.py>) | 行为或合同测试入口 |
| [`test_agent_loop_runtime.py`](<test_agent_loop_runtime.py>) | 行为或合同测试入口 |
| [`test_composio_agent_flow.py`](<test_composio_agent_flow.py>) | 行为或合同测试入口 |
| [`test_cron_service.py`](<test_cron_service.py>) | 行为或合同测试入口 |
| [`test_duplicate_message_in_context.py`](<test_duplicate_message_in_context.py>) | 行为或合同测试入口 |
| [`test_exec_tool_guard.py`](<test_exec_tool_guard.py>) | 行为或合同测试入口 |
| [`test_gateway_auth_tool.py`](<test_gateway_auth_tool.py>) | 行为或合同测试入口 |
| [`test_google_credentials_sync.py`](<test_google_credentials_sync.py>) | 行为或合同测试入口 |

## 依赖方向

Runtime 通过稳定 channel、provider、tool 和 workspace Interface 工作；profile 不得绕过工具权限或把未验证输出升级为外部事实。

## 修改规则

未验证 Agent 输出不得写成机器事实、指标或控制命令。

## 验证方式

从 Analyst Runtime 仓库根执行 `uv sync --frozen --extra dev`、`uv run pytest -q -m "not e2e"` 和 `uv build`；e2e 只在所需外部依赖已受控配置时运行。

## 相关文档

- [返回父目录 README](../README.md)
- [Analyst Runtime 根 README](<../README.md>)
