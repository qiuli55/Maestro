"""Maestro 桌面启动器入口（PyInstaller 兼容包装）。

PyInstaller 把 launcher 当 __main__，相对导入 `from . import X` 失败。
这里把 launcher 重写为薄包装，真实业务逻辑放在 maestro.runtime + 调 maestro.cli。
"""
from __future__ import annotations

import argparse
import ctypes
import inspect
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

# 绝对导入：pathex 把 src/ 加进去了；源码运行时 sys.path 也含 src/
from maestro import runtime
from maestro.runtime import user_data_dir, project_root, src_dir


def _port_free(port: int) -> bool:
    """端口当前是否可绑定（复用 port.txt 前验证，避免启动后才发现被占）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def _find_free_port(start: int = 8787, end: int = 8797) -> int:
    """返回 [start, end] 内第一个空闲端口；全占则 raise。

    注意：bind-then-close 存在理论 TOCTOU 窗口（探测后、使用前被抢占），
    概率极低；真发生时 _wait_health 30s 超时会暴露问题而非静默。
    """
    for p in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError(f"无可用端口 ({start}..{end} 全被占用)")


def _wait_health(url: str, timeout: float = 30.0) -> None:
    """阻塞直到 /api/healthz 200 或超时。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 禁代理
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            opener.open(url, timeout=1)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.25)
    raise RuntimeError(f"后端 30s 内未就绪（最后一次错: {last_err}）")


def _read_port_file() -> int | None:
    """上次崩溃/退出时写入的端口，下次启动优先复用（用户多次重启免端口漂移）。"""
    p = user_data_dir() / "port.txt"
    if p.exists():
        try:
            return int(p.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
    return None


def _write_port_file(port: int) -> None:
    p = user_data_dir() / "port.txt"
    p.write_text(str(port), encoding="utf-8")


# frozen 模式下打包 exe 的 sys.executable 是自身（不是 Python 解释器），
# 无法 `-m maestro.server`。改成 spawn 系统 Python 解释器跑 server 包；
# 通过 MAESTRO_HOME（项目根）让 server 找到 configs/web/wallpaper。
# 源码模式下后端解释器可用 MAESTRO_PYTHON 环境变量显式指定（默认 sys.executable）


def _spawn_server(port: int) -> subprocess.Popen:
    """启动后端子进程。

    frozen（PyInstaller）：spawn 自身 exe + --server-mode——依赖全在 _MEIPASS，
    系统 Python 没有 dotenv/fastapi 等第三方包，绝不能用系统 Python 跑源码。
    源码运行：spawn 当前解释器 -m maestro.server（PYTHONPATH=src）；
    特殊环境可用 MAESTRO_PYTHON 指定另一个装好依赖的解释器。
    """
    import shutil

    _log = open(user_data_dir() / f"maestro-server-{os.getpid()}.log", "w")
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--server-mode", "--port", str(port)]
        env = dict(os.environ)
        env["MAESTRO_PORT"] = str(port)
        env["MAESTRO_HOST"] = "127.0.0.1"
        env["MAESTRO_HOME"] = env.get("MAESTRO_HOME") or str(runtime.project_root())
        return subprocess.Popen(cmd, env=env, stdout=_log, stderr=subprocess.STDOUT)
    # 源码运行
    py = (os.environ.get("MAESTRO_PYTHON", "").strip()
          or sys.executable
          or shutil.which("python")
          or shutil.which("python3"))
    env = dict(os.environ)
    env["MAESTRO_PORT"] = str(port)
    env["MAESTRO_HOST"] = "127.0.0.1"
    env["MAESTRO_HOME"] = env.get("MAESTRO_HOME") or str(runtime.project_root())
    pp = str(src_dir())
    env["PYTHONPATH"] = pp + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [py, "-u", "-m", "maestro.server"],
        env=env,
        # cwd 固定用 launcher 所在的真实目录（不随 project_root 补丁/配置漂移）
        cwd=str(Path(__file__).resolve().parent),
        stdout=_log,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if sys.platform == "win32" else 0,
    )


def _watchdog(proc: subprocess.Popen, on_die) -> None:
    """子进程看门狗：死了就调 on_die（通常用于关主窗口退到后台）。"""
    while True:
        if proc.poll() is not None:
            on_die(proc.returncode)
            return
        time.sleep(1.0)


def _serve_only(args) -> int:
    """--no-gui 模式：仅启后端不打开主窗口（CI/服务端调试用）。"""
    print(f"[Maestro] 后端就绪：http://127.0.0.1:{args.port}")
    try:
        proc.wait() if (proc := _spawn_server(args.port)) else None  # type: ignore
        # 注：上面写法太花哨，实际就是直接 wait
    except KeyboardInterrupt:
        pass
    return 0


def _serve_only_real(args) -> int:
    """--no-gui 模式：仅启后端。"""
    port = args.port
    proc = _spawn_server(port)
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_health(f"{base_url}/api/healthz", timeout=30)
    except Exception:
        proc.terminate()
        proc.wait(timeout=5)
        raise
    print(f"[Maestro] 后端就绪：{base_url}")
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
    return 0


def _gui_mode(args) -> int:
    """完整桌面模式：后端 + 壁纸层 + PyWebView 主窗口。"""
    port = args.port
    proc = _spawn_server(port)
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_health(f"{base_url}/api/healthz", timeout=30)
    except Exception:
        proc.terminate()
        proc.wait(timeout=5)
        raise

    die_event = threading.Event()

    def _on_die(_rc):
        die_event.set()

    threading.Thread(target=_watchdog, args=(proc, _on_die), daemon=True).start()

    try:
        import webview  # type: ignore[import-not-found]
    except ImportError as e:
        print(f"[Maestro] PyWebView 未安装：{e}；退到 webbrowser.open 模式", file=sys.stderr)
        import webbrowser
        webbrowser.open(base_url)
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
        return 0

    _icon = str(project_root() / "packaging" / "maestro.ico")
    if not Path(_icon).exists():
        _icon = str(Path(__file__).resolve().parents[2] / "web" / "favicon.ico")

    # pywebview 5.4~5.x 支持 create_window(icon=)，6.x 已移除——按签名自适应
    win_kwargs = {}
    if Path(_icon).exists() and "icon" in inspect.signature(webview.create_window).parameters:
        win_kwargs["icon"] = _icon

    main_window = webview.create_window(
        title="Maestro",
        url=base_url + "/",
        width=1400,
        height=900,
        min_size=(900, 600),
        resizable=True,
        **win_kwargs,
    )

    wallpaper_window = None
    if sys.platform == "win32":
        # pywebview 4.x 用 visible=，6.x 改名 hidden=
        _hidden_kw = ("hidden" if "hidden" in inspect.signature(webview.create_window).parameters
                      else "visible")
        wallpaper_window = webview.create_window(
            title="Maestro Wallpaper",
            url=base_url + "/?wallpaper=1",
            width=100, height=100,
            resizable=False,
            frameless=True,  # 壁纸层不能带标题栏（挂 WorkerW 后会露出关闭/最大化按钮）
            on_top=True,
            **{_hidden_kw: False},
        )

    def _install_wallpaper():
        """GUI 起来后把壁纸窗口挂到 WorkerW（在 webview.start 的后台线程执行）。"""
        from maestro import desktop as _desktop
        for _ in range(50):  # 最多等 10s，native 句柄就绪即挂
            try:
                native = getattr(wallpaper_window, "native", None)
                if native is not None:
                    hwnd = int(native.Handle.ToInt32())
                    if hwnd and _desktop.install_wallpaper_layer(hwnd):
                        _start_mouse_forward(_desktop)
                        _start_rect_watchdog(_desktop)
                        return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.2)

    def _start_rect_watchdog(_desktop):
        """壁纸窗口偶发漂移自愈：周期校验实测矩形，漂了就拉回原位。

        漂移根因未查明（尺寸不变、位置移出屏幕左上，启动数分钟后偶发），
        先用 3s 轮询兜底保证可用性。
        """
        def _run():
            while True:
                time.sleep(3)
                try:
                    _desktop.ensure_wallpaper_rect()
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=_run, name="rect-watchdog", daemon=True).start()

    def _start_mouse_forward(_desktop):
        """全局鼠标钩子 → 壁纸层全交互转发（与网页一致的使用体验）。

        接管决策（钩子线程内同步判定）：
        - 右键永远放行（桌面右键菜单是用户唯一的桌面操作逃生口）
        - 问页面 window.__maestroHit：命中交互元素（dock/输入框/面板/按钮/
          戳脸区等）→ 吞掉转发给壁纸层
        - 兜底：空桌面（不在图标/任务栏/其它窗口上）也吞掉转发（壁纸即桌面
          表面，与 Wallpaper Engine 行为一致）
        - 其余（桌面整理图标、任务栏、普通应用窗口）照常放行，零影响

        慢操作（真正的 evaluate_js 注入）入队由 worker 线程异步做，钩子
        回调只留一次快速的 hit 判定查询。坐标换算：注入 "(物理偏移)*
        innerWidth/vw" 表达式由页面自归一化，任何 DPI 缩放都对。
        """
        from maestro import mouse_hook
        vx, vy, vw, vh = _desktop._state["rect"]
        q: queue.Queue = queue.Queue()
        last = [0.0]
        pressed = [False]  # 左键按住中（转发 move 带 buttons=1，支持页面内拖拽）

        def cx(x):
            return f"({x}-{vx})*innerWidth/{vw}"

        def cy(y):
            return f"({y}-{vy})*innerHeight/{vh}"

        _HIT_HELPER = """window.__maestroHit=function(x,y,vx,vy,vw,vh){
            try{
                var px=(x-vx)*innerWidth/vw, py=(y-vy)*innerHeight/vh;
                var el=document.elementFromPoint(px,py);
                if(!el)return 'free';
                var hit=el.closest('#dock,#maestro,#history-panel,#wf-builder,#reply,'
                    +'#bubble,#hitface,#agent-selector,#conv-list,button,a,input,textarea,'
                    +'select,label,[role="button"],[contenteditable],.win-mini,.panel,.popup,.modal,.menu');
                return hit?'mine':'free';
            }catch(e){return 'err';}}"""

        def _hit_query(x, y):
            """问页面这个点是否属于壁纸 UI。异常按'非交互'处理。"""
            expr = f"window.__maestroHit?window.__maestroHit({x},{y},{vx},{vy},{vw},{vh}):'nohelper'"
            try:
                r = wallpaper_window.evaluate_js(expr)
            except Exception:  # noqa: BLE001
                return "err"
            if r == "nohelper":  # 页面刷新过：重注入一次再问
                try:
                    wallpaper_window.evaluate_js(_HIT_HELPER)
                    r = wallpaper_window.evaluate_js(expr)
                except Exception:  # noqa: BLE001
                    return "err"
            return r if isinstance(r, str) else "err"

        def on_move(x, y):
            now = time.time()
            if now - last[0] < 0.03 and not pressed[0]:  # ~30Hz；拖拽时不节流
                return
            last[0] = now
            try:
                wallpaper_window.evaluate_js(
                    f"window.dispatchEvent(new MouseEvent('mousemove',"
                    f"{{clientX:{cx(x)},clientY:{cy(y)},buttons:{1 if pressed[0] else 0}}}))")
            except Exception:  # noqa: BLE001
                pass

        _BTN = {"ldown": ("mousedown", 0), "lup": ("mouseup", 0),
                "rdown": ("mousedown", 2), "rup": ("mouseup", 2),
                "mdown": ("mousedown", 1), "mup": ("mouseup", 1)}

        def _dispatch(kind, x, y, delta):
            """worker 线程：把事件注入页面。返回 JS 判定结果字符串。"""
            px, py = cx(x), cy(y)
            if kind == "wheel":
                return wallpaper_window.evaluate_js(f"""(function(){{
                    var el=document.elementFromPoint({px},{py});
                    document.title='MSW';
                    if(!el)return 'none';
                    var step={delta if delta else 0}>0?-56:56,n=el;
                    while(n&&n!==document.documentElement){{
                        var cs=getComputedStyle(n);
                        if((cs.overflowY==='auto'||cs.overflowY==='scroll')&&n.scrollHeight>n.clientHeight){{
                            n.scrollTop+=step;return 'scroll';}}
                        n=n.parentElement;}}
                    return 'noscroll';}})()""")
            mtype, btn = _BTN[kind]
            return wallpaper_window.evaluate_js(f"""(function(){{
                var px={px},py={py};
                var el=document.elementFromPoint(px,py);
                document.title='MS '+(el?(el.id||el.className||el.tagName):'null');
                if(!el)return 'none';
                var o={{clientX:px,clientY:py,button:{btn},buttons:{btn if mtype=='mousedown' else 0},bubbles:true,cancelable:true,view:window}};
                el.dispatchEvent(new MouseEvent('{mtype}',o));
                if('{mtype}'==='mouseup'){{
                    el.dispatchEvent(new MouseEvent('click',o));
                    var ed=el.closest?el.closest('input,textarea,select,[contenteditable]'):null;
                    if(ed){{ed.focus();document.title='MSF '+(ed.id||ed.tagName);return 'editable';}}
                }}
                return 'ok';}})()""")

        def _find_webview2_ctl(form_ctl):
            """递归找 WinForms 宿主里的 WebView2 控件（pythonnet 对象）。"""
            try:
                stack = [form_ctl]
                while stack:
                    c = stack.pop()
                    try:
                        if c.GetType().Name == "WebView2":
                            return c
                        for child in c.Controls:
                            stack.append(child)
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
            return None

        def _focus_wallpaper():
            """把键盘焦点转给壁纸层的 WebView2（IME 跟焦点走，中文可输入）。

            两层缺一不可：
            1. OS 前台：壁纸窗所属线程必须在前台，物理键盘才路由进来
               （SetForegroundWindow + AttachThreadInput 抢前台配方）。
            2. Chromium 内部焦点：OS SetFocus 不等于 WebView2 认为持有焦点，
               控制器不认焦点时直接丢弃键盘输入。走官方路径：WinForms 宿主里
               的 WebView2 控件 .Focus()（内部 MoveFocus(PROGRAMMATIC)），
               经 BeginInvoke 切到 UI 线程执行。
            旧的 set_focus_without_activation 保留为 .NET 路径失败时的兜底。
            """
            try:
                native = wallpaper_window.native
                hwnd = int(native.Handle.ToInt32())
                my_tid = ctypes.windll.kernel32.GetCurrentThreadId()
                fg = ctypes.windll.user32.GetForegroundWindow()
                if int(fg or 0) != hwnd:
                    fg_tid = ctypes.windll.user32.GetWindowThreadProcessId(fg, None)
                    ctypes.windll.user32.AttachThreadInput(my_tid, fg_tid, True)
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                    ctypes.windll.user32.AttachThreadInput(my_tid, fg_tid, False)
                ctl = _find_webview2_ctl(native)
                if ctl is not None:
                    from System import Action
                    native.BeginInvoke(Action(ctl.Focus))
                    return True
            except Exception:  # noqa: BLE001
                pass
            # 兜底：纯 Win32 焦点转移
            try:
                hwnd = int(wallpaper_window.native.Handle.ToInt32())
                for target in (_desktop.find_webview_input_hwnds(hwnd) or [hwnd]):
                    if _desktop.set_focus_without_activation(target):
                        return True
            except Exception:  # noqa: BLE001
                pass
            return False

        def _worker():
            while True:
                kind, x, y, delta = q.get()
                try:
                    r = _dispatch(kind, x, y, delta)
                    if kind == "lup" and r == "editable":  # editable 在 mouseup 时判定
                        _focus_wallpaper()
                except Exception:  # noqa: BLE001
                    pass

        def on_interact(kind, x, y, delta):
            """钩子线程：快速决策是否吞掉。右键永远放行。"""
            if kind in ("rdown", "rup"):
                return False
            mine = _hit_query(x, y) == "mine"
            if not mine and not _desktop.point_over_empty_desktop(x, y):
                return False
            if kind == "ldown":
                pressed[0] = True
            elif kind == "lup":
                pressed[0] = False
            q.put((kind, x, y, delta))
            return True

        threading.Thread(target=_worker, daemon=True).start()
        mouse_hook.start(on_move, on_interact)

    try:
        webview.start(_install_wallpaper if wallpaper_window else None)
    except Exception as e:
        print(f"[Maestro] PyWebView 启动失败：{e}", file=sys.stderr)
        import webbrowser
        webbrowser.open(base_url)
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
        return 1
    finally:
        if sys.platform == "win32":
            try:
                from maestro import desktop as _desktop
                _desktop.uninstall_wallpaper_layer()
            except Exception:  # noqa: BLE001
                pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    return 0


def run() -> int:
    """启动器主入口（PyInstaller / 源码双兼容）。"""
    parser = argparse.ArgumentParser(prog="maestro-launcher")
    parser.add_argument("--no-gui", action="store_true",
                        help="仅启服务不开窗口（CI/服务端调试用）")
    parser.add_argument("--port", type=int, default=None,
                        help="强制指定端口（默认从 user_data/port.txt 读，被占就 8788..）")
    parser.add_argument("--server-mode", action="store_true",
                        help=argparse.SUPPRESS)  # 内部：frozen 子进程跑 uvicorn
    args = parser.parse_args()

    # frozen 子进程模式：同进程 import app + uvicorn.run（依赖全在 _MEIPASS）
    if getattr(args, "server_mode", False):
        import uvicorn
        uvicorn.run("maestro.server:app",
                    host=os.environ.get("MAESTRO_HOST", "127.0.0.1"),
                    port=args.port, reload=False)
        return 0

    # 端口选择：CLI 强定 > 持久化端口 > 默认探测
    if args.port is None:
        saved = _read_port_file()
        if saved and _port_free(saved):
            args.port = saved
        else:
            args.port = _find_free_port()
    _write_port_file(args.port)

    if args.no_gui:
        return _serve_only_real(args)
    return _gui_mode(args)


if __name__ == "__main__":
    sys.exit(run())
