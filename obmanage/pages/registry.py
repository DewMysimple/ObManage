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
    FeatureDescriptor("statistics", "仓库统计", "仓库统计", "统计容量、文件类型、Markdown 和字符"),
    FeatureDescriptor("template_suite", "模板套件部署", "模板套件", "把选定模板组件安全部署到仓库"),
    FeatureDescriptor("obsidian_config", "Obsidian 配置分发", "配置分发", "将一个 .obsidian 配置分发到多个仓库"),
    FeatureDescriptor("templater", "Templater 分发", "Templater", "将 File/Templater 分发到多个仓库"),
    FeatureDescriptor("trash_cleanup", "回收站清理", "回收站", "预览并直接清理仓库的 .trash 内容"),
)
