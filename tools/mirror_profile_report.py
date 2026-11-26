"""Path-free presentation of one mirror observation; private data stays separate."""
from __future__ import annotations

import csv
import io


NOTES = [
    "阶段可能嵌套或并行；累计计时不能相加作为总墙钟或互斥百分比。",
    "metadata_queries 是已计量的 Python API 请求数，不是物理磁盘 IOPS；hash_bytes 不是总线流量。",
    "系统缓存未知，未控制杀毒软件；单轮数据不代表稳定平均值或统计置信区间。",
    "未做无插桩 A/B 对照；计时、锁和计数存在未量化开销。",
    "快速基线可能跳过受检元数据完全不变的内容修改；deep 为引擎校验，不是独立第三方审计。",
    "复制源读取含同步 SHA-256；stage_write 为 Python 写调用，不含全部关闭缓冲或掉电持久保证。",
    "sqlite_commits/sqlite_commit 仅统计显式提交 API；不是底层磁盘刷新次数。",
    "无逐文件时间线和慢文件排名；仅保存有界阶段汇总和进度，避免大量日志与路径泄露。",
]


def mechanism():
    return """# 普通镜像实际调用链与计量范围

- `obmanage/ui.py:SyncWorker.run` 在受控 QThread 内调用 `SyncEngine.analyze/execute`；`MainWindow.analyze/start_sync/_execute_plan` 管理预览、方向确认、单任务和恢复门禁。工具只测引擎及自身前置操作，不包含 GUI 渲染时间。
- `obmanage/app.py:acquire_instance_lock` 以默认状态目录的 QLockFile 互斥；`--preview` 只分析，不能执行，也没有本任务所需完整报告。工具持有同一实例锁，检查锁所有权并复用 `management/recovery.py:require_recovery_clear`，不另开 state 绕过门禁。
- `obmanage/engine.py:SyncEngine._analyze` 调用 `validate_roots`，按 source_scan/target_scan 顺序执行 `_scan`。`_scan` 是栈式深度遍历，每个项目用 `paths.snapshot` 重新 lstat，每个目录核验祖先链后真实 scandir；普通 mirror 包含隐藏项、视频、配置、回收站、空目录和子仓库，仅保留既有部署恢复目录排除。
- `scanning/session.py:ScanSession` 服务于发现/统计和集合视图；本次普通镜像走 `_scan`，不把历史会话当成执行授权。`scanning/io.py:scandir` 计量创建迭代器和 next 调用，enumeration 不包括消费者处理项目的时间。
- `_plan_from_manifests` 的 planning 包含两方向基线读取、失效、比较、必要的内容读取和 SQLite 写入。快速模式只有双端完整快照精确匹配时命中 baseline_hits；缺失直接 add，大小不同直接 update；同大小且无有效基线调用 `_hash_pair`。deep 先退役旧记录再重新比较内容，不为明显大小差异读取内容。
- `_hash_pair` 异卷用固定两个 worker 并行读取同一文件两端，`copy_context().run` 传播 ScanMetrics；同卷串行。source_sha256/target_sha256 包括 `_hash_file` 的前后及句柄核验；其 hash_bytes 来自 `reading.hash_stream` 的实际流读取计数。失败汇合线程，不把失败当作相同。
- `_analyze` 在规划后重新核验根身份，并调用 `_validate_analysis_manifests` 再次完整遍历两端。analysis_final_source/target 嵌套在 final_revalidation；根身份检查不在该方法计时内，包含在 analysis 总墙钟。
- 确认绑定本进程新计划、完整清单、路径/卷/根/锚点、模式、统计、预览 ID 和单调时钟期限；本地 console 输入同时确认外部写入已暂停。有删除再独立确认文件及目录数量。输入后额外调用 validate_plan，execute 自身仍完整执行 _validate_plan，不替代其检查。默认拒绝缺失目标父目录；仅另获授权后显式 --create-target-parent，可将唯一缺失直接父目录及锚点身份纳入同一摘要与现场确认，预检后独占 mkdir 一次，拒绝并发出现或多级缺失。
- `execute` 在 execute_preflight 内真实重扫两端；新建目标及目录后，`_start_copy` 登记同目标卷临时文件；`_write_copy_temp` 使用 `copying.copy_stream_and_hash` 读取、暂存并同步计算来源摘要。异卷单来源 worker 最多提前一文件；主线程同时校验当前暂存，顺序发布、更新基线。线程上下文显式传播，source_to_stage 与 staged_sha256 可重叠。
- `_verify_commit_copy` 完整读取 staged_sha256，设置时间戳，atomic_publish 替换目标。若最终身份/时间变化，postwrite_recheck 再读最终目标。copy_baseline 包含 BaselineStore.finalize_copy 的逐文件事务；sqlite_finalize 与 sqlite_commit 嵌套，不可相加。
- copy_pipeline 覆盖复制循环及 worker 汇合/暂存清理，是不重叠的全复制流水线墙钟；不含目录准备和最终屏障。source_read 和 stage_write 是其内 Python 流调用累计时间，不能反推外接盘标称速度。
- 有复制、改名或建目录时，final_barrier 复核来源并检查预期目标。本地 exFAT 来源可凭本轮持续有效的目录及文件读取缓存复核，仅重读未覆盖的范围；source_reused_skipped_files 记录该阶段免查的跳过文件数。纯删除且没有登记暂存时，紧邻删除的 execute_preflight 同时完成双端检查，计入 predelete_reused_preflight。delete_stage 按原计划逐项删除。删除后的 post_delete_source 和 post_delete_target 分别计量两端的终态清单检查。
- 后验 post_quick_rescan 与 post_deep_verify 分开调用普通镜像 analyze，独立墙钟，不计入 execute。对来源前后清单只能说明受检元数据稳定；初始未读取的内容没有前测摘要，不声称证明绝对零写入，读取可能触发系统访问时间更新。
- ScanMetrics 的可选 detail_phases 只保留固定范围的计数和首末时点汇总；perf_counter_ns 计算耗时，自然时间仅作索引。API 调用可能来自 OS 缓存，fstat 和卷 WinAPI 等并非全部包含在 metadata_queries。所有结果必须以本轮 JSON 为准，未发生阶段为 null / NOT_RUN。
"""


def hotspots(data):
    rows = []
    for name, stage in data["stages"].items():
        if stage["duration_ms"] is not None:
            rows.append(dict(stage=name, duration_ms=stage["duration_ms"], status=stage["status"],
                             scope="top_level_wall", overlapping="false"))
        for phase_name, duration in stage.get("metrics", {}).get("timing_ms", {}).items():
            if phase_name != "total":
                rows.append(dict(stage=name + "." + phase_name, duration_ms=duration,
                                 status="OBSERVED", scope="inclusive_accumulated", overlapping="true"))
    return sorted(rows, key=lambda row: row["duration_ms"], reverse=True)


def csv_report(rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=("stage", "duration_ms", "status", "scope", "overlapping"))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def number(value):
    return "NOT_RUN / 未测" if value is None else f"{value:,}"


def summary(data):
    p, result, env = data["plan"], data["result"], data["environment"]
    phases = []
    for name, stage in data["stages"].items():
        phases.append(f"| {name} | {number(stage['duration_ms'])} | {stage['status']} | 否（顶层） |")
        metric = stage.get("metrics", {})
        for key in ("source_scan", "target_scan", "planning", "source_sha256", "target_sha256",
                    "final_revalidation", "analysis_final_source", "analysis_final_target",
                    "execute_preflight", "preflight_source", "preflight_target", "copy_pipeline",
                    "source_to_stage", "staged_sha256", "atomic_publish", "copy_baseline", "final_barrier",
                    "barrier_source", "barrier_target", "delete_stage", "post_delete_revalidation",
                    "post_delete_source", "post_delete_target", "sqlite_open", "sqlite_read", "sqlite_write", "sqlite_commit"):
            if key in metric.get("timing_ms", {}):
                counts = metric.get("scopes", {}).get(key, {})
                if counts.get("source_selective_checks"):
                    work = (f"枚举 {counts.get('directory_enumerations', 0)} 个目录，"
                            f"单查 {counts.get('source_rechecked_paths', 0)} 个文件，"
                            f"复用 {counts['source_reused_skipped_files']} 个跳过文件")
                elif "directory_enumerations" in counts:
                    work = f"枚举 {counts['directory_enumerations']} 个目录"
                else:
                    work = f"{counts.get('hash_bytes', '—')} hash bytes"
                phases.append(f"| ↳ {key} | {number(metric['timing_ms'][key])} | "
                              f"{work} | 是，嵌套/可能并行 |")
    top = hotspots(data)[:5]
    ranking = "\n".join(f"| {i} | {row['stage']} | {number(row['duration_ms'])} ms | 本轮计量 JSON | "
                        f"{'实测总墙钟' if row['scope'] == 'top_level_wall' else '实测累计，范围重叠'} |"
                        for i, row in enumerate(top, 1)) or "| — | 未执行任何可测阶段 | — | — | 无 |"
    reasons = ", ".join(data["blockers"]) or "无"
    notes = "\n".join("- " + line for line in data["measurement_notes"])
    return f"""# ObManage 仓库镜像真实执行性能报告（脱敏）

## 1. 本次执行是否真实发生
- 状态：{data['status']}；run_id：{data['run_id']}
- 真实执行：{data['execution_performed']}；用户现场确认：{data['operator_confirmed']}
- 方向：本机 {env.get('source_device_type') or '未判定设备'} → 移动端 {env.get('target_device_type') or '未判定设备'}
- 功能：仓库镜像 / mirror / 快速分析
- Git commit / ObManage 版本：{data['repository']['git_commit']} / {data['repository']['obmanage_version']}；dirty={data['repository']['git_dirty']}
- 源码指纹：{data['repository']['implementation_sha256']}
- 操作系统 / Python / 目标文件系统：{env['os']} / {env['python']} / {env.get('target_fs') or '未测'}
- 目标最初存在：{env.get('target_original_exists')}；直接父目录存在：{env.get('target_parent_exists')}

## 2. 变更工作量
| 新增文件 | 更新文件 | 重命名 | 删除文件 | 新增/删除目录 | 跳过 | 预计复制字节 | 实际复制字节 |
| ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| {number(p.get('add_files'))} | {number(p.get('update_files'))} | {number(p.get('rename_items'))} | {number(p.get('delete_files'))} | {number(p.get('add_dirs'))}/{number(p.get('delete_dirs'))} | {number(p.get('skip_files'))} | {number(p.get('bytes_to_copy'))} | {number(result.get('copied_bytes'))} |

实际复制/删除文件/删除目录：{number(result.get('copied_files'))} / {number(result.get('deleted_files'))} / {number(result.get('deleted_dirs'))}。未执行时 null 不代表已运行零耗时。

## 3. 各阶段实测耗时
| 阶段 | 总墙钟或累计 ms | 状态或读取量 | 是否与其他阶段重叠 |
| --- | ---: | --- | --- |
{chr(10).join(phases)}

- 分析总时间：{number(data['timing_ms']['analysis_total'])} ms
- 实际 execute 总时间：{number(data['timing_ms']['execute_total'])} ms
- 快速复扫：{number(data['timing_ms']['post_quick_rescan'])} ms
- 完整内容校验：{number(data['timing_ms']['post_deep_verify'])} ms
- 有效镜像吞吐：{data['throughput']['effective_mirror_MiB_s']} MiB/s；复制阶段吞吐：{data['throughput']['copy_phase_MiB_s']} MiB/s
- 所有累计阶段均不可与其父阶段相加，不能据此推算复制总用时。

## 4. 扫描工作机制（基于源码 + 本轮数据）
- 机制与函数位置见同目录 scan_mechanism.md；代码解释与本轮观测分开。
- 初始深度枚举两端，按快照基线/大小/必要 SHA 规划，再完整末次复核。
- SQLite 基线命中/失效请求：{number(data['scan_counters'].get('baseline_hits'))} / {number(data['scan_counters'].get('baseline_stale_records'))}；哈希流字节：{number(data['scan_counters'].get('hash_bytes'))}。
- 本轮分析 Python 元数据请求/枚举调用/枚举条目：{number(data['scan_counters'].get('metadata_queries'))} / {number(data['scan_counters'].get('directory_enumerations'))} / {number(data['scan_counters'].get('directory_entries'))}。分端范围计数见 JSON stages.analysis.metrics.scopes。
- 缺失目标的 target_scan 只观察缺失状态，没有实际子树枚举，不能与已有来源树比较介质速度。

## 5. 性能瓶颈 Top 5
| 排名 | 阶段/问题 | 观测耗时与工作量 | 证据 | 可信度 |
| --- | --- | --- | --- | --- |
{ranking}

以上是观测范围排名，有包含关系；没有复制数据时，真实复制瓶颈 Top 3 无法判定。元数据/API 热点的耗时包含 OS 等待，不等于纯 CPU 开销。

## 6. 正确性与风险
- 引擎结果：{result['engine_status']}；阻断原因代码：{reasons}。
- 快速复扫/完整内容校验/独立审计：{data['verification']['quick']['status']} / {data['verification']['deep']['status']} / {data['verification']['independent_audit']['status']}。
- 来源前后受检元数据稳定：{data['verification'].get('source_metadata_stable')}；没有完整前测摘要时不推断来源字节绝对不变。
- 未执行项目和原因、阶段状态、错误类别见 run_metrics.json。原始错误与逐文件计划只在私有文件中。
{notes}

## 7. 希望 ChatGPT 继续回答的问题
请基于上述真实数据以及源码调用链：
1. 指出最可能的前三个性能瓶颈，并区分 I/O 与 CPU/元数据固定成本；
2. 给出不削弱镜像安全和一致性的优化优先级；
3. 说明值得进一步插桩或单独测量的缺失指标；
4. 给出下一轮仅用临时数据验证优化的 A/B 实验方案；
5. 不要把单轮结果当成稳定平均性能，也不要把累计阶段时间相加为总墙钟。
"""


def analysis(data):
    return summary(data) + """
## 8. 完整指标与审计范围
run_metrics.json 保留各顶层阶段 started_at、单调时钟耗时、状态、计数、分端范围、阶段首末时点及异常次数。
timeline.jsonl 为有界顶层里程碑；hotspots.csv 同时标明顶层墙钟与嵌套累计，排名不能直接解释为互斥占比。
private_plan.json 包含全部新增/覆盖/仅大小写改名/删除/跳过项目与预期位置；private_plan.md 可完整展开目标删除清单。
run_local.log 保留完整本地错误。上述三个私有文件不得上传或提交；其他输出只使用固定字段与匿名聚合。

目标父目录缺失时默认停止；操作者可先准备该父目录，再在本地交互终端以 --execute --verify-deep 运行新预览并输入当下短语。
只有另获父目录创建授权并显式 --create-target-parent 时，工具才把唯一直接父目录的独占 mkdir 加入本次现场确认；不递归创建祖先。
非交互输入、过期、拒绝或安全门禁失败后均不会调用 execute；父目录准备与镜像执行分别计时。
一次进程最多调用一次普通 SyncEngine.execute，无自动补跑；失败时保留引擎已提交结果并尽可能只读复扫。
独立 SHA 审计默认 NOT_RUN，避免在完整引擎深度校验之外无提示增加两端全量读取成本。
"""
