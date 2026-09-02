"""Windows 桌面壁纸层：把 HTML 窗口挂到 Program Manager WorkerW 层级。

WorkerW 是图标/桌面所在的虚拟窗口。挂到它的子窗口：
  - 永远在桌面图标下层（点击桌面图标照常响应）
  - 不抢焦点（不会成为前台窗口）
  - 不出现在任务栏/Alt+Tab
  - 配合 WS_EX_LAYERED + WS_EX_TRANSPARENT 让点击穿透到桌面

非 Windows 平台：实现空 stub，桌面壁纸集成跳过；launcher 退到只启主窗口。
"""
from __future__ import annotations

import sys

if sys.platform != "win32":
    # 非 Windows：纯 stub，零 ctypes 依赖——导入 desktop 模块不会拉平台特定库
    def install_wallpaper_layer(*_args, **_kwargs) -> bool:
        """非 Windows 平台未启用壁纸层（stub）。launcher 走单窗口模式。"""
        return False

    def uninstall_wallpaper_layer() -> None:
        """非 Windows 平台空操作（stub）。"""
        return None
else:  # ===== Windows 实现 =====
    import ctypes
    import ctypes.wintypes as wintypes  # 统一 wintypes 引用，避免 c_wparam 等别名差异

    HWND = wintypes.HWND
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    WM_CLOSE = 0x0010
    GW_HWNDNEXT = 2
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    SWP_FRAMECHANGED = 0x0020
    HWND_BOTTOM = 1
    SW_HIDE = 0
    SW_SHOW = 5

    WS_CHILD = 0x40000000
    WS_VISIBLE = 0x10000000
    WS_CLIPSIBLINGS = 0x04000000
    WS_CLIPCHILDREN = 0x02000000
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOOLWINDOW = 0x00000080  # 不在 Alt+Tab 出现

    LWA_ALPHA = 0x00000002

    SetWindowPos = user32.SetWindowPos
    SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    SetWindowPos.restype = wintypes.BOOL

    SetParent = user32.SetParent
    SetParent.argtypes = [HWND, HWND]
    SetParent.restype = HWND

    SetWindowLongW = user32.SetWindowLongW
    SetWindowLongW.argtypes = [HWND, ctypes.c_int, ctypes.c_long]
    SetWindowLongW.restype = ctypes.c_long

    GetWindowLongW = user32.GetWindowLongW
    GetWindowLongW.argtypes = [HWND, ctypes.c_int]
    GetWindowLongW.restype = ctypes.c_long

    SetLayeredWindowAttributes = user32.SetLayeredWindowAttributes
    SetLayeredWindowAttributes.argtypes = [HWND, wintypes.COLORREF, ctypes.c_byte, wintypes.DWORD]
    SetLayeredWindowAttributes.restype = wintypes.BOOL

    GetDesktopWindow = user32.GetDesktopWindow
    GetDesktopWindow.argtypes = []
    GetDesktopWindow.restype = HWND

    GetWindow = user32.GetWindow
    GetWindow.argtypes = [HWND, ctypes.c_uint]
    GetWindow.restype = HWND

    ShowWindow = user32.ShowWindow
    ShowWindow.argtypes = [HWND, ctypes.c_int]
    ShowWindow.restype = wintypes.BOOL

    PostMessageW = user32.PostMessageW
    PostMessageW.argtypes = [HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
    PostMessageW.restype = wintypes.BOOL

    IsWindow = user32.IsWindow
    IsWindow.argtypes = [HWND]
    IsWindow.restype = wintypes.BOOL

    GW_OWNER = 4
    GWL_EXSTYLE = -20

    _state: dict = {"hwnd": None, "parent": None}

    _FindWindowW = user32.FindWindowW
    _FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _FindWindowW.restype = HWND
    _SendMessageTimeoutW = user32.SendMessageTimeoutW
    _SendMessageTimeoutW.argtypes = [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.DWORD)]
    _SendMessageTimeoutW.restype = ctypes.c_long

    def _class_name(hwnd: HWND) -> str:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def _spawn_wallpaper_workerw() -> None:
        """给 Progman 发 0x052C，让 Explorer 在图标层后面生成一个空 WorkerW（壁纸挂点）。"""
        progman = _FindWindowW("Progman", None)
        if progman:
            SMTO_ABORTIFHUNG = 0x0002
            _SendMessageTimeoutW(progman, 0x052C, 0, 0, SMTO_ABORTIFHUNG, 1000, None)

    def _has_defview_child(hwnd: HWND) -> bool:
        """该窗口是否把桌面图标层（SHELLDLL_DefView）当孩子。"""
        found = []
        CMPFUNC = ctypes.CFUNCTYPE(ctypes.c_int, HWND, wintypes.LPARAM)
        EnumChildWindows = user32.EnumChildWindows
        EnumChildWindows.argtypes = [HWND, ctypes.c_void_p, wintypes.LPARAM]
        EnumChildWindows.restype = wintypes.BOOL

        def enum_child(ch, _lparam):
            if _class_name(ch) == "SHELLDLL_DefView":
                found.append(True)
                return 0
            return 1

        EnumChildWindows(hwnd, CMPFUNC(enum_child), 0)
        return bool(found)

    def _find_workerw() -> HWND | None:
        """找"图标层正下方"的空 WorkerW 作为壁纸挂点（多壁纸软件共存时最稳）。"""
        _spawn_wallpaper_workerw()
        CMPFUNC = ctypes.CFUNCTYPE(ctypes.c_int, HWND, wintypes.LPARAM)
        icons_workerw = None
        candidates = []

        def enum_top(hwnd, _lparam):
            nonlocal icons_workerw
            if _class_name(hwnd) == "WorkerW":
                if _has_defview_child(hwnd):
                    icons_workerw = hwnd
                else:
                    candidates.append(hwnd)
            return True

        user32.EnumWindows(CMPFUNC(enum_top), 0)
        if icons_workerw:
            # 图标层 Z 序正下方的空 WorkerW = 标准壁纸挂点
            below = GetWindow(icons_workerw, GW_HWNDNEXT)
            if below and _class_name(below) == "WorkerW" and not _has_defview_child(below):
                return below
        return candidates[0] if candidates else None

    def install_wallpaper_layer(window_handle: int) -> bool:
        """把 PyWebView 的窗口句柄挂到 WorkerW 下面，做成壁纸层。

        window_handle: PyWebView 暴露的 HWND（PyWebView 的 window.handle / hwnd 属性）。
        返回 True 表示成功挂入壁纸层；False 表示 WorkerW 未找到或挂载失败。
        """
        if _state.get("hwnd"):
            return True  # 已挂过，幂等
        parent = _find_workerw()
        if not parent:
            return False
        hwnd = HWND(window_handle)
        # 1) 改样式：去掉 WS_EX_TOOLWINDOW 是 PyWebView 默认就有的；叠加 LAYERED+TRANSPARENT
        #    实现鼠标穿透；NOACTIVATE 防抢焦点
        ex = GetWindowLongW(hwnd, GWL_EXSTYLE)
        ex &= ~0x00000080  # 去掉 WS_EX_TOOLWINDOW：不做"工具窗"（避免某些主题遮挡）
        ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
        # 2) 设到完全透明（点穿透），但仍渲染内容
        #    LWA_ALPHA + alpha=255 表示仅启用 layered（不真透明），
        #    WS_EX_TRANSPARENT 负责 hit-test 穿透。
        SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
        # 3) SetParent → 挂到 WorkerW
        SetParent(hwnd, parent)
        # 4) 全屏平铺到虚拟桌面尺寸（用 SM_CX/CY_VIRTUALSCREEN 跨多屏）
        GetSystemMetrics = user32.GetSystemMetrics
        GetSystemMetrics.argtypes = [ctypes.c_int]
        GetSystemMetrics.restype = ctypes.c_int
        SM_CXVIRTUALSCREEN = 78
        SM_CYVIRTUALSCREEN = 79
        SM_XVIRTUALSCREEN = 76
        SM_YVIRTUALSCREEN = 77
        vw = GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = GetSystemMetrics(SM_CYVIRTUALSCREEN)
        vx = GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = GetSystemMetrics(SM_YVIRTUALSCREEN)
        SetWindowPos(hwnd, HWND_BOTTOM, vx, vy, vw, vh,
                      SWP_NOACTIVATE | SWP_FRAMECHANGED)
        ShowWindow(hwnd, SW_SHOW)
        _state["hwnd"] = hwnd
        _state["parent"] = parent
        _state["rect"] = (vx, vy, vw, vh)  # 供鼠标坐标换算（screen → client）
        return True

    def uninstall_wallpaper_layer() -> None:
        """服务退出时还原桌面（关掉挂入的窗口）。"""
        hwnd = _state.get("hwnd")
        if hwnd and IsWindow(hwnd):
            PostMessageW(hwnd, WM_CLOSE, 0, 0)
        _state["hwnd"] = None
        _state["parent"] = None
