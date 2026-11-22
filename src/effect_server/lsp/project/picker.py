"""Open the local workstation's native picker in its own GUI main thread."""

import json
from pathlib import Path
import subprocess
import sys
from threading import Lock

_dialog = Lock()


def browse(kind: str, initial: str):
    if not _dialog.acquire(blocking=False):
        # Only the server uses this exception; the GUI runs as a standalone script.
        from ..runtime.snapshot import RegistrationConflict
        raise RegistrationConflict("已有一个文件选择窗口打开。")
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), kind, initial],
                                capture_output=True, text=True, encoding="utf-8",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise ValueError("服务所在桌面无法打开系统文件选择器，可手动填写路径。")
        return {"path": json.loads(result.stdout)}
    finally:
        _dialog.release()


if __name__ == "__main__":
    import tkinter as tk
    from tkinter import filedialog

    _, kind, initial = sys.argv
    directory = Path(initial).expanduser() if initial else Path.home()
    directory = next((p for p in (directory, *directory.parents) if p.is_dir()), Path.home())
    window = tk.Tk()
    window.withdraw()
    window.attributes("-topmost", True)
    options = {"parent": window, "initialdir": str(directory)}
    try:
        if kind == "executable":
            selected = filedialog.askopenfilename(title="选择 effect-codegen", **options)
        else:
            selected = filedialog.askdirectory(title="选择目录", mustexist=True, **options)
        print(json.dumps(str(Path(selected)) if selected else None))
    finally:
        window.destroy()
