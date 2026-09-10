from __future__ import annotations

import json
import ntpath
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DEFAULT_LOCAL = str(Path.home() / "Desktop" / "Obsidian仓库")
DEFAULT_PORTABLE = "H:\\ObsidianVault\\Obsidian仓库"
DIRECTIONS = ("to_portable", "to_local")


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


class SettingsStore:
    def __init__(self, state_dir: Path | str):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "settings.json"
        self.last_error = ""

    def load(self) -> AppSettings:
        self.last_error = ""
        if not self.path.exists():
            return AppSettings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("设置内容必须为对象")
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
                # Upgrade requires re-enabling an automatic direction in the
                # new explicit UI; never replay a legacy timer silently.
                raw["schedule_enabled"] = False
                raw["bound_source"] = ""
                raw["bound_target"] = ""
            settings = AppSettings(**{key: value for key, value in raw.items() if key in known})
            self._validate(settings)
            return settings
        except (OSError, ValueError, TypeError) as exc:
            self.last_error = f"设置文件无法读取，已使用默认设置：{exc}"
            return AppSettings()

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
        self._validate(settings)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(asdict(settings), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
