"""Maestro 桌面启动器入口（PyInstaller / 源码双兼容）。

职责：拉起后端子进程 → 等 healthz → 建主窗口 + 壁纸窗口 → 挂 WorkerW
→ 转发鼠标交互 → 退出时清理。PyInstaller 把本文件当 __main__，故全用绝对导入。
"""
from __future__ import annotations

import argparse
import ctypes
import inspect
import logging
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

logger = logging.getLogger(__name__)

# 壁纸层后台线程的统一停机标志（rect 看门狗 / 鼠标转发 worker 都看它）
_WALLPAPER_STOP = threading.Event()


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


def _watch_server(proc: subprocess.Popen) -> None:
    """后端看门狗：子进程退出时记 error 日志（原本 die_event 无人 wait，纯死代码）。"""
    rc = proc.wait()
    logger.error("后端子进程退出 rc=%s", rc)


def _serve_only_real(args) -> int:
    """--no-gui 模式：仅启后端不开窗口（CI/服务端调试用）。"""
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


def _fallback_browser(base_url: str, proc: subprocess.Popen, exit_code: int = 0) -> int:
    """PyWebView 未安装/启动失败：退到系统浏览器打开（最低保证可用）。"""
    print(f"[Maestro] 退到系统浏览器模式：{base_url}", file=sys.stderr)
    import webbrowser
    webbrowser.open(base_url)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
    return exit_code


def _resolve_icon() -> str | None:
    """图标路径：packaging/maestro.ico 优先，退 web/favicon.ico；都没有返回 None。"""
    for p in (project_root() / "packaging" / "maestro.ico",
              Path(__file__).resolve().parents[2] / "web" / "favicon.ico"):
        if p.exists():
            return str(p)
    return None


def _open_main_window(webview, base_url: str):
    """创建主交互窗口（1400x900，可拖可缩）。"""
    icon = _resolve_icon()
    # pywebview 5.4~5.x 支持 create_window(icon=)，6.x 已移除——按签名自适应
    win_kwargs = {}
    if icon and "icon" in inspect.signature(webview.create_window).parameters:
        win_kwargs["icon"] = icon
    return webview.create_window(
        title="Maestro",
        url=base_url + "/",
        width=1400,
        height=900,
        min_size=(900, 600),
        resizable=True,
        **win_kwargs,
    )


def _open_wallpaper_window(webview, base_url: str):
    """创建壁纸窗口（100x100 frameless，挂 WorkerW 后被拉伸铺满桌面）。

    非 Windows 返回 None（壁纸层仅 Windows 实现）。
    """
    if sys.platform != "win32":
        return None
    # pywebview 4.x 用 visible=，6.x 改名 hidden=
    hidden_kw = ("hidden" if "hidden" in inspect.signature(webview.create_window).parameters
                 else "visible")
    return webview.create_window(
        title="Maestro Wallpaper",
        url=base_url + "/?wallpaper=1",
        width=100, height=100,
        resizable=False,
        frameless=True,  # 壁纸层不能带标题栏（挂 WorkerW 后会露出关闭/最大化按钮）
        on_top=True,
        **{hidden_kw: False},
    )


def _install_wallpaper(wallpaper_window):
    """返回给 webview.start 的回调：等 native 句柄就绪后挂 WorkerW。

    在 webview.start 的后台线程执行。原先把看门狗排在 mouse_hook.start()
    （阻塞的消息泵）之后，永远轮不到执行——现已调整为先起看门狗再装钩子。
    """
    def _run():
        from maestro import desktop as _desktop
        for _ in range(50):  # 最多等 10s，native 句柄就绪即挂
            try:
                native = getattr(wallpaper_window, "native", None)
                if native is not None:
                    hwnd = int(native.Handle.ToInt32())
                    if hwnd and _desktop.install_wallpaper_layer(hwnd):
                        logger.info("壁纸层已挂载 WorkerW")
                        _start_rect_watchdog(_desktop)
                        _MouseForwarder(wallpaper_window, _desktop).start()
                        return
            except Exception as e:  # noqa: BLE001 — 单轮失败重试
                logger.debug("挂载壁纸层重试: %s", e)
            time.sleep(0.2)
        logger.warning("壁纸层 10s 内未挂载成功，放弃（主窗口不受影响）")
    return _run


def _start_rect_watchdog(_desktop) -> None:
    """壁纸窗口偶发漂移自愈：周期校验实测矩形，漂了就拉回原位。

    漂移根因未查明（尺寸不变、位置移出屏幕左上，启动数分钟后偶发），
    先用 3s 轮询兜底保证可用性。daemon 线程随进程退出；
    _WALLPAPER_STOP 置位后可提前停。
    """
    def _run():
        while not _WALLPAPER_STOP.is_set():
            _WALLPAPER_STOP.wait(3.0)
            try:
                _desktop.ensure_wallpaper_rect()
            except Exception as e:  # noqa: BLE001 — 单轮失败下轮再试
                logger.debug("壁纸矩形校验失败: %s", e)
    threading.Thread(target=_run, name="rect-watchdog", daemon=True).start()


class _MouseForwarder:
    """全局鼠标事件 → 壁纸层转发器（与网页一致的使用体验）。

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

    _BTN = {"ldown": ("mousedown", 0), "lup": ("mouseup", 0),
            "rdown": ("mousedown", 2), "rup": ("mouseup", 2),
            "mdown": ("mousedown", 1), "mup": ("mouseup", 1)}

    def __init__(self, wallpaper_window, desktop):
        """desktop._state["rect"] 由 install_wallpaper_layer 写入（挂载完成后才构造）。"""
        self.win = wallpaper_window
        self.desktop = desktop
        self.vx, self.vy, self.vw, self.vh = desktop._state["rect"]
        self.q: queue.Queue = queue.Queue()
        self.last = 0.0
        self.pressed = False  # 左键按住中（转发 move 带 buttons=1，支持页面内拖拽）

    def start(self) -> None:
        """启动 worker 线程并安装全局钩子（阻塞在钩子消息泵，须在后台线程调）。"""
        threading.Thread(target=self._worker, name="mouse-forward-worker",
                         daemon=True).start()
        from maestro import mouse_hook
        mouse_hook.start(self._on_move, self.on_interact)

    # —— 坐标换算：屏幕物理坐标 → 页面 CSS 像素表达式 ——
    def _cx(self, x: int) -> str:
        return f"({x}-{self.vx})*innerWidth/{self.vw}"

    def _cy(self, y: int) -> str:
        return f"({y}-{self.vy})*innerHeight/{self.vh}"

    def _hit_query(self, x: int, y: int) -> str:
        """问页面这个点是否属于壁纸 UI。异常按'非交互'处理。"""
        expr = (f"window.__maestroHit?window.__maestroHit({x},{y},"
                f"{self.vx},{self.vy},{self.vw},{self.vh}):'nohelper'")
        try:
            r = self.win.evaluate_js(expr)
        except Exception as e:  # noqa: BLE001 — 查询失败按非交互处理
            logger.debug("hit_query 失败 (%s,%s): %s", x, y, e)
            return "err"
        if r == "nohelper":  # 页面刷新过：重注入一次再问
            try:
                self.win.evaluate_js(self._HIT_HELPER)
                r = self.win.evaluate_js(expr)
            except Exception as e:  # noqa: BLE001
                logger.debug("hit_helper 重注入失败 (%s,%s): %s", x, y, e)
                return "err"
        return r if isinstance(r, str) else "err"

    def _on_move(self, x: int, y: int) -> None:
        now = time.time()
        if now - self.last < 0.03 and not self.pressed:  # ~30Hz；拖拽时不节流
            return
        self.last = now
        try:
            self.win.evaluate_js(
                f"window.dispatchEvent(new MouseEvent('mousemove',"
                f"{{clientX:{self._cx(x)},clientY:{self._cy(y)},"
                f"buttons:{1 if self.pressed else 0}}}))")
        except Exception as e:  # noqa: BLE001 — 个别 move 丢失无碍
            logger.debug("mousemove 转发失败: %s", e)

    def _dispatch(self, kind: str, x: int, y: int, delta: int | None) -> str:
        """worker 线程：把事件注入页面。返回 JS 判定结果字符串。"""
        px, py = self._cx(x), self._cy(y)
        if kind == "wheel":
            return self.win.evaluate_js(f"""(function(){{
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
        mtype, btn = self._BTN[kind]
        return self.win.evaluate_js(f"""(function(){{
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

    def _find_webview2_ctl(self, form_ctl):
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
                except Exception:  # noqa: BLE001 — 单节点异常继续遍历
                    continue
        except Exception as e:  # noqa: BLE001 — 遍历失败走 Win32 兜底
            logger.debug("WebView2 控件查找失败: %s", e)
        return None

    def _focus_wallpaper(self) -> bool:
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
            native = self.win.native
            hwnd = int(native.Handle.ToInt32())
            my_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            fg = ctypes.windll.user32.GetForegroundWindow()
            if int(fg or 0) != hwnd:
                fg_tid = ctypes.windll.user32.GetWindowThreadProcessId(fg, None)
                ctypes.windll.user32.AttachThreadInput(my_tid, fg_tid, True)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                ctypes.windll.user32.AttachThreadInput(my_tid, fg_tid, False)
            ctl = self._find_webview2_ctl(native)
            if ctl is not None:
                from System import Action
                native.BeginInvoke(Action(ctl.Focus))
                return True
        except Exception as e:  # noqa: BLE001 — .NET 路径失败走 Win32 兜底
            logger.debug("WebView2 Focus 路径失败: %s", e)
        # 兜底：纯 Win32 焦点转移
        try:
            hwnd = int(self.win.native.Handle.ToInt32())
            for target in (self.desktop.find_webview_input_hwnds(hwnd) or [hwnd]):
                if self.desktop.set_focus_without_activation(target):
                    return True
        except Exception as e:  # noqa: BLE001 — 兜底也失败只能放弃
            logger.debug("Win32 焦点兜底失败: %s", e)
        return False

    def _worker(self) -> None:
        """队列消费者：慢操作串行执行；停机标志置位后退出（规则：循环必有出口）。"""
        while not _WALLPAPER_STOP.is_set():
            try:
                kind, x, y, delta = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                r = self._dispatch(kind, x, y, delta)
                if kind == "lup" and r == "editable":  # editable 在 mouseup 时判定
                    self._focus_wallpaper()
            except Exception as e:  # noqa: BLE001 — 单事件失败不影响后续
                logger.debug("dispatch %s (%s,%s) 失败: %s", kind, x, y, e)

    def on_interact(self, kind: str, x: int, y: int, delta: int | None) -> bool:
        """钩子线程：快速决策是否吞掉。右键永远放行。"""
        if kind in ("rdown", "rup"):
            return False
        mine = self._hit_query(x, y) == "mine"
        if not mine and not self.desktop.point_over_empty_desktop(x, y):
            return False
        if kind == "ldown":
            self.pressed = True
        elif kind == "lup":
            self.pressed = False
        self.q.put((kind, x, y, delta))
        return True


def _teardown(proc: subprocess.Popen) -> None:
    """退出清理：停壁纸线程 → 卸壁纸层 → 终止后端。"""
    _WALLPAPER_STOP.set()
    if sys.platform == "win32":
        try:
            from maestro import desktop as _desktop
            _desktop.uninstall_wallpaper_layer()
        except Exception as e:  # noqa: BLE001 — 清理失败不阻断退出
            logger.warning("卸载壁纸层失败: %s", e)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _gui_mode(args) -> int:
    """完整桌面模式：后端 + 壁纸层 + PyWebView 主窗口。"""
    proc = _spawn_server(args.port)
    base_url = f"http://127.0.0.1:{args.port}"
    try:
        _wait_health(f"{base_url}/api/healthz", timeout=30)
    except Exception:
        proc.terminate()
        proc.wait(timeout=5)
        raise

    threading.Thread(target=_watch_server, args=(proc,), daemon=True).start()

    try:
        import webview  # type: ignore[import-not-found]
    except ImportError as e:
        logger.warning("PyWebView 未安装(%s)，退到系统浏览器模式", e)
        return _fallback_browser(base_url, proc)

    _open_main_window(webview, base_url)
    wallpaper_window = _open_wallpaper_window(webview, base_url)

    try:
        webview.start(_install_wallpaper(wallpaper_window)
                      if wallpaper_window else None)
    except Exception as e:
        logger.error("PyWebView 启动失败: %s", e)
        return _fallback_browser(base_url, proc, exit_code=1)
    finally:
        _teardown(proc)
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
