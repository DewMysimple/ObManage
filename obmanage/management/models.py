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
class VaultStatistics:
    """Counts for the active content of one vault.

    ``folders`` excludes the vault root and every excluded subtree.  Markdown
    byte/file counts describe all safely observed regular ``.md`` files;
    ``utf8_characters`` only includes files decoded completely as strict UTF-8.
    ``complete`` is false whenever part of the active tree could not be read.
    """

    vault_path: str
    markdown_files: int = 0
    utf8_characters: int = 0
    markdown_bytes: int = 0
    folders: int = 0
    characters_counted: bool = True
    complete: bool = True


@dataclass(frozen=True, slots=True)
class VaultStatisticsResult:
    statistics: tuple[VaultStatistics, ...] = ()
    issues: tuple[ManagementIssue, ...] = ()

    @property
    def by_path(self) -> dict[str, VaultStatistics]:
        return {item.vault_path: item for item in self.statistics}
