from datetime import datetime, timedelta

from obmanage.scheduler import Scheduler
from obmanage.settings import AppSettings, SettingsStore


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
