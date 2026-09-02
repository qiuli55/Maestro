"""Maestro 桌面启动器入口（PyInstaller 兼容包装）。

PyInstaller 把 launcher 当 __main__，相对导入 `from . import X` 失败。
这里把 launcher 重写为薄包装，真实业务逻辑放在 maestro.runtime + 调 maestro.cli。
"""
from __future__ import annotations

import argparse
import json
import os
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
_PY = r"C:\Users\A\.workbuddy\binaries\python\versions\3.13.12\python.exe"


def _spawn_server(port: int) -> subprocess.Popen:
    """启动后端子进程。

    frozen（PyInstaller）：spawn 自身 exe + --server-mode——依赖全在 _MEIPASS，
    系统 Python 没有 dotenv/fastapi 等第三方包，绝不能用系统 Python 跑源码。
    源码运行：spawn 系统 python -m maestro.server（PYTHONPATH=src）。
    """
    import shutil

    _log = open(user_data_dir() / f"maestro-server-{os.getpid()}.log", "w")
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--server-mode", "--port", str(port)]
        env = dict(os.environ)
        env["MAESTRO_PORT"] = str(port)
        env["MAESTRO_HOST"] = "127.0.0.1"
        env.setdefault("MAESTRO_HOME", str(project_root()))
        return subprocess.Popen(cmd, env=env, stdout=_log, stderr=subprocess.STDOUT)
    # 源码运行
    if Path(_PY).exists():
        py = _PY
    else:
        py = shutil.which("python") or shutil.which("python3") or sys.executable
    env = dict(os.environ)
    env["MAESTRO_PORT"] = str(port)
    env["MAESTRO_HOST"] = "127.0.0.1"
    env.setdefault("MAESTRO_HOME", str(project_root()))
    pp = str(src_dir())
    env["PYTHONPATH"] = pp + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [py, "-u", "-m", "maestro.server"],
        env=env,
        stdout=_log,
        stderr=subprocess.STDOUT,
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

    main_window = webview.create_window(
        title="Maestro",
        url=base_url + "/",
        width=1400,
        height=900,
        min_size=(900, 600),
        resizable=True,
        icon=_icon if Path(_icon).exists() else None,
    )

    if sys.platform == "win32":
        webview.create_window(
            title="Maestro Wallpaper",
            url=base_url + "/?wallpaper=1",
            width=100, height=100,
            resizable=False,
            frameless=False,
            on_top=True,
            visible=False,
        )

    try:
        webview.start()
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
