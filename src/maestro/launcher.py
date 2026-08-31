"""Maestro 桌面启动器：拉起后端 + 壁纸层 + 主窗口，关主窗口时整体退。

双进程模型：
  - launcher 进程：管桌面集成（PyWebView）+ 主窗口事件循环
  - server 子进程：`python -m maestro.server`（走 sys._MEIPASS 独立的 module 查找）

为什么不用同进程：PyInstaller 打包后 sys._MEIPASS 模块路径与当前进程隔离，
PyWebView 嵌入的 Chromium 抢 GIL 让 uvicorn 高频心跳受影响。分开两进程后浏览器崩
了也不连带后端崩溃；launcher 的 desktop.py 也能独立管理壁纸层生命周期。

端口策略：优先 8787，被占就 8788..8797；端口写入 user_data_dir()/port.txt，
前端始终走同源 location.origin，无需硬编码。
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

from . import runtime
from .runtime import project_root, user_data_dir


def _port_free(port: int) -> bool:
    """端口当前是否可绑定（复用 port.txt 前验证，避免启动后才发现被占）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def _find_free_port(start: int = 8787, end: int = 8797) -> int:
    # 注意：bind-then-close 存在理论 TOCTOU 窗口（探测后、使用前被抢占），
    # 概率极低；真发生时 _wait_health 30s 超时会暴露问题而非静默。
    """返回 [start, end] 内第一个空闲端口；全占则 raise。"""
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
    """启动后端子进程（独立 -m maestro.server）。"""
    env = dict(os.environ)
    env["MAESTRO_PORT"] = str(port)
    env["MAESTRO_HOST"] = "127.0.0.1"
    # MAESTRO_HOME：让后端知道产物根（已写 DB / outputs / backups）
    env.setdefault("MAESTRO_HOME", str(project_root()))
    # PATH 透传
    # cwd=src：让 -m maestro.server 能解析 maestro 包（源码运行 + 打包后 _internal/src 都可用）
    src_dir = Path(__file__).resolve().parent
    return subprocess.Popen(
        [sys.executable, "-m", "maestro.server"],
        env=env,
        cwd=str(src_dir),
        stdout=None,  # 继承当前 stdout/stderr（开发时可见；打包后 NUL）
        stderr=None,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
    )


def _watchdog(proc: subprocess.Popen, on_die) -> None:
    """子进程看门狗：死了就调 on_die（通常用于关主窗口退到后台）。"""
    while True:
        if proc.poll() is not None:
            on_die(proc.returncode)
            return
        time.sleep(1.0)


def run() -> int:
    """启动器主入口（被 PyInstaller 调）。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-gui", action="store_true",
                        help="仅启服务不开窗口（CI/服务端调试用）")
    parser.add_argument("--port", type=int, default=None,
                        help="强制指定端口（默认从 user_data/port.txt 读，被占就 8788..）")
    args = parser.parse_args()

    # 端口选择：CLI 强定 > 持久化端口 > 默认探测
    port = args.port
    if port is None:
        # 持久化端口优先复用，但必须验证仍空闲（被其他应用占用则继续探测）
        saved = _read_port_file()
        if saved and _port_free(saved):
            port = saved
        else:
            port = _find_free_port()

    base_url = f"http://127.0.0.1:{port}"
    _write_port_file(port)

    # 1) 启后端子进程
    proc = _spawn_server(port)
    try:
        _wait_health(f"{base_url}/api/healthz", timeout=30)
    except Exception:
        proc.terminate()
        proc.wait(timeout=5)
        raise

    if args.no_gui:
        print(f"[Maestro] 后端就绪：{base_url}")
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
        return 0

    # 2) 后台线程看门狗
    die_event = threading.Event()

    def _on_die(rc):
        die_event.set()

    threading.Thread(target=_watchdog, args=(proc, _on_die), daemon=True).start()

    # 3) 启动 PyWebView（系统 Chromium / Edge 内核）
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

    # 3a) 主窗口：1400x900 起步，可缩，关闭不退出 launcher（壁纸层仍在跑）
    main_window = webview.create_window(
        title="Maestro",
        url=base_url + "/",
        width=1400,
        height=900,
        min_size=(900, 600),
        resizable=True,
    )

    # 3b) 壁纸层：独立子窗口，挂到桌面（Windows-only）；非 Win 不启
    if sys.platform == "win32":
        wallpaper_window = webview.create_window(
            title="Maestro Wallpaper",
            url=base_url + "/?wallpaper=1",  # 前端按 ?wallpaper=1 切换壁纸模式
            width=100, height=100,  # 占位，挂 WorkerW 时会重设全屏
            resizable=False,
            frameless=False,  # PyWebView 内置去标题栏较麻烦；先保留，setparent 后再隐藏
            on_top=True,
            visible=False,  # 创建后立即隐藏，等挂入 WorkerW 再显示
        )

    # 4) 启 GUI 事件循环
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
        # 5) 主窗口关闭：清理壁纸层 + 终止后端
        if sys.platform == "win32":
            try:
                from . import desktop
                desktop.uninstall_wallpaper_layer()
            except Exception:  # noqa: BLE001
                pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    return 0


if __name__ == "__main__":
    sys.exit(run())
