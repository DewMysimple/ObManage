"""Typed, UI-independent contracts for Obsidian vault management."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ManagementIssue:
    """A path or input that could not be inspected completely and safely."""

    code: str
    message: str
    path: str | None = None
    severity: Literal["error", "warning"] = "error"


@dataclass(frozen=True, slots=True)
class VaultInfo:
    """One unique physical Obsidian vault found by :func:`discover_vaults`."""

    path: str
    name: str
    parent_path: str | None = None

    @property
    def is_nested(self) -> bool:
        return self.parent_path is not None


@dataclass(frozen=True, slots=True)
class OpenedVaultCandidates:
    """Read-only paths obtained from Obsidian's local configuration."""

    paths: tuple[str, ...] = ()
    issues: tuple[ManagementIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class VaultCatalogResult:
    vaults: tuple[VaultInfo, ...] = ()
    issues: tuple[ManagementIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class FileTypeStatistics:
    """Extension-based totals for one human-readable file category."""

    category: str
    label: str
    files: int = 0
    total_bytes: int = 0
    extensions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VaultStatistics:
    """Counts and logical sizes for the active content of one vault.

    ``folders`` excludes the vault root and every excluded subtree.  File and
    byte totals include every safely observed regular file.  Markdown byte/file
    counts are retained as a focused subset; ``utf8_characters`` only includes
    files decoded completely as strict UTF-8.  ``complete`` is false whenever
    part of the active tree could not be read or changed during the scan.
    """

    vault_path: str
    markdown_files: int = 0
    utf8_characters: int = 0
    markdown_bytes: int = 0
    folders: int = 0
    characters_counted: bool = True
    complete: bool = True
    total_files: int = 0
    total_bytes: int = 0
    file_types: tuple[FileTypeStatistics, ...] = ()

    @property
    def non_markdown_files(self) -> int:
        return max(0, self.total_files - self.markdown_files)


@dataclass(frozen=True, slots=True)
class VaultStatisticsResult:
    statistics: tuple[VaultStatistics, ...] = ()
    issues: tuple[ManagementIssue, ...] = ()

    @property
    def by_path(self) -> dict[str, VaultStatistics]:
        return {item.vault_path: item for item in self.statistics}

    @property
    def file_types(self) -> tuple[FileTypeStatistics, ...]:
        """Aggregate category totals across every returned vault."""
        totals: dict[str, FileTypeStatistics] = {}
        for vault in self.statistics:
            for item in vault.file_types:
                current = totals.get(item.category)
                totals[item.category] = FileTypeStatistics(
                    category=item.category,
                    label=item.label,
                    files=item.files + (current.files if current else 0),
                    total_bytes=item.total_bytes + (current.total_bytes if current else 0),
                    extensions=tuple(sorted(
                        set(item.extensions) | (set(current.extensions) if current else set()),
                        key=lambda value: (value.casefold(), value),
                    )),
                )
        return tuple(totals.values())
