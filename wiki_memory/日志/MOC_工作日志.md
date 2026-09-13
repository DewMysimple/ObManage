---
type: moc
status: active
kind: process
importance: high
updated: 2026-09-13
topic: work-log-index
source_logs: []
supersedes: null
---

# 工作日志 MOC

> 单一工作日志索引，按更新时间倒序。任务类型通过 `kind` 元数据区分。

| 时间 | 类型 | 目标 | 状态 | 主题 | 日志 |
| --- | --- | --- | --- | --- | --- |
| 2026-09-13 | feature | - | archived | repeated-launch-focus | [重复启动聚焦窗口](./2026-09-13-重复启动聚焦窗口.md) |
| 2026-09-13 | maintenance | 基于提交 `64552a8` 构建最新 Windows 便携目录，并生成 ZIP 交付包。 | archived | portable-package-build | [2026-09-13｜最新便携包构建](./2026-09-13-最新便携包构建.md) |
| 2026-09-13 | feature | - | archived | newest-first-log-and-parallel-statistics | [操作日志倒序与统计提速](./2026-09-13-操作日志倒序与统计提速.md) |
| 2026-09-13 | feature | 扫描回收站后默认选择全部非空项，移除回收站页的路径复制入口，并将新清理改为确认后直接删除、不创建隔离备份。 | archived | direct-trash-cleanup | [2026-09-13｜回收站直接清理](./2026-09-13-回收站直接清理.md) |
| 2026-09-13 | maintenance | 把“每次完整对话的实质修改都提交、推送并生成最新 `ObManage.zip`”加入工程约束，并确保 ZIP 解压后直接就是程序根目录。 | archived | flat-portable-archive | [2026-09-13｜压缩包根目录结构约束](./2026-09-13-压缩包根目录结构约束.md) |
| 2026-09-11 | feature | 缩短本机仓库与移动盘之间约五分钟的完整内容校验，同时继续逐字节读取两端、保持 SHA-256、来源只读和并发变化 fail-closed。 | archived | parallel-deep-content-verification | [2026-09-11｜完整内容校验并行优化](./2026-09-11-完整内容校验并行优化.md) |
| 2026-09-11 | feature | 把五个旧版仓库小工具的业务意图重新设计为 ObManage 的独立页面和安全业务引擎，同时保留仓库镜像为第一页。 | archived | multi-page-repository-management | [2026-09-11｜多页面仓库管理升级](./2026-09-11-多页面仓库管理升级.md) |
| 2026-09-11 | bug | 解决本机仓库同步到 exFAT 移动盘时，在首个文件已正确落盘后仍报告“提交后目标文件发生变化”的问题。 | archived | exfat-post-replace-validation | [2026-09-11｜exFAT 提交校验修复](./2026-09-11-exFAT提交校验修复.md) |
| 2026-09-11 | bug | 解决本机向移动盘镜像时，目标独有的 Windows 只读文件在删除阶段报告“拒绝访问”的问题，并覆盖只读旧目标的更新。 | archived | windows-readonly-target-mutation | [2026-09-11｜Windows 只读目标处理修复](./2026-09-11-Windows只读目标处理修复.md) |
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
