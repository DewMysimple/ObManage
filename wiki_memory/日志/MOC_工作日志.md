---
type: moc
status: active
kind: process
importance: high
updated: 2026-09-11
topic: work-log-index
source_logs: []
supersedes: null
---

# 工作日志 MOC

> 单一工作日志索引，按更新时间倒序。任务类型通过 `kind` 元数据区分。

| 时间 | 类型 | 目标 | 状态 | 主题 | 日志 |
| --- | --- | --- | --- | --- | --- |
| 2026-09-11 | bug | 解决本机仓库同步到 exFAT 移动盘时，在首个文件已正确落盘后仍报告“提交后目标文件发生变化”的问题。 | archived | exfat-post-replace-validation | [2026-09-11｜exFAT 提交校验修复](./2026-09-11-exFAT提交校验修复.md) |
| 2026-09-10 | maintenance | 按用户提供模板建立 ObManage 工程记忆，优化公开产品说明和 Agent 规范，精简界面顶部与差异表，并按用户约束提交、推送本轮修改。 | archived | memory-and-ui-cleanup | [2026-09-10｜工程记忆与界面整理](./2026-09-10-工程记忆与界面整理.md) |

## 使用方式

- 从仓库根目录执行 `python wiki_memory/工具/memory_lint.py index` 生成或刷新。
- 查询时先阅读当前状态，再按关键词定位日志。
- 历史日志是审计记录，不应直接覆盖当前状态。

## 入口

- [ObManage 工程记忆](../README.md)
- [记忆维护协议](../AGENTS.md)
- [工作日志说明](./README.md)
- [当前项目概览](../当前状态/项目概览.md)
- [当前系统架构](../当前状态/系统架构.md)
