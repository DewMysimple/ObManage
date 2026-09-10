from __future__ import annotations

from datetime import datetime, timedelta

from .settings import AppSettings


class Scheduler:
    """Local-time scheduler. Each tick consumes a due run, including busy ticks."""

    def __init__(self) -> None:
        self.next_run: datetime | None = None
        self.mode = "interval"
        self.interval_minutes = 60
        self.daily_time = "20:00"

    def configure(self, settings: AppSettings, now: datetime | None = None) -> None:
        self.pause()
        self.mode = settings.schedule_mode
        self.interval_minutes = settings.interval_minutes
        self.daily_time = settings.daily_time
        if not settings.schedule_enabled:
            return
        if (settings.bound_source, settings.bound_target) != (settings.source, settings.target):
            return
        if self.mode not in ("interval", "daily") or not 1 <= self.interval_minutes <= 10080:
            raise ValueError("定时设置无效")
        self._advance(now or datetime.now())

    def _advance(self, now: datetime) -> None:
        if self.mode == "interval":
            self.next_run = now + timedelta(minutes=self.interval_minutes)
            return
        hour, minute = map(int, self.daily_time.split(":"))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        self.next_run = candidate

    def tick(self, busy: bool, now: datetime | None = None) -> bool:
        current = now or datetime.now()
        if self.next_run is None or current < self.next_run:
            return False
        self._advance(current)
        return not busy

    def pause(self) -> None:
        self.next_run = None
