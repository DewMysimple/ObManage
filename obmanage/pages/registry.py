from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeatureDescriptor:
    key: str
    title: str
    short_title: str
    description: str


# Static registration keeps every page discoverable in a frozen PyInstaller build.
FEATURES = (
    FeatureDescriptor("mirror", "仓库镜像", "仓库镜像", "本机与移动硬盘之间的增量镜像"),
    FeatureDescriptor("incremental", "仓库增量处理", "仓库增量处理", "逐仓库选择来源，在两端按所选方向更新"),
    FeatureDescriptor("vault_backup", "仓库备份", "仓库备份", "双向同步除视频之外的仓库内容"),
    FeatureDescriptor("statistics", "仓库统计", "仓库统计", "统计容量、文件类型、Markdown 和字符"),
    FeatureDescriptor("comsync", ".comSync", ".comSync", "统一同步配置、Templater 和 File 目录结构"),
    FeatureDescriptor("trash_cleanup", ".Trash", ".Trash", "预览并直接清理仓库的 .trash 内容"),
    FeatureDescriptor("archive", ".Archive", ".Archive", "打包仓库为 ZIP，可排除视频并选择压缩级别"),
)
