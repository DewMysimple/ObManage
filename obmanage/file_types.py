"""Shared filename-based file type rules used across ObManage services."""
from __future__ import annotations

from pathlib import Path


# Keep ambiguous developer-oriented extensions such as ``.ts`` out of this
# list: an Obsidian vault may contain TypeScript source, and filename-based
# filtering must never discard it as MPEG transport stream video.
VIDEO_EXTENSIONS = frozenset({
    ".3g2",
    ".3gp",
    ".asf",
    ".avi",
    ".divx",
    ".f4v",
    ".flv",
    ".m2t",
    ".m2ts",
    ".m2v",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpe",
    ".mpeg",
    ".mpg",
    ".mpv",
    ".mts",
    ".ogv",
    ".qt",
    ".rm",
    ".rmvb",
    ".vob",
    ".webm",
    ".wmv",
})


def is_video_filename(name: str) -> bool:
    """Return whether *name* has a recognized, unambiguous video extension."""
    return Path(name.casefold()).suffix in VIDEO_EXTENSIONS


__all__ = ["VIDEO_EXTENSIONS", "is_video_filename"]
