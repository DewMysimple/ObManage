from datetime import datetime
from dataclasses import replace

from obmanage.operation_log import LogEntry, append_entry, filter_entries, read_entries


def test_time_feature_event_task_status_and_query_are_combined():
    rows = [LogEntry("2026-09-20 10:00:00", "vault A 预览", "archive", "预览", "one"),
            LogEntry("2026-09-21 11:00:00", "vault B 执行失败", "mirror", "执行", "two", "error"),
            LogEntry("2026-09-21 11:10:00", "vault A 打包", "archive", "打包", "three", "success"),
            LogEntry("", "old timestamp unavailable", "legacy")]
    assert filter_entries(rows, start=datetime(2026,9,21), end=datetime(2026,9,21,11,10),
        feature="archive", event="打包", task="three", level="success", query="VAULT a") == [rows[2]]
    assert filter_entries(rows, feature="archive") == [rows[2], rows[0]]
    assert filter_entries(rows, end=datetime(2026,9,21,11)) == [rows[1], rows[0]]
    assert filter_entries(rows, start=datetime(2026,9,22)) == []


def test_load_all_retained_history_multiline_and_structured_dedup(tmp_path):
    old = [f"[2026-09-20 10:00:00] old {n}" for n in range(3500)]
    (tmp_path / "ui.previous.log").write_text("\n".join(old), encoding="utf-8")
    entry = LogEntry("2026-09-21 10:00:00", "message\nfull error detail", "archive", "打包", "task")
    append_entry(tmp_path, entry)
    append_entry(tmp_path, replace(entry, identifier="second"))
    (tmp_path / "ui.log").write_text((f"[{entry.timestamp}] {entry.message}\n") * 2, encoding="utf-8")
    rows, issues = read_entries(tmp_path)
    assert not issues
    assert len(rows) == 3502
    assert len(filter_entries(rows, query="old 0")) > 0
    assert len(filter_entries(rows, task="task")) == 2
    assert rows[-1].message.endswith("full error detail")


def test_broken_log_is_visible_and_other_records_survive(tmp_path):
    append_entry(tmp_path, LogEntry("2026-09-21 10:00:00", "good"))
    with (tmp_path / "operations.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{bad\n{"timestamp":42,"message":"bad"}\n')
    rows, issues = read_entries(tmp_path)
    assert len(rows) == 1 and rows[0].message == "good"
    assert len(issues) == 2


def test_rotated_records_remain_searchable(tmp_path):
    append_entry(tmp_path, LogEntry("2026-09-20 10:00:00", "x" * 2_000_001))
    append_entry(tmp_path, LogEntry("2026-09-21 10:00:00", "current"))
    rows, issues = read_entries(tmp_path)
    assert not issues and len(rows) == 2
    assert (tmp_path / "operations.1.jsonl").exists()
