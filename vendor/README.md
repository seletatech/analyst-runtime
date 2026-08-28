# Analyst Runtime release wheelhouse

<!-- heineda-nav
{"entrypoints":[],"owners":[],"truth":{},"production":"analyst-runtime-image"}
-->

## 目录职责

`analyst-runtime/vendor` 的第一方项目内容与导航边界。它属于 **vendor** 边界。当前目录不替代父级架构说明，也不接管其他 Module 的数据或安全决策。

生产属性：本目录由 Analyst Runtime 项目维护；是否进入最终 image/profile 由 Dockerfile、package 配置和 workspace 选择显式决定。

## 从这里开始

| 你要做的事情 | 下一步 |
|---|---|
| 运行该区域验证 | 从 Runtime 仓库根执行测试与 build；依赖入口见 [`pyproject.toml`](<../pyproject.toml>) |

## 子目录

本目录没有需要继续下钻的项目子目录。

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

This directory is a release-time deployment cache for the Linux CPython 3.12
wheels locked by `../uv.lock`. Wheel binaries are intentionally ignored by
Git. A release operator must hydrate this directory before building the image;
`Dockerfile.mvp` verifies package hashes from the locked `uv export` output and
installs without network access. A missing or incomplete wheelhouse fails the
build instead of falling back to the network.

Do not treat this directory as source code or commit wheel binaries.
