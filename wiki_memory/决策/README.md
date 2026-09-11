---
type: knowledge
status: active
kind: architecture
importance: high
updated: 2026-09-11
topic: decisions-index
source_logs:
  - "[[日志/2026-09-10-工程记忆与界面整理]]"
  - "[[日志/2026-09-11-多页面仓库管理升级]]"
supersedes: null
---

# 工程决策

以下决策记录已有实现及用户明确约束。本次建记忆没有另行改变同步策略。

| 决策 | 当前作用 |
| --- | --- |
| [ADR-001 方向与删除边界](./ADR-001-方向与删除边界.md) | 明确来源、只修改目标、失败停止清理 |
| [ADR-002 内容等价与增量记录](./ADR-002-内容等价与增量记录.md) | 首次哈希、独立端点快照、往返复用 |
| [ADR-003 Windows 桌面运行与打包](./ADR-003-Windows桌面运行与打包.md) | Qt 工作线程、便携目录和 DLL 搜索环境 |
| [ADR-004 工程记忆与每轮交付](./ADR-004-工程记忆与每轮交付.md) | 来源审计、已授权事实、每轮 commit/push |
| [ADR-005 多页面管理与持久恢复](./ADR-005-多页面管理与持久恢复.md) | 六页应用壳、事务式部署/清理和全局恢复门禁 |

新决定使用[决策模板](../模板/决策模板.md)。`proposed` 表示尚未采用；现行选择为 `active`；替代后保留旧页并标记 `superseded`，不删除历史。
