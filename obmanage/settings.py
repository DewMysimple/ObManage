from __future__ import annotations

import json
import ntpath
import os
import shutil
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_LOCAL = str(Path.home() / "Desktop" / "Obsidian仓库")
DEFAULT_PORTABLE = "H:\\ObsidianVault\\Obsidian仓库"
DIRECTIONS = ("to_portable", "to_local")
SETTINGS_SCHEMA_VERSION = 2


def default_state_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return base / "ObManage"


@dataclass
class AppSettings:
    source: str = DEFAULT_LOCAL
    target: str = DEFAULT_PORTABLE
    recent_targets: list[str] = field(default_factory=list)
    schedule_enabled: bool = False
    schedule_mode: str = "interval"
    interval_minutes: int = 60
    daily_time: str = "20:00"
    bound_source: str = ""
    bound_target: str = ""
    direction: str = "to_portable"

    @property
    def local_path(self) -> str:
        return self.source if self.direction == "to_portable" else self.target

    @property
    def portable_path(self) -> str:
        return self.target if self.direction == "to_portable" else self.source

    def set_endpoints(self, local_path: str, portable_path: str, direction: str | None = None) -> None:
        selected = self.direction if direction is None else direction
        if selected not in DIRECTIONS:
            raise ValueError("同步方向无效")
        previous = (self.source, self.target, self.direction)
        self.direction = selected
        self.source, self.target = ((local_path, portable_path) if selected == "to_portable"
                                    else (portable_path, local_path))
        if previous != (self.source, self.target, self.direction):
            self.schedule_enabled = False
            self.bound_source = ""
            self.bound_target = ""

    def set_direction(self, direction: str) -> None:
        self.set_endpoints(self.local_path, self.portable_path, direction)


@dataclass
class SettingsDocument:
    """Versioned settings shared by the application shell and every feature page."""

    mirror: AppSettings = field(default_factory=AppSettings)
    selected_page: str = "mirror"
    features: dict[str, dict[str, Any]] = field(default_factory=dict)
    schema_version: int = SETTINGS_SCHEMA_VERSION

    def feature(self, key: str) -> dict[str, Any]:
        if not key or key == "mirror":
            raise ValueError("功能设置名称无效")
        return self.features.setdefault(key, {})


class SettingsStore:
    def __init__(self, state_dir: Path | str):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "settings.json"
        self.last_error = ""
        self._document: SettingsDocument | None = None
        self._loaded_legacy = False

    def load(self) -> AppSettings:
        """Compatibility API for mirror-only callers."""
        return self.load_document().mirror

    def load_document(self) -> SettingsDocument:
        self.last_error = ""
        if not self.path.exists():
            self._document = SettingsDocument()
            self._loaded_legacy = False
            return self._document
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("设置内容必须为对象")
            if raw.get("schema_version") == SETTINGS_SCHEMA_VERSION:
                document = self._decode_document(raw)
                self._document = document
                self._loaded_legacy = False
                return document
            if "schema_version" in raw:
                raise ValueError(f"不支持的设置版本：{raw.get('schema_version')}")
            settings = self._decode_mirror(raw)
            document = SettingsDocument(mirror=settings)
            self._document = document
            self._loaded_legacy = True
            return document
        except (OSError, ValueError, TypeError) as exc:
            self.last_error = f"设置文件无法读取，已使用默认设置：{exc}"
            self._document = SettingsDocument()
            self._loaded_legacy = False
            return self._document

    def _decode_document(self, raw: dict[str, Any]) -> SettingsDocument:
        errors: list[str] = []
        features_raw = raw.get("features")
        if not isinstance(features_raw, dict):
            errors.append("功能设置无效，已单独重置。")
            features_raw = {}
        mirror_raw = features_raw.get("mirror", {})
        if not isinstance(mirror_raw, dict):
            errors.append("仓库镜像设置无效，已单独重置。")
            mirror = AppSettings()
        else:
            try:
                mirror = self._decode_mirror(mirror_raw)
            except (ValueError, TypeError) as exc:
                mirror = AppSettings()
                errors.append(f"仓库镜像设置无效，已单独重置：{exc}")
        ui = raw.get("ui", {})
        if not isinstance(ui, dict):
            errors.append("界面设置无效，已单独重置。")
            ui = {}
        selected_page = ui.get("selected_page", "mirror")
        if not isinstance(selected_page, str) or not selected_page:
            errors.append("当前页面设置无效，已单独重置。")
            selected_page = "mirror"
        features: dict[str, dict[str, Any]] = {}
        for key, value in features_raw.items():
            if key == "mirror":
                continue
            if isinstance(key, str) and isinstance(value, dict):
                features[key] = value
            elif isinstance(key, str):
                # One damaged feature must not erase the mirror configuration.
                errors.append(f"功能 {key} 的设置无效，已单独重置。")
                features[key] = {}
        if errors:
            self.last_error = " ".join(errors)
        return SettingsDocument(
            mirror=mirror,
            selected_page=selected_page,
            features=features,
        )

    def _decode_mirror(self, raw: dict[str, Any]) -> AppSettings:
        raw = dict(raw)
        known = {item.name for item in fields(AppSettings)}
        if "direction" not in raw:
            # Preserve the legacy effective source/target exactly. For the
            # known H -> system-drive workflow, label the fixed endpoints
            # correctly. Unknown custom pairs retain source-as-local.
            source_drive = ntpath.splitdrive(str(raw.get("source", DEFAULT_LOCAL)))[0].casefold()
            target_drive = ntpath.splitdrive(str(raw.get("target", DEFAULT_PORTABLE)))[0].casefold()
            portable_drive = ntpath.splitdrive(DEFAULT_PORTABLE)[0].casefold()
            local_drive = ntpath.splitdrive(DEFAULT_LOCAL)[0].casefold()
            raw["direction"] = ("to_local" if source_drive == portable_drive and target_drive == local_drive
                                else "to_portable")
            # A configuration without direction predates explicit direction
            # binding, so never replay its timer silently.
            raw["schedule_enabled"] = False
            raw["bound_source"] = ""
            raw["bound_target"] = ""
        settings = AppSettings(**{key: value for key, value in raw.items() if key in known})
        self._validate(settings)
        return settings

    @staticmethod
    def _validate(settings: AppSettings) -> None:
        if settings.direction not in DIRECTIONS:
            raise ValueError("同步方向无效")
        for name in ("source", "target", "bound_source", "bound_target"):
            if not isinstance(getattr(settings, name), str):
                raise ValueError(f"{name} 必须为路径文本")
        if not isinstance(settings.recent_targets, list) or not all(
            isinstance(value, str) for value in settings.recent_targets
        ):
            raise ValueError("最近目标必须为路径列表")
        settings.recent_targets = list(dict.fromkeys(settings.recent_targets))[:10]
        if type(settings.schedule_enabled) is not bool:
            raise ValueError("定时开关无效")
        if settings.schedule_mode not in ("interval", "daily"):
            raise ValueError("定时模式无效")
        if type(settings.interval_minutes) is not int or not 1 <= settings.interval_minutes <= 10080:
            raise ValueError("同步间隔必须为 1 至 10080 分钟")
        if not isinstance(settings.daily_time, str):
            raise ValueError("每日时间无效")
        parts = settings.daily_time.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError("每日时间必须为 HH:mm")
        hour, minute = map(int, parts)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("每日时间超出范围")
        settings.daily_time = f"{hour:02}:{minute:02}"
        if (settings.bound_source, settings.bound_target) != (settings.source, settings.target):
            settings.schedule_enabled = False

    def save(self, settings: AppSettings) -> None:
        """Compatibility API that updates only the mirror namespace."""
        document = self._document or self.load_document()
        document.mirror = settings
        self.save_document(document)

    def save_document(self, document: SettingsDocument) -> None:
        if document.schema_version != SETTINGS_SCHEMA_VERSION:
            raise ValueError("设置版本无效")
        if not isinstance(document.selected_page, str) or not document.selected_page:
            raise ValueError("当前页面无效")
        if not isinstance(document.features, dict) or not all(
            isinstance(key, str) and isinstance(value, dict)
            for key, value in document.features.items()
        ) or "mirror" in document.features:
            raise ValueError("功能设置无效")
        settings = document.mirror
        self._validate(settings)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self._loaded_legacy and self.path.exists():
            backup = self.state_dir / "settings.v1.backup.json"
            if not backup.exists():
                shutil.copy2(self.path, backup)
        temporary = self.path.with_suffix(".json.tmp")
        payload = {
            "schema_version": SETTINGS_SCHEMA_VERSION,
            "ui": {"selected_page": document.selected_page},
            "features": {
                **document.features,
                "mirror": asdict(settings),
            },
        }
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        self._document = document
        self._loaded_legacy = False
