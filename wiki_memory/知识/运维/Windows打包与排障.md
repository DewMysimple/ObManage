---
type: knowledge
status: active
kind: operations
importance: high
updated: 2026-09-10
topic: windows-build-troubleshooting
source_logs:
  - "[[日志/2026-09-10-工程记忆与界面整理]]"
supersedes: null
---

# Windows 打包与排障

## 环境与产物

项目使用普通 GIL 版 Python 3.14 x64。运行依赖 PySide6 6.11.2，开发构建使用 PyInstaller 6.22.0 和 pytest 9.1.1，版本以[requirements.txt](../../../requirements.txt)及[requirements-dev.txt](../../../requirements-dev.txt)为准。

执行 `python tools/build.py` 输出 `dist/ObManage/ObManage.exe` 与 `_internal/`，把[docs/使用指南.md](../../../docs/使用指南.md)复制为包内 `使用说明.md`，同时拷贝第三方声明及 `licenses/`。GitHub README 包含仓库链接和示例图，因此不再直接充当包内文档。分发整个目录，单独 EXE 不完整。产物被 Git 忽略，不把本机生成包自动视为公开 Release。

构建脚本会清理并重建已知 `build/` 与 `dist/` 产物。重建前确认旧 EXE 已空闲退出，避免 DLL 被占用；不能为解锁强杀正在同步的进程。手工递归清理前确认绝对目标在项目构建目录内，禁止把计算路径交给其他 shell 删除。

## DLL 与启动检查

构建子进程必须使用脚本中净化后的 PATH，保留 Python/venv 及 Windows 系统目录。开发工具附带的同名 `icuuc.dll` 可能与 Qt 所需 Windows ICU 导出不符；源码可启动而冻结包失败时，先核查收集到的 DLL 与启动日志，不盲目往 `_internal` 复制 DLL。

隐藏参数 `--smoke-test <PNG>` 会打开 UI、保存窗口截图后退出；未指定 `--state-dir` 时自动使用临时隔离状态。要验证方向或已有计划，用临时样例数据与隔离状态准备场景，不让烟测启动真实镜像。

[tools/render_demo.py](../../../tools/render_demo.py)可用合成文件列表生成无私人数据的真实 Qt 预览，默认输出 [docs/images/main-window.png](../../../docs/images/main-window.png)。支持 `--width` / `--height` / `--output` 和 `--maximized`；输出窗口、viewport、列宽总和及水平滚动范围，便于核对表格是否填满。真正最大化需要在首次 `show()` 创建原生窗口后调用 `showMaximized()`，避免 Windows 的启动显示参数吞掉首次最大化请求。该工具只展示构造好的计划，不触发真实扫描或同步，也不能代替冻结 EXE 烟测。

## 本地状态与故障

| 文件 | 作用 |
| --- | --- |
| `%LOCALAPPDATA%/ObManage/settings.json` | 路径、方向、最近目标、定时绑定；临时文件 fsync 后替换 |
| `baselines.sqlite3`（及 WAL/SHM） | 内容等价快照与临时文件所有权，禁止当产品数据提交 |
| `obmanage.log` 及轮换备份 | 启动里程碑、异常和 Qt 消息 |
| `ui.log` / `ui.previous.log` | 界面可读操作记录及原因 |
| `instance.lock` | 同一状态目录的 GUI 实例锁 |

配置损坏回退默认并记录错误，旧版本配置升级暂停定时。保留同目录运行进程时不要任意删除锁；先从托盘退出。遇到失败先查状态日志、盘连接和计划是否过期，再重新分析。删除数据库会丢失加速和临时文件所有权记录，不应作为常规排障第一步。

来源：[build.py](../../../tools/build.py)、[app.py](../../../obmanage/app.py)、[settings.py](../../../obmanage/settings.py)、[UI 日志及退出](../../../obmanage/ui.py)、[第三方声明](../../../THIRD_PARTY_NOTICES.md)。
