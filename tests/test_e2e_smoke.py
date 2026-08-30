"""前端浏览器冒烟测试（Playwright，可选）。

有 playwright + 浏览器二进制时运行（无则自动跳过）：
    pytest tests/test_e2e_smoke.py -q

覆盖：资源加载、多窗口、Markdown 渲染、窗口合并、工作流选择器、关联弹层。
服务端由本测试文件临时拉起（独立 DB，不碰开发库）。
"""
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytest.importorskip("playwright.sync_api")
try:
    from playwright.sync_api import sync_playwright
except Exception:  # noqa: BLE001
    pytest.skip("playwright 不可用", allow_module_level=True)

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="module")
def server():
    """临时拉起 Maestro 服务（独立 DB + 端口）。"""
    env = dict(os.environ)
    env["MAESTRO_DB"] = str(ROOT / f".e2e_tmp_{uuid.uuid4().hex[:8]}.db")
    env["MAESTRO_DB_POOL"] = "0"
    env["MAESTRO_PORT"] = str(PORT)
    err_log = ROOT / ".e2e_server_err.log"
    proc = subprocess.Popen(
        [sys.executable, "-m", "maestro.server"],
        cwd=str(ROOT / "src"),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=open(err_log, "w"),
    )
    # 等端口就绪
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 禁代理（本机服务）
    for _ in range(40):
        try:
            opener.open(f"{BASE}/api/healthz", timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    else:
        proc.terminate()
        pytest.fail(f"服务未在预期时间内启动: {err_log.read_text()[:500]}")
    yield BASE
    proc.terminate()
    proc.wait(timeout=10)
    for f in Path(ROOT).glob(".e2e_tmp_*.db*"):
        f.unlink(missing_ok=True)
    Path(ROOT / ".e2e_server_err.log").unlink(missing_ok=True)


def test_frontend_smoke(server):
    """核心前端功能冒烟：资源/窗口/Markdown/合并/工作流/链接。"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(server, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)

        # 资源加载
        assert page.locator('link[href="style.css"]').count() == 1
        assert page.locator('script[src="app.js"]').count() == 1
        assert page.evaluate("typeof window.winCreate") == "function"

        # 双窗口 + Markdown 渲染
        page.evaluate("""() => {
          window.winCreate("e2eA", "窗口A", "chat");
          window.winAppendMsg("e2eA", "user", "你好");
          window.winAppendMsg("e2eA", "assistant", "## 标题\\n\\n- 列表项");
          window.winCreate("e2eB", "窗口B", "chat");
        }""")
        page.wait_for_timeout(300)
        assert page.locator('.win-msgs[data-conv="e2eA"]').count() == 1
        assert page.locator('.win-msgs[data-conv="e2eA"] .msg-content ul').count() == 1
        assert page.locator('.win-msgs[data-conv="e2eA"] .msg-avatar').count() == 1

        # 合并窗口
        page.click("#merge-btn")
        page.wait_for_timeout(400)
        assert page.locator(".win.merged").count() == 1
        assert page.locator(".win.merged .mw-tab").count() == 2

        # 任务模式 → 工作流选择器
        page.click("#mode-task")
        page.wait_for_timeout(400)
        assert page.locator("#workflow-select option").count() >= 6

        # 关联弹层
        page.click('.win.merged .win-btn[data-win="link"]')
        page.wait_for_timeout(300)
        assert page.locator("#link-pop.show").count() == 1

        # 页面 JS 错误
        assert not errors, f"页面 JS 报错: {errors}"
        browser.close()
