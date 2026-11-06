"""Own and verify the disposable trees used by the scanning benchmark."""
from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from obmanage.management.catalog import _read_stable_file
from obmanage.paths import assert_plain_chain, canonical, identity, native, snapshot

MARKER = ".obmanage-benchmark-run"


def _plain_directory(path):
    state = assert_plain_chain(path, leaf_snapshotter=snapshot)
    if state is None or state["kind"] != "dir" or not state["inode"]:
        raise ValueError("Cannot verify the benchmark directory identity")
    return identity(state)


def test_parent(path):
    """Reject a vault, its descendants, and reserved or already-owned paths."""
    selected = _plain_directory(path)
    parent = Path(canonical(os.path.realpath(native(path), strict=True)))
    if _plain_directory(parent) != selected:
        raise ValueError("The test parent changed during resolution")
    if parent == parent.parent:
        raise ValueError("The test parent must be below the volume root")
    for ancestor in (parent, *parent.parents):
        if (ancestor.name.casefold() in {".obsidian", ".trash"}
                or ancestor.name.casefold().startswith(".obmanage-deploy-")
                or any(snapshot(ancestor / name) is not None for name in (".obsidian", MARKER))):
            raise ValueError("The test parent is inside a vault or reserved benchmark/recovery tree")
    return parent, selected


def _cleanup(parent, parent_identity, child, child_identity, marker_identity, run_id):
    try:
        if (child.parent != parent or _plain_directory(parent) != parent_identity
                or _plain_directory(child) != child_identity):
            raise ValueError("directory identity changed")
        marker = snapshot(child / MARKER)
        if (identity(marker) != marker_identity or marker is None or marker["kind"] != "file"
                or _read_stable_file(str(child / MARKER), marker) != run_id):
            raise ValueError("benchmark marker missing or changed")
        if _plain_directory(parent) != parent_identity or _plain_directory(child) != child_identity:
            raise ValueError("directory identity changed while checking the marker")
        shutil.rmtree(native(child))
    except Exception as exc:
        raise RuntimeError(f"Benchmark cleanup stopped. Inspect the isolated directory manually: {child}") from exc


@contextmanager
def sandbox(path=None, *, validate=None):
    parent, parent_identity = test_parent(path if path is not None else Path(tempfile.gettempdir()))
    if validate is not None:
        validate(parent)
    run_id = uuid4().hex.encode("ascii")
    child = Path(tempfile.mkdtemp(prefix=f"obmanage-scan-{run_id.decode()}-", dir=native(parent)))
    # Use the ordinary absolute path for tools and long-path normalization.
    child = parent / child.name
    if _plain_directory(parent) != parent_identity:
        raise RuntimeError(f"Benchmark parent changed. Inspect the isolated directory manually: {child}")
    child_identity = _plain_directory(child)
    if validate is not None:
        validate(child)
    marker = child / MARKER
    with open(native(marker), "xb") as handle:
        handle.write(run_id)
    marker_identity = identity(snapshot(marker))
    try:
        yield child
    finally:
        if validate is not None:
            validate(child)
        _cleanup(parent, parent_identity, child, child_identity, marker_identity, run_id)
