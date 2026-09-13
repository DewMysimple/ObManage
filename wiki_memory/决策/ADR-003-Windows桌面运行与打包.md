---
type: decision
status: active
kind: architecture
importance: high
updated: 2026-09-13
topic: windows-qt-portable-runtime
source_logs:
  - "[[日志/2026-09-10-工程记忆与界面整理]]"
  - "[[日志/2026-09-11-完整内容校验并行优化]]"
  - "[[日志/2026-09-13-压缩包根目录结构约束]]"
supersedes: null
---

# ADR-003｜Windows 桌面运行与打包

## 背景与决策

用户需要无需安装 Python 的中文桌面程序，同时保持引擎可单独验证。采用 Python 3.14、PySide6 Widgets、SQLite 和 PyInstaller `onedir`，界面与引擎通过模型及信号连接。

`SyncWorker` 继承 `QThread`，在 `run()` 中创建引擎及 SQLite 连接，主线程消费进度和结束信号。完整校验可在这个分析线程内部创建最多两个纯读取 worker，但对外进度仍由分析线程发出，SQLite 不进入哈希线程。GUI 与 CLI 预览共用同一 `QLockFile`，引擎实例在进程内也共享任务锁。桌面 GUI 取得实例锁后还监听同一状态目录派生的 `QLocalServer` 通道；第二次启动连接该通道，请求已有窗口恢复、置顶并激活，而不是创建第二个窗口。保持当前 Qt 线程生命周期实现，不能在未验证的情况下替换为其他工作对象模式。

打包使用构建脚本限定子进程 PATH 为 Python/venv 和 Windows 系统目录，以防开发工具附带的同名 ICU DLL 混入 Qt 依赖。每次实质修改完成交付时都生成便携目录和 `ObManage.zip`；ZIP 根层直接包含 EXE、动态库、使用说明和第三方许可证，解压后就是程序根目录，不再套一层目录。使用说明来自[独立指南](../../docs/使用指南.md)，不依赖 GitHub README 的仓库链接和图片。manifest 使用 `asInvoker`、长路径与 PerMonitorV2。

## 理由和影响

避免 GUI 被大文件读取阻塞，依赖和状态边界可检查。`onedir` 要求分发整个目录，不能只拷 EXE。源码启动成功不能证明冻结包依赖正确，必须补独立 EXE 启动验证。

## 验证与来源

[SyncWorker](../../obmanage/ui.py)、[GUI 启动、实例聚焦及 smoke-test](../../obmanage/app.py)、[构建环境 PATH 注释与脚本](../../tools/build.py)、[Windows manifest](../../windows.manifest)、[UI 取消与任务互斥测试](../../tests/test_ui.py)、[实例聚焦测试](../../tests/test_app.py)。具体操作见[Windows 打包与排障](../知识/运维/Windows打包与排障.md)。
