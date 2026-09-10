"""Build a portable Windows application using the current Python environment."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", "ObManage", "--manifest", str(ROOT / "windows.manifest"),
        "--icon", str(ROOT / "assets" / "obmanage.ico"),
        "--exclude-module", "PySide6.QtWebEngineCore", "--exclude-module", "PySide6.QtWebEngineWidgets",
        "--exclude-module", "PySide6.QtQml", "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "tkinter", "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
        str(ROOT / "run_obmanage.py"),
    ], cwd=ROOT, env=build_environment, check=True)
    shutil.copy2(ROOT / "README.md", ROOT / "dist" / "ObManage" / "使用说明.md")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", ROOT / "dist" / "ObManage" / "THIRD_PARTY_NOTICES.md")
    shutil.copytree(ROOT / "licenses", ROOT / "dist" / "ObManage" / "licenses", dirs_exist_ok=True)
    print(ROOT / "dist" / "ObManage" / "ObManage.exe")


if __name__ == "__main__":
    main()
