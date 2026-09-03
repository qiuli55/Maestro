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

    def point_over_empty_desktop(*_args, **_kwargs) -> bool:
        """非 Windows 平台未启用壁纸交互（stub）。"""
        return False

    def find_webview_input_hwnds(*_args, **_kwargs) -> list:
        """非 Windows 平台 stub。"""
        return []

    def set_focus_without_activation(*_args, **_kwargs) -> bool:
        """非 Windows 平台 stub。"""
        return False

    def ensure_wallpaper_rect() -> bool:
        """非 Windows 平台 stub。"""
        return False
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
        # 任务栏让位：壁纸窗口不铺到任务栏底下，页面底部的 dock/输入框才露得出来
        tb = _FindWindowW("Shell_TrayWnd", None)
        if tb:
            rect = wintypes.RECT()
            if user32.GetWindowRect(tb, ctypes.byref(rect)):
                if rect.top >= vy + vh // 2:        # 底部任务栏（常规）
                    vh = rect.top - vy
                elif rect.bottom <= vy + vh // 2:   # 顶部任务栏
                    vy = rect.bottom
                    vh = (vy + vh) - rect.bottom
                # 左右任务栏罕见，不做处理（仍全屏铺）
        SetWindowPos(hwnd, HWND_BOTTOM, vx, vy, vw, vh,
                      SWP_NOACTIVATE | SWP_FRAMECHANGED)
        ShowWindow(hwnd, SW_SHOW)
        _state["hwnd"] = hwnd
        _state["parent"] = parent
        _state["rect"] = (vx, vy, vw, vh)  # 供鼠标坐标换算（screen → client），含任务栏让位
        return True

    def uninstall_wallpaper_layer() -> None:
        """服务退出时还原桌面（关掉挂入的窗口）。"""
        hwnd = _state.get("hwnd")
        if hwnd and IsWindow(hwnd):
            PostMessageW(hwnd, WM_CLOSE, 0, 0)
        _state["hwnd"] = None
        _state["parent"] = None

    _GetWindowRect = user32.GetWindowRect
    _GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
    _GetWindowRect.restype = wintypes.BOOL

    def ensure_wallpaper_rect() -> bool:
        """壁纸窗口位置漂移自愈：实测矩形与安装预期不符就拉回原位。

        观测到偶发漂移（尺寸不变、位置移出屏幕左上，原因未查明），
        这里按 _state["rect"] 定期校验，漂了就 SetWindowPos 拉回。
        返回 True 表示发生了漂移并已拉回；False 表示位置正常/未挂载。
        """
        hwnd = _state.get("hwnd")
        expected = _state.get("rect")
        if not hwnd or not expected or not IsWindow(hwnd):
            return False
        rect = wintypes.RECT()
        if not _GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        vx, vy, vw, vh = expected
        cur_w, cur_h = rect.right - rect.left, rect.bottom - rect.top
        if (rect.left, rect.top, cur_w, cur_h) == (vx, vy, vw, vh):
            return False
        SetWindowPos(hwnd, HWND_BOTTOM, vx, vy, vw, vh,
                      SWP_NOACTIVATE)
        return True

    # ====== 壁纸全交互：空桌面判定 + 键盘焦点转移 ======
    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class LVHITTESTINFO(ctypes.Structure):
        _fields_ = [("pt", POINT), ("flags", wintypes.UINT), ("iItem", ctypes.c_int)]

    _WindowFromPoint = user32.WindowFromPoint
    _WindowFromPoint.argtypes = [POINT]
    _WindowFromPoint.restype = HWND

    _GetAncestor = user32.GetAncestor
    _GetAncestor.argtypes = [HWND, ctypes.c_uint]
    _GetAncestor.restype = HWND

    _GetWindowThreadProcessId = user32.GetWindowThreadProcessId
    _GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
    _GetWindowThreadProcessId.restype = wintypes.DWORD

    _SendMessageW = user32.SendMessageW
    _SendMessageW.argtypes = [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _SendMessageW.restype = ctypes.c_long

    _kernel32 = ctypes.WinDLL("kernel32")
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.VirtualAllocEx.restype = ctypes.c_void_p
    _kernel32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                         ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    _kernel32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                        ctypes.c_size_t, wintypes.DWORD]
    _kernel32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    _kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

    LVM_HITTEST = 0x1012
    GA_PARENT = 1
    MEM_COMMIT = 0x1000
    MEM_RELEASE = 0x8000
    PAGE_READWRITE = 0x04
    PROCESS_VM_OPERATION = 0x0008
    PROCESS_VM_READ = 0x0010
    PROCESS_VM_WRITE = 0x0020

    def _find_desktop_defview() -> HWND | None:
        """找桌面图标层窗口（SHELLDLL_DefView），可能挂在 Progman 或某个 WorkerW 下。"""
        result: list = []
        CMPFUNC = ctypes.CFUNCTYPE(ctypes.c_int, HWND, wintypes.LPARAM)

        def enum_top(hwnd, _lp):
            if _class_name(hwnd) in ("Progman", "WorkerW") and _has_defview_child(hwnd):
                def enum_child(ch, _lp2):
                    if _class_name(ch) == "SHELLDLL_DefView":
                        result.append(ch)
                        return 0
                    return 1
                user32.EnumChildWindows(hwnd, CMPFUNC(enum_child), 0)
                return False  # 找到即停
            return True

        user32.EnumWindows(CMPFUNC(enum_top), 0)
        return result[0] if result else None

    def point_over_empty_desktop(x: int, y: int) -> bool:
        """物理坐标 (x,y) 是否落在"空桌面"（不在图标上、不在任务栏/其它窗口上）。

        True = 可安全吞掉鼠标事件转发给壁纸层；False/异常 = 放行（保守默认，
        宁可少交互也不能吞掉桌面图标的点击）。
        用跨进程 LVM_HITTEST 判点是否在图标项上：列表控件在别的进程
        （explorer），LVHITTESTINFO 指针必须 VirtualAllocEx 到对方空间。
        """
        try:
            pt = POINT(x, y)
            hwnd = _WindowFromPoint(pt)
            if not hwnd:
                return False
            if _class_name(hwnd) != "SysListView32":
                return False  # 任务栏 / 其它应用窗口 → 放行
            defview = _find_desktop_defview()
            if not defview or _GetAncestor(hwnd, GA_PARENT) != defview:
                return False  # 非桌面图标列表（别应用的列表控件）→ 放行
            pid = wintypes.DWORD(0)
            _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            hproc = _kernel32.OpenProcess(
                PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE, False, pid.value)
            if not hproc:
                return False
            try:
                info = LVHITTESTINFO(pt=pt)
                size = ctypes.sizeof(info)
                mem = _kernel32.VirtualAllocEx(hproc, None, size, MEM_COMMIT, PAGE_READWRITE)
                if not mem:
                    return False
                try:
                    n = ctypes.c_size_t(0)
                    _kernel32.WriteProcessMemory(hproc, mem, ctypes.byref(info), size, ctypes.byref(n))
                    _SendMessageW(hwnd, LVM_HITTEST, 0, mem)
                    _kernel32.ReadProcessMemory(hproc, mem, ctypes.byref(info), size, ctypes.byref(n))
                    return info.iItem == -1  # -1 = 空白区域（不在任何图标上）
                finally:
                    _kernel32.VirtualFreeEx(hproc, mem, 0, MEM_RELEASE)
            finally:
                _kernel32.CloseHandle(hproc)
        except Exception:  # noqa: BLE001 — 判定失败按放行处理
            return False

    def find_webview_input_hwnds(top_hwnd: int) -> list[int]:
        """找 WebView2 可能接收键盘输入的子窗口，按优先级返回。

        Chromium 的 Chrome_RenderWidgetHostHWND 是 OS 键盘输入的真正落点，
        优先；其次 Chrome_WidgetWin_*（深层的在前）。
        """
        widgets: list[int] = []
        renders: list[int] = []
        CMPFUNC = ctypes.CFUNCTYPE(ctypes.c_int, HWND, wintypes.LPARAM)

        def enum_child(ch, _lp):
            cls = _class_name(ch)
            if cls == "Chrome_RenderWidgetHostHWND":
                renders.append(int(ch))
            elif cls.startswith("Chrome_WidgetWin"):
                widgets.append(int(ch))
            return 1

        user32.EnumChildWindows(HWND(top_hwnd), CMPFUNC(enum_child), 0)
        return renders + widgets[::-1]

    def set_focus_without_activation(target_hwnd: int) -> bool:
        """把键盘焦点给（可能是后台的）target，不抢前台激活。

        AttachThreadInput 经典手法：把当前线程、壁纸窗口属主线程、前台线程
        三方输入队列临时合并后 SetFocus。IME 跟随焦点，中文输入可用。
        """
        try:
            _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            my_tid = _kernel32.GetCurrentThreadId()
            pid = wintypes.DWORD(0)
            tgt_tid = _GetWindowThreadProcessId(HWND(target_hwnd), ctypes.byref(pid))
            fg = user32.GetForegroundWindow()
            fg_tid = _GetWindowThreadProcessId(fg, ctypes.byref(pid)) if fg else 0
            attached = []
            if tgt_tid and tgt_tid != my_tid:
                if user32.AttachThreadInput(my_tid, tgt_tid, True):
                    attached.append((my_tid, tgt_tid))
            if fg_tid and fg_tid not in (my_tid, tgt_tid):
                if user32.AttachThreadInput(my_tid, fg_tid, True):
                    attached.append((my_tid, fg_tid))
            try:
                return bool(user32.SetFocus(HWND(target_hwnd)))
            finally:
                for a, b in attached:
                    user32.AttachThreadInput(a, b, False)
        except Exception:  # noqa: BLE001
            return False
