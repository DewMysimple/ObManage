# Third-party components

ObManage uses the following unmodified third-party components. The portable build keeps Qt libraries as replaceable dynamic libraries in `_internal`; Python application source is included alongside the development project.

- Python 3.14 — Python Software Foundation License. https://www.python.org/psf/license/
- PySide6 / Shiboken6 6.11.2 and Qt 6 — available under the GNU LGPL version 3 and applicable component licenses. https://doc.qt.io/qtforpython-6/licenses.html ; https://www.qt.io/licensing/open-source-lgpl-obligations
- PyInstaller 6.22.0 — GPL version 2 or later with the bootloader exception permitting distribution of bundled applications. https://pyinstaller.org/en/stable/license.html
- SQLite — public domain. https://www.sqlite.org/copyright.html

LGPL version 3 and its GPL version 3 terms are included in `licenses`. Qt for Python sources are available at https://code.qt.io/cgit/pyside/pyside-setup.git/ and Qt sources at https://code.qt.io/cgit/qt/ . Exact installed dependency versions are recorded in `requirements.txt` and `requirements-dev.txt`.
