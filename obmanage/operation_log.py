"""Structured operation records, with read-only compatibility for older UI logs."""
from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class LogEntry:
    timestamp: str
    message: str
    feature: str = "system"
    event: str = "记录"
    task: str = ""
    level: str = "info"
    identifier: str = ""

    @property
    def when(self):
        try:
            value = datetime.fromisoformat(self.timestamp)
            return value.astimezone().replace(tzinfo=None) if value.tzinfo else value
        except ValueError:
            return None

    def display(self, title):
        time = self.timestamp or "时间未知"
        return f"[{time}] {title} · {self.event} · {self.level_label}  {self.message}"

    @property
    def level_label(self):
        return {"info": "记录", "success": "成功", "error": "失败", "warning": "提醒", "cancelled": "取消"}.get(self.level, self.level)


def new_entry(message, feature="system", event="记录", task="", level="info"):
    return LogEntry(datetime.now().isoformat(sep=" ", timespec="seconds"), str(message),
                    feature, event, task, level, uuid.uuid4().hex)


def _legacy(text):
    rows = []
    current = None
    for line in text.splitlines():
        match = re.match(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*)$", line)
        if match:
            if current:
                rows.append(current)
            current = LogEntry(match[1], match[2], "legacy", "历史记录")
        elif current:
            current = LogEntry(current.timestamp, current.message + "\n" + line, "legacy", "历史记录")
        elif line.strip():
            rows.append(LogEntry("", line, "legacy", "历史记录"))
    if current:
        rows.append(current)
    return rows


def read_entries(state_dir):
    state = Path(state_dir)
    legacy, structured, issues = [], [], []
    for name in ("ui.previous.log", "ui.log"):
        path = state / name
        if path.exists():
            try:
                legacy.extend(_legacy(path.read_text(encoding="utf-8", errors="replace")))
            except OSError as exc:
                issues.append(f"{name} 读取失败：{exc}")
    for name in [f"operations.{n}.jsonl" for n in range(4, 0, -1)] + ["operations.jsonl"]:
        path = state / name
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    try:
                        data = json.loads(line)
                        row = LogEntry(**data)
                        if any(not isinstance(value, str) for value in asdict(row).values()):
                            raise ValueError("记录字段类型错误")
                        structured.append(row)
                    except (ValueError, TypeError):
                        issues.append(f"{name} 第 {line_number} 行无法解析，已跳过。")
        except (OSError, UnicodeError) as exc:
            issues.append(f"{name} 读取不完整：{exc}")
    # New records also have a human-readable ui.log copy. Preserve duplicate
    # events, suppress only the corresponding number of compatibility copies.
    duplicates = Counter((row.timestamp, row.message) for row in structured)
    old = []
    for row in legacy:
        key = row.timestamp, row.message
        if duplicates[key]:
            duplicates[key] -= 1
        else:
            old.append(row)
    return sorted(old + structured, key=lambda e: e.timestamp), issues


def append_entry(state_dir, entry):
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    path = state / "operations.jsonl"
    if path.exists() and path.stat().st_size > 2_000_000:
        for number in range(4, 0, -1):
            source = state / (f"operations.{number - 1}.jsonl" if number > 1 else "operations.jsonl")
            if source.exists():
                source.replace(state / f"operations.{number}.jsonl")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")


def filter_entries(entries, *, start=None, end=None, feature="", event="", task="", level="", query=""):
    words = query.casefold().split()
    rows = []
    for row in entries:
        if (feature and row.feature != feature or event and row.event != event or
                task and row.task != task or level and row.level != level):
            continue
        if start is not None or end is not None:
            when = row.when
            if when is None or start is not None and when < start or end is not None and when > end:
                continue
        if any(word not in row.message.casefold() for word in words):
            continue
        rows.append(row)
    return list(reversed(rows))
