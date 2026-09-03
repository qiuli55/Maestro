"""全局低级鼠标钩子：把鼠标事件转发给壁纸层（可按需吞掉，实现壁纸全交互）。

壁纸窗口挂在图标层下方且 WS_EX_TRANSPARENT，Windows 永远把鼠标事件先给图标层
——壁纸页面的鼠标事件收不到。本模块用 WH_MOUSE_LL 全局钩子捕获事件：

- mousemove：旁路转发（绝不拦截，桌面零影响）
- 按键/滚轮：交给 on_interact 回调决策——返回 True 则吞掉（事件不到系统，
  由调用方转发给壁纸层），False 放行（桌面图标/任务栏/其它窗口照常响应）

钩子回调内只做快速决策；慢操作（evaluate_js 等）由调用方自行排队异步做，
LL 钩子超时会被 Windows 静默摘除，务必快进快出。
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32")
kernel32 = ctypes.WinDLL("kernel32")

WH_MOUSE_LL = 14
WM_MOUSEMOVE = 0x0200
# 按键/滚轮事件 → 语义名（on_interact 的 kind 参数）
BUTTON_EVENTS = {
    0x0201: "ldown", 0x0202: "lup",
    0x0204: "rdown", 0x0205: "rup",
    0x0207: "mdown", 0x0208: "mup",
    0x020A: "wheel", 0x020E: "hwheel",
}
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


def start(on_move, on_interact) -> None:
    """启动后台钩子线程（daemon，随进程退出自动清理）。

    on_move(x, y)：mousemove 旁路转发，永不拦截。
    on_interact(kind, x, y, delta) -> bool：按键/滚轮决策，True=吞掉。
    delta 仅 wheel/hwheel 有值（原始 wparam 的高 16 位，有符号短整型）。
    """
    threading.Thread(target=_run, args=(on_move, on_interact), daemon=True).start()


def _wheel_delta(mouseData: int) -> int:
    """WM_MOUSEWHEEL 的 delta 在 mouseData 高 16 位（有符号）。"""
    return ctypes.c_short((mouseData >> 16) & 0xFFFF).value


def _run(on_move, on_interact) -> None:
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
            elif wparam in BUTTON_EVENTS:
                try:
                    swallow = bool(on_interact(
                        BUTTON_EVENTS[wparam], x, y, _wheel_delta(int(info.mouseData))))
                except Exception:  # noqa: BLE001 决策异常按放行处理（宁可不吞）
                    swallow = False
                if swallow:
                    return 1  # 吞掉：事件不再传给系统，由调用方转发壁纸层
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
