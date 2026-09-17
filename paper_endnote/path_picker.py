from __future__ import annotations

import threading
from pathlib import Path


class PathPickerError(RuntimeError):
    pass


_PICKER_LOCK = threading.Lock()


def pick_endnote_library(initial_path: str = "") -> str:
    """Open one native file chooser and return a canonical EndNote library path.

    The web UI cannot obtain a real local path from ``<input type=file>`` and an
    uploaded ``.enl`` would be useless without its sibling ``.Data`` directory.
    This helper therefore runs a local, serialized desktop chooser.  The path is
    validated again by the operation that consumes it.
    """

    with _PICKER_LOCK:
        root = None
        try:
            import tkinter as tk
            from tkinter import filedialog

            initial = Path(str(initial_path or "").strip().strip('"')).expanduser()
            initial_dir = initial.parent if initial.suffix else initial
            if not initial_dir.is_dir():
                initial_dir = Path.home()

            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except tk.TclError:
                pass
            root.update_idletasks()
            selected = filedialog.askopenfilename(
                parent=root,
                title="选择 EndNote 库",
                initialdir=str(initial_dir),
                initialfile=initial.name if initial.suffix.casefold() == ".enl" else "",
                filetypes=[("EndNote library", "*.enl"), ("All files", "*.*")],
            )
        except Exception as exc:
            raise PathPickerError(
                "无法打开本机文件选择器；请直接粘贴 EndNote .enl 的绝对路径"
            ) from exc
        finally:
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass

    if not selected:
        return ""
    path = Path(selected).expanduser()
    if path.suffix.casefold() != ".enl":
        raise PathPickerError("请选择 .enl 格式的 EndNote 库")
    if not path.is_file():
        raise PathPickerError(f"EndNote 库不存在：{path}")
    try:
        return str(path.resolve(strict=True))
    except OSError as exc:
        raise PathPickerError(f"无法读取 EndNote 库：{path}") from exc
