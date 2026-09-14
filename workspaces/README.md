# Workspace templates

<!-- project-nav
{"entrypoints":[],"owners":[],"truth":{},"production":"profile-source"}
-->

## 目录职责

`analyst-runtime/workspaces` 的第一方项目内容与导航边界。它属于 **workspaces** 边界。当前目录不替代父级架构说明，也不接管其他 Module 的数据或安全决策。

生产属性：本目录是可选择的 workspace profile 源码；只有部署时明确选择的 profile 才进入对应运行环境。

## 从这里开始

| 你要做的事情 | 下一步 |
|---|---|
| 继续查找实现或资料 | 进入 [`data-analyst/`](<data-analyst/README.md>) |
| 运行该区域验证 | 从 Runtime 仓库根执行测试与 build；依赖入口见 [`pyproject.toml`](<../pyproject.toml>) |

## 子目录

| 子目录 | 内容 | 什么时候进入 |
|---|---|---|
| [`data-analyst/`](<data-analyst/README.md>) | Analyst Runtime 的 `data-analyst` system policy、persona 与工具配置 | 修改或核验该职责时 |
| [`executive/`](<executive/README.md>) | Analyst Runtime 的 `executive` system policy、persona 与工具配置 | 修改或核验该职责时 |
| [`manufacturing-analyst/`](<manufacturing-analyst/README.md>) | Analyst Runtime 的 `manufacturing-analyst` system policy、persona 与工具配置 | 修改或核验该职责时 |

## 关键文件

本目录没有直属程序入口；内容通过上方子目录导航。

## 依赖方向

Runtime 通过稳定 channel、provider、tool 和 workspace Interface 工作；profile 不得绕过工具权限或把未验证输出升级为外部事实。

## 修改规则

未验证 Agent 输出不得写成机器事实、指标或控制命令。

## 验证方式

从 Analyst Runtime 仓库根执行 `uv sync --frozen --extra dev`、`uv run pytest -q -m "not e2e"` 和 `uv build`；e2e 只在所需外部依赖已受控配置时运行。

## 相关文档

- [返回父目录 README](../README.md)
- [Analyst Runtime 根 README](<../README.md>)

Each folder is a complete product-level system-prompt configuration. Copy its
`SOUL.md`, `AGENTS.md`, and `workspace.json` into the active product
`workspace/`. Keep the template directories immutable; Analyst Runtime writes
memory and session state into the active workspace at runtime.

```bash
cp analyst-runtime/workspaces/data-analyst/{SOUL.md,AGENTS.md,workspace.json} workspace/
```

- `SOUL.md` defines who the agent is, such as a data analyst or an executive.
- `AGENTS.md` defines how that role works and delivers answers.
- `workspace.json` enables optional runtime profiles. An absent or empty list
  means the generic Analyst Runtime only.

Choose runtime profiles when a product workspace is created. If a profile has
already persisted owned state, Analyst Runtime fails closed when that profile is
removed; migrate or remove that state explicitly before changing profiles.

`trusted_gateway` is optional. Set its `runtime` and `project_id` only when an
authenticated product gateway must append request-scoped system policy. If it
is absent, Analyst Runtime rejects all such metadata; the normal `SOUL.md` and
`AGENTS.md` prompt still works.

Runtime state (`memory/`, `sessions/`, data and artifacts) belongs to the
active product workspace and must not be copied into these templates.
