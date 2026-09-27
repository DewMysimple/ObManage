"""Optional, explicitly configured desktop integrations; no cloud credentials."""
import os
from pathlib import Path


def open_baidu(executable: str) -> str:
    path = Path(executable)
    if path.name.casefold() != "baidunetdisk.exe" or not path.is_file():
        raise ValueError("请选择有效的 BaiduNetdisk.exe。")
    if os.name != "nt":
        raise OSError("百度网盘联动仅支持 Windows。")
    # The client handles its own single-instance activation, including tray state.
    # startfile takes one literal path and never evaluates a shell command.
    os.startfile(str(path.resolve()))
    return "已请求打开百度网盘；上传进度请在网盘客户端查看。"
