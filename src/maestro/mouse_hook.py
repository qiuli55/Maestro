"""全局低级鼠标钩子：把鼠标移动/点击转发给壁纸层（只旁路转发，绝不拦截）。

壁纸窗口挂在图标层下方，Windows 永远把鼠标事件先给图标层——壁纸页面的
mousemove/click 事件永远收不到。本模块用 WH_MOUSE_LL 全局钩子捕获屏幕坐标，
由调用方换算成壁纸页面的 client 坐标注入 JS，让视差/戳脸彩蛋在壁纸层复活。
钩子回调一律 CallNextHookEx 放行，桌面行为零影响。
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32")
kernel32 = ctypes.WinDLL("kernel32")

WH_MOUSE_LL = 14
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
LRESULT = ctypes.c_longlong


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

SetWindowsHookExW = user32.SetWindowsHookExW
SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
SetWindowsHookExW.restype = ctypes.c_void_p
CallNextHookEx = user32.CallNextHookEx
CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
CallNextHookEx.restype = LRESULT
UnhookWindowsHookEx = user32.UnhookWindowsHookEx
UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]


def start(on_move, on_click) -> None:
    """启动后台钩子线程（daemon，随进程退出自动清理）。"""
    threading.Thread(target=_run, args=(on_move, on_click), daemon=True).start()


def _run(on_move, on_click) -> None:
    keep_proc_ref: list = []  # 防 GC

    def proc(code, wparam, lparam):
        if code == 0:
            info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            x, y = int(info.pt.x), int(info.pt.y)
            if wparam == WM_MOUSEMOVE:
                try:
                    on_move(x, y)
                except Exception:  # noqa: BLE001 回调异常绝不影响鼠标
                    pass
            elif wparam == WM_LBUTTONDOWN:
                try:
                    on_click(x, y)
                except Exception:  # noqa: BLE001
                    pass
        return CallNextHookEx(None, code, wparam, lparam)

    proc_ref = HOOKPROC(proc)
    keep_proc_ref.append(proc_ref)
    # WH_MOUSE_LL 的 hMod 必须为 NULL（64 位下传模块句柄会报 ERROR_MOD_NOT_FOUND）
    hook = SetWindowsHookExW(WH_MOUSE_LL, proc_ref, None, 0)
    # WH_MOUSE_LL 要求安装线程跑消息泵
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    if hook:
        UnhookWindowsHookEx(hook)
