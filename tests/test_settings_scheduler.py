from datetime import datetime, timedelta
import json

from obmanage.scheduler import Scheduler
from obmanage.settings import AppSettings, SettingsStore


def test_direction_switch_keeps_endpoints_and_clears_automatic_binding():
    settings = configured()
    local, portable = settings.local_path, settings.portable_path
    settings.set_direction("to_local")
    assert (settings.source, settings.target) == (portable, local)
    assert (settings.local_path, settings.portable_path) == (local, portable)
    assert not settings.schedule_enabled
    assert not settings.bound_source and not settings.bound_target
    settings.set_direction("to_portable")
    assert (settings.source, settings.target) == (local, portable)


def test_reverse_direction_settings_roundtrip(tmp_path):
    settings = configured()
    settings.set_direction("to_local")
    store = SettingsStore(tmp_path)
    store.save(settings)
    restored = store.load()
    assert restored.direction == "to_local"
    assert restored.source == "H:\\mirror"
    assert restored.local_path == "C:\\source"
    assert restored.portable_path == "H:\\mirror"


def test_legacy_reverse_settings_preserve_source_and_target(tmp_path):
    store = SettingsStore(tmp_path)
    raw = {"source": "H:\\移动仓库", "target": "C:\\本机仓库", "schedule_enabled": True,
           "bound_source": "H:\\移动仓库", "bound_target": "C:\\本机仓库"}
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    settings = store.load()
    assert settings.direction == "to_local"
    assert settings.source == raw["source"] and settings.target == raw["target"]
    assert settings.local_path == raw["target"]
    assert settings.portable_path == raw["source"]
    assert not settings.schedule_enabled


def test_legacy_forward_settings_preserve_paths_but_pause_old_timer(tmp_path):
    store = SettingsStore(tmp_path)
    raw = {"source": "C:\\工作库", "target": "H:\\副本", "schedule_enabled": True,
           "bound_source": "C:\\工作库", "bound_target": "H:\\副本"}
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    settings = store.load()
    assert settings.direction == "to_portable"
    assert (settings.source, settings.target) == (raw["source"], raw["target"])
    assert not settings.schedule_enabled


def test_edit_endpoint_while_reversed_keeps_source_role():
    settings = configured()
    settings.set_direction("to_local")
    settings.set_endpoints("D:\\笔记本", "F:\\移动盘")
    assert (settings.source, settings.target) == ("F:\\移动盘", "D:\\笔记本")
    assert settings.direction == "to_local"


def configured(**kwargs):
    settings = AppSettings(source="C:\\source", target="H:\\mirror", schedule_enabled=True,
                           bound_source="C:\\source", bound_target="H:\\mirror")
    for name, value in kwargs.items():
        setattr(settings, name, value)
    return settings


def test_settings_round_trip_and_recent_paths(tmp_path):
    store = SettingsStore(tmp_path)
    settings = configured(recent_targets=["H:\\镜像", "D:\\副本", "H:\\镜像"])
    store.save(settings)
    actual = store.load()
    assert actual.schedule_enabled
    assert actual.recent_targets == ["H:\\镜像", "D:\\副本"]
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_settings_fall_back_without_scheduling(tmp_path):
    store = SettingsStore(tmp_path)
    store.path.write_text('{"schedule_enabled": true, "interval_minutes": "bad"}', encoding="utf-8")
    assert not store.load().schedule_enabled
    assert store.last_error


def test_target_change_cannot_reuse_old_schedule(tmp_path):
    settings = configured(target="D:\\new-mirror")
    store = SettingsStore(tmp_path)
    store.save(settings)
    assert not store.load().schedule_enabled
    scheduler = Scheduler()
    scheduler.configure(configured(target="D:\\new-mirror"))
    assert scheduler.next_run is None


def test_interval_busy_run_is_consumed_and_not_queued():
    start = datetime(2026, 9, 10, 10)
    scheduler = Scheduler()
    scheduler.configure(configured(interval_minutes=5), start)
    assert not scheduler.tick(False, start + timedelta(minutes=4))
    assert not scheduler.tick(True, start + timedelta(minutes=5))
    assert not scheduler.tick(False, start + timedelta(minutes=5, seconds=1))
    assert scheduler.tick(False, start + timedelta(minutes=10))


def test_sleep_resume_coalesces_missed_runs():
    start = datetime(2026, 9, 10, 10)
    scheduler = Scheduler()
    scheduler.configure(configured(interval_minutes=5), start)
    assert scheduler.tick(False, start + timedelta(hours=8))
    assert not scheduler.tick(False, start + timedelta(hours=8, seconds=1))
    assert scheduler.next_run == start + timedelta(hours=8, minutes=5)


def test_daily_schedule_next_local_day_and_pause():
    start = datetime(2026, 9, 10, 20, 0)
    scheduler = Scheduler()
    scheduler.configure(configured(schedule_mode="daily", daily_time="20:00"), start)
    assert scheduler.next_run == datetime(2026, 9, 11, 20)
    assert scheduler.tick(False, datetime(2026, 9, 11, 20, 1))
    assert scheduler.next_run == datetime(2026, 9, 12, 20)
    scheduler.pause()
    assert not scheduler.tick(False, datetime(2027, 1, 1))
