"""检查 Maestro 壁纸窗口是否已挂到 WorkerW（只读检查，不改任何窗口）。"""
import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32")
hits = []

ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

def _name(hwnd, is_class=False):
    buf = ctypes.create_unicode_buffer(256)
    (user32.GetClassNameW if is_class else user32.GetWindowTextW)(hwnd, buf, 256)
    return buf.value

@ENUMPROC
def cb(hwnd, _l):
    title = _name(hwnd)
    if "Maestro" in title:
        parent = user32.GetParent(hwnd)
        pcls = _name(parent, is_class=True) if parent else "(无父窗口)"
        hits.append((title, _name(hwnd, is_class=True), pcls, bool(user32.IsWindowVisible(hwnd))))
    return True

user32.EnumWindows(cb, 0)
for title, cls, pcls, vis in hits:
    print(f"TITLE={title!r} CLASS={cls!r} PARENT={pcls!r} VISIBLE={vis}")
if not hits:
    print("NO_MAESTRO_WINDOWS")
