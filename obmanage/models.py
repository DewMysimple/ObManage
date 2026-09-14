from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


class SyncError(Exception):
    """A synchronization operation could not safely continue."""


class SyncCancelled(Exception):
    """The user requested cancellation."""


@dataclass
class PlanItem:
    action: str
    relative_path: str
    size: int = 0
    reason: str = ""


@dataclass
class SyncPlan:
    source: str
    target: str
    items: list[PlanItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source_empty: bool = False
    pair_id: str = ""
    created_at: float = field(default_factory=time.time)
    context: dict[str, Any] = field(default_factory=dict, repr=False)
    mode: str = "mirror"
    excluded_source_files: int = 0
    excluded_source_bytes: int = 0
    excluded_target_files: int = 0
    excluded_target_bytes: int = 0

    @property
    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(("add", "update", "rename", "delete", "skip", "exclude", "mkdir", "rmdir", "error"), 0)
        counts.update(Counter(item.action for item in self.items))
        return counts

    @property
    def bytes_to_copy(self) -> int:
        return sum(item.size for item in self.items if item.action in ("add", "update"))

    @property
    def has_changes(self) -> bool:
        return any(item.action not in ("skip", "exclude", "error") for item in self.items)

    @property
    def can_execute(self) -> bool:
        return not self.errors and not any(item.action == "error" for item in self.items)


@dataclass
class Progress:
    phase: str
    message: str = ""
    relative_path: str = ""
    completed_bytes: int = 0
    total_bytes: int = 0
    completed_files: int = 0
    total_files: int = 0


@dataclass
class SyncResult:
    status: str
    copied_files: int = 0
    copied_bytes: int = 0
    deleted_files: int = 0
    deleted_dirs: int = 0
    skipped_files: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    renamed_items: int = 0
