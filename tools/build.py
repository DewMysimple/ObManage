"""Build a portable Windows application using the current Python environment."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _create_portable_archive(package_dir: Path) -> Path:
    """Create a flat-root ZIP beside the onedir package."""
    archive_base = package_dir.parent / package_dir.name
    archive_path = archive_base.with_suffix(".zip")
    shutil.make_archive(str(archive_base), "zip", root_dir=package_dir)
    return archive_path


def _check_output_path(path: Path) -> None:
    if path.resolve() != path or (
        path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise RuntimeError(f"构建路径不能重定向：{path}")


def _publish_package(staged: Path, package_dir: Path) -> None:
    _check_output_path(package_dir.parent)
    _check_output_path(package_dir)
    if package_dir.exists():
        try:
            shutil.rmtree(package_dir)
        except PermissionError:
            # An open directory handle can prevent removing the empty root
            # even though every previous build file could be removed.
            if not package_dir.is_dir() or any(package_dir.iterdir()):
                raise
    if package_dir.exists():
        _check_output_path(package_dir)
        for item in staged.iterdir():
            item.rename(package_dir / item.name)
    else:
        staged.rename(package_dir)


def main() -> None:
    from make_icon import make_icon

    make_icon(ROOT / "assets" / "obmanage.ico")
    # Native tools on a developer's PATH may ship incompatible DLLs with the
    # same names as Windows system libraries (notably Poppler's ICU vs Qt's
    # Windows ICU dependency). Resolve dependencies using a clean child PATH.
    build_environment = os.environ.copy()
    windows_dir = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    build_environment["PATH"] = os.pathsep.join(map(str, (
        Path(sys.executable).parent, Path(sys.base_prefix), Path(sys.base_prefix) / "DLLs",
        windows_dir / "System32", windows_dir,
    )))
    dist_path, work_path = ROOT / "dist", ROOT / "build"
    package_dir = dist_path / "ObManage"
    for output in (dist_path, work_path, package_dir):
        _check_output_path(output)
    dist_path.mkdir(exist_ok=True)
    pyinstaller = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", "ObManage", "--manifest", str(ROOT / "windows.manifest"),
        "--icon", str(ROOT / "assets" / "obmanage.ico"),
        "--exclude-module", "PySide6.QtWebEngineCore", "--exclude-module", "PySide6.QtWebEngineWidgets",
        "--exclude-module", "PySide6.QtQml", "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "tkinter", "--workpath", str(work_path),
    ]
    archive_path = dist_path / "ObManage.zip"
    with TemporaryDirectory(prefix=".obmanage-build-", dir=dist_path) as staging:
        subprocess.run([*pyinstaller, "--distpath", staging, str(ROOT / "run_obmanage.py")],
                       cwd=ROOT, env=build_environment, check=True)
        staged = Path(staging) / "ObManage"
        shutil.copy2(ROOT / "docs" / "使用指南.md", staged / "使用说明.md")
        shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", staged / "THIRD_PARTY_NOTICES.md")
        shutil.copytree(ROOT / "licenses", staged / "licenses", dirs_exist_ok=True)
        staged_archive = _create_portable_archive(staged)
        _publish_package(staged, package_dir)
        staged_archive.replace(archive_path)
    print(package_dir / "ObManage.exe")
    print(archive_path)


if __name__ == "__main__":
    main()
