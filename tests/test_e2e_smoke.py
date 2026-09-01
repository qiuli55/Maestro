"""前端浏览器端到端套件（Playwright，可选）。

本套件按真实用户旅程覆盖：资源/聊天/任务/工作流/审批/多窗口/历史/
搜索/响应头/无 JS 错误。服务由本文件临时拉起（独立 DB，不碰开发库）。

有 playwright + 浏览器二进制时运行（无则自动 skip）：
    pytest tests/test_e2e_smoke.py -q
"""
from __future__ import annotations

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
    from playwright.sync_api import sync_playwright, expect
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
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(60):
        try:
            opener.open(f"{BASE}/api/healthz", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.fail(f"服务未在预期时间内启动: {err_log.read_text()[:500]}")
    yield BASE
    proc.terminate()
    proc.wait(timeout=10)
    for f in Path(ROOT).glob(".e2e_tmp_*.db*"):
        f.unlink(missing_ok=True)
    Path(ROOT / ".e2e_server_err.log").unlink(missing_ok=True)


@pytest.fixture()
def browser_page(server):
    """每个测试一个干净浏览器页面 + 收集 pageerror。"""
    js_errors: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()
        page.on("pageerror", lambda e: js_errors.append(str(e)))
        page.goto(server, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)  # 让 IIFE 末尾的 window.__e2e__ = {...} 跑完
        yield page, js_errors
        ctx.close()
        b.close()


# ============================================================================
# 资源加载 + 响应头
# ============================================================================

def test_resource_assets_loaded(server):
    """首屏关键资源：favicon、style.css、app.js 全部就位；所有全局函数挂到 window。"""
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_context().new_page()
        page.goto(server, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        assert page.locator('link[rel="icon"]').count() >= 1
        assert page.locator('link[rel="stylesheet"][href="style.css"]').count() == 1
        assert page.locator('script[src="app.js"]').count() == 1
        for fn in ("winCreate", "winAppendMsg", "winSelectRender", "mergeWindows",
                   "renderTaskCard", "pollTaskCard", "wfOpen", "loadWorkflows",
                   "loadConvs", "backfillAllHistories", "connectEvtWs",
                   "showTyping", "hideTyping", "mdRender"):
            # 这些函数原本是 IIFE 内部，e2e 通过 window.__e2e__ 暴露
            assert page.evaluate(f"typeof window.__e2e__.{fn}") == "function", f"__e2e__.{fn} 未挂到 window"
        b.close()


def test_security_headers_on_html(server):
    """/ 响应头含 CSP / nosniff / X-Frame-Options / Referrer-Policy / Permissions-Policy。"""
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_context().new_page()
        resp = page.goto(server, wait_until="domcontentloaded")
        h = resp.headers
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "no-referrer"
        assert "permissions-policy" in h
        assert "camera=()" in h["permissions-policy"]
        csp = h["content-security-policy"]
        for needle in ("default-src 'self'", "object-src 'none'",
                       "frame-ancestors 'none'", "script-src 'self'"):
            assert needle in csp, f"CSP 缺 {needle!r}"
        b.close()


def test_no_console_errors_on_load(server):
    """加载 5s 内无任何 JS 异常。"""
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_context().new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        page.goto(server, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        assert not errs, f"页面错误: {errs[:5]}"
        b.close()


# ============================================================================
# 聊天：发消息 / Markdown 渲染 / 折叠 / 头像 / 打字指示
# ============================================================================

def test_chat_message_with_markdown(browser_page):
    """发带 Markdown 的消息：标题/列表/行内代码/头像全渲染，无 JS 错误。"""
    page, errs = browser_page
    page.evaluate("""() => {
      window.__e2e__.winCreate("m1", "Markdown测试", "chat");
      window.__e2e__.winAppendMsg("m1", "user", "hi");
      window.__e2e__.winAppendMsg("m1", "assistant", "## H\\n\\n- A\\n- B\\n\\n\`x = 1\`");
    }""")
    page.wait_for_timeout(300)
    body = page.locator('.win-msgs[data-conv="m1"]')
    expect(body.locator(".msg-content h2")).to_have_text("H")
    assert body.locator(".msg-content ul li").count() == 2
    assert body.locator(".msg-content code.md-inline").count() == 1
    assert body.locator(".msg-avatar").count() >= 1
    assert not errs


def test_chat_long_message_folded(browser_page):
    """长消息折叠"展开全文"按钮，点击展开/收起。"""
    page, errs = browser_page
    page.evaluate(
        "(text) => { window.__e2e__.winCreate('m2', '折叠测试', 'chat'); window.__e2e__.winAppendMsg('m2', 'assistant', text); }",
        "A" * 2000,
    )
    page.wait_for_timeout(300)
    msg = page.locator('.win-msgs[data-conv="m2"] .win-msg.assistant')
    expect(msg).to_have_class("win-msg assistant md-msg folded")
    assert page.locator('.md-fold-btn:has-text("展开全文")').count() == 1
    page.click(".md-fold-btn")
    page.wait_for_timeout(200)
    expect(msg).not_to_have_class("folded")
    assert page.locator('.md-fold-btn:has-text("收起")').count() == 1
    assert not errs


def test_chat_typing_indicator_and_completion(browser_page):
    """发消息 → typing 出现 → 流式完成 typing 消失 → 回复含发送内容。

    stub 的 SSE 一次性返回完整帧（无网络延迟），typing 存在时间可能 <150ms——
    所以在 stub 里记录 pump 前后状态，验证完整生命周期而非定时截图。
    """
    page, errs = browser_page
    page.evaluate("""() => {
      window.__typingSeen = false;
      const orig = window.fetch;
      window.fetch = function(url, opts){
        if(String(url).includes("/api/chat/stream")){
          const body = JSON.parse(opts.body);
          const NL = String.fromCharCode(10);
          const frame = "data: " + JSON.stringify({delta: "echo:" + body.message}) + NL + NL +
                        "data: " + JSON.stringify({done: true, reply: "echo:" + body.message}) + NL + NL;
          // 用微任务延迟一帧，让 pump 的第一帧前 typing 气泡有入列机会
          return new Promise(resolve => {
            setTimeout(() => {
              window.__typingSeen = !!document.querySelector('.win-msg.typing');
              resolve(new Response(frame,
                {status: 200, headers: {"Content-Type": "text/event-stream"}}));
            }, 120);
          });
        }
        return orig.apply(this, arguments);
      };
      window.__e2e__.winCreate("m3", "流式", "chat");
    }""")
    page.click("#mode-chat")
    page.evaluate("window.__e2e__.selectedWinId = 'm3'; window.__e2e__.updateDockPlaceholder();")
    page.fill("#dock-input", "ping")
    page.click("#dock-send")
    # 此时 fetch 还在 120ms 延迟里，typing 气泡应已入列
    page.wait_for_timeout(60)
    assert page.locator('.win-msgs[data-conv="m3"] .win-msg.typing').count() == 1, \
        "发消息后应立即出现 typing 气泡"
    page.wait_for_timeout(1800)
    # 流式完成：typing 消失 + 回复渲染
    assert page.locator('.win-msgs[data-conv="m3"] .win-msg.typing').count() == 0
    last = page.evaluate("""() => {
      const a = document.querySelectorAll('.win-msgs[data-conv="m3"] .win-msg.assistant');
      return a.length ? a[a.length-1].textContent : '';
    }""")
    assert "echo:ping" in last
    assert window_typing_seen(page), "stub 记录的 typing 状态应为 true"
    assert not errs


def window_typing_seen(page):
    return page.evaluate("window.__typingSeen === true")
    assert not errs


# ============================================================================
# 多窗口：合并 / Tab 切换
# ============================================================================

def test_window_merge_creates_multi_tab(browser_page):
    """两个窗口 → 合并 → 一个多标签大窗口（2 个 tab）。"""
    page, errs = browser_page
    page.evaluate("""() => {
      window.__e2e__.winCreate('wA', '窗口A', 'chat');
      window.__e2e__.winAppendMsg('wA', 'user', '1');
      window.__e2e__.winCreate('wB', '窗口B', 'chat');
    }""")
    page.wait_for_timeout(300)
    page.click("#merge-btn")
    page.wait_for_timeout(500)
    assert page.locator(".win.merged").count() == 1
    assert page.locator(".win.merged .mw-tab").count() == 2
    assert not errs


def test_window_click_tab_switches_active(browser_page):
    """点合并窗口的 tab 切换激活子窗口；body 切换到对应会话内容。"""
    page, errs = browser_page
    page.evaluate("""() => {
      window.__e2e__.winCreate('tA', 'A', 'chat');
      window.__e2e__.winAppendMsg('tA', 'user', 'a');
      window.__e2e__.winCreate('tB', 'B', 'chat');
      window.__e2e__.winAppendMsg('tB', 'user', 'b');
    }""")
    page.click("#merge-btn")
    page.wait_for_timeout(500)
    page.click('.mw-tab[data-conv="tB"]')
    page.wait_for_timeout(200)
    active = page.evaluate("document.querySelector('.win.merged .mw-tab.active').dataset.conv")
    assert active == "tB"
    body_text = page.locator(".win.merged .win-body").inner_text()
    assert "b" in body_text
    assert not errs


# ============================================================================
# 任务卡片 + 审批
# ============================================================================

def test_task_card_rendered_via_winAppendTaskCard(browser_page):
    """winAppendTaskCard 创建任务卡：含状态徽章 + 元信息（agent/模型/taskId）。"""
    page, errs = browser_page
    page.evaluate("""() => {
      window.__e2e__.winCreate('t1', '任务卡', 'chat');
      window.__e2e__.winAppendTaskCard('t1', 'task_demo_1', ['embedded'], 'deepseek:deepseek-chat', 'p');
    }""")
    page.wait_for_timeout(300)
    card = page.locator('.task-card[data-task="task_demo_1"]')
    assert card.count() == 1
    expect(card.locator(".tc-status")).to_have_text("待拆分")
    meta = card.locator(".tc-meta").inner_text()
    assert "embedded" in meta
    assert "deepseek:deepseek-chat" in meta
    assert "task_demo_1" in meta
    assert not errs


def test_task_card_progress_via_renderTaskCard(browser_page):
    """renderTaskCard 渲染：进度条按完成比更新（2/4 = 50%），toggle 显示 2/4。"""
    page, errs = browser_page
    page.evaluate("""() => {
      window.__e2e__.winCreate('t2', 'P', 'chat');
      window.__e2e__.winAppendTaskCard('t2', 'task_p', ['embedded'], null, 'p');
    }""")
    page.wait_for_timeout(200)
    page.evaluate("""() => {
      window.__e2e__.renderTaskCard('t2', {
        task: { id: 'task_p', status: 'running' },
        subtasks: [
          {status:'done',desc:'a'},{status:'done',desc:'b'},
          {status:'running',desc:'c'},{status:'pending',desc:'d'},
        ],
      });
    }""")
    page.wait_for_timeout(200)
    bar_w = page.evaluate("""() => {
      const b = document.querySelector('.task-card[data-task="task_p"] .tc-bar i');
      return b ? b.style.width : null;
    }""")
    assert "50" in bar_w
    assert page.locator('.task-card[data-task="task_p"] .tc-sub').count() == 4
    assert "2/4" in page.locator(".tc-toggle").first.inner_text()
    assert not errs


def test_task_card_final_state_done_shows_output(browser_page):
    """DONE 状态：结果区显示、含复制按钮、长结果折叠。"""
    page, errs = browser_page
    page.evaluate("""() => {
      window.__e2e__.winCreate('t3', '完成', 'chat');
      window.__e2e__.winAppendTaskCard('t3', 'task_done', ['embedded'], null, 'p');
    }""")
    page.wait_for_timeout(200)
    long_result = "最终结果" + "x" * 1500
    page.evaluate(
        "(r) => { window.__e2e__.renderTaskCard('t3', { task: { id: 'task_done', status: 'done', result: r }, subtasks: [] }); }",
        long_result,
    )
    page.wait_for_timeout(300)
    card = page.locator('.task-card[data-task="task_done"]')
    expect(card.locator(".tc-status")).to_have_text("已完成")
    expect(card).to_have_class("task-card done-card")
    result = card.locator(".tc-result")
    result_class = result.get_attribute("class") or ""
    assert "show" in result_class.split() and "clamped" in result_class.split(),         f"结果区应有 show clamped 类，实得: {result_class!r}"
    assert result.inner_text().startswith("最终结果")
    assert page.locator('.tc-result-toggle:has-text("展开全文")').count() == 1
    assert not errs


def test_approval_card_renders_with_approve(browser_page, server):
    """showApprovalCard 渲染卡片：含 cmd + 批准/拒绝按钮；点批准后状态变"已批准"。"""
    page, errs = browser_page
    # 用 page.route 拦截审批接口（比 evaluate 内替换 fetch 稳定，无沙箱作用域问题）
    page.route("**/api/tasks/*/approvals/*", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"ok":true,"approval_id":"ap_e2e_1","decision":"approved"}'))
    page.evaluate("""() => {
      window.__e2e__.winCreate('ap1', '审批', 'chat');
      window.__e2e__.showApprovalCard({
        kind: 'approval', conv_id: 'ap1',
        content: JSON.stringify({
          approval_id: 'ap_e2e_1', task_id: 't_demo', subtask_id: 'st_1',
          cmd: 'rm -rf /tmp/test',
        }),
      });
    }""")
    page.wait_for_timeout(300)
    card = page.locator('.approval-card[data-approval="ap_e2e_1"]')
    assert card.count() == 1
    expect(card.locator(".tc-status")).to_have_text("待审批")
    expect(card.locator("code")).to_contain_text("rm -rf /tmp/test")
    assert card.locator(".ap-approve:has-text('批准执行')").count() == 1
    assert card.locator(".ap-reject:has-text('拒绝')").count() == 1
    card.locator(".ap-approve").click()
    page.wait_for_timeout(500)
    expect(card.locator(".tc-status")).to_have_text("已批准")
    # 按钮行被隐藏（display:none）
    assert not card.locator(".tc-approve-row").is_visible()
    assert not errs


# ============================================================================
# 工作流：选择器 / 编排器 / 卡片
# ============================================================================

def test_workflow_selector_presets_loaded(browser_page):
    """任务模式下工作流选择器有预设（>= 6 项）。"""
    page, errs = browser_page
    page.click("#mode-task")
    page.wait_for_timeout(500)
    assert page.locator("#workflow-select option").count() >= 6
    assert not errs


def test_workflow_builder_open_and_renders_stages(browser_page):
    """🧩 打开编排器：默认 2 环节各 1 张卡。"""
    page, errs = browser_page
    page.click("#mode-task")
    page.click("#wf-open-builder")
    page.wait_for_timeout(500)
    assert page.locator("#wf-builder.show").count() == 1
    assert page.locator(".wfb-stage").count() == 2
    assert page.locator(".wf-card").count() == 2
    assert not errs


def test_workflow_card_toggle_on_off(browser_page):
    """点卡片：亮/暗启用切换（off = 透明度 < 0.7）。"""
    page, errs = browser_page
    page.click("#mode-task")
    page.click("#wf-open-builder")
    page.wait_for_timeout(500)
    card = page.locator('.wf-card').first
    expect(card).to_have_class("wf-card on")
    card.click()
    page.wait_for_timeout(200)
    expect(card).to_have_class("wf-card off")
    op = page.evaluate("(el) => getComputedStyle(el).opacity", card.element_handle())
    assert float(op) < 0.7
    card.click()
    page.wait_for_timeout(200)
    expect(card).to_have_class("wf-card on")
    assert not errs


def test_workflow_card_use_prev_toggle_disabled_on_first_stage(browser_page):
    """第一环节 use_prev 勾选框禁用；第二环节可勾选。"""
    page, errs = browser_page
    page.click("#mode-task")
    page.click("#wf-open-builder")
    page.wait_for_timeout(500)
    page.click('.wf-card[data-si="0"][data-ci="0"] .wc-set')
    page.wait_for_timeout(200)
    cb0 = page.locator('.wf-card[data-si="0"][data-ci="0"] .wc-prev')
    assert cb0.is_disabled()
    page.click('.wf-card[data-si="1"][data-ci="0"] .wc-set')
    page.wait_for_timeout(200)
    cb1 = page.locator('.wf-card[data-si="1"][data-ci="0"] .wc-prev')
    assert not cb1.is_disabled()
    cb1.check()
    page.wait_for_timeout(200)
    assert cb1.is_checked()
    assert not errs


def test_workflow_card_add_skill_with_auto_category(browser_page):
    """技能输入框回车 → 后端分类（视频混剪 → 视频）→ 彩色 chip 显示。"""
    page, errs = browser_page
    page.click("#mode-task")
    page.click("#wf-open-builder")
    page.wait_for_timeout(500)
    page.click('.wf-card[data-si="0"][data-ci="0"] .wc-set')
    page.wait_for_timeout(200)
    page.fill('.wf-card[data-si="0"][data-ci="0"] .skill-add input', "视频混剪")
    page.press('.wf-card[data-si="0"][data-ci="0"] .skill-add input', "Enter")
    page.wait_for_timeout(800)
    chip = page.locator('.wf-card[data-si="0"][data-ci="0"] .wc-skill:has-text("视频混剪")')
    assert chip.count() == 1
    bg = page.evaluate("(el) => getComputedStyle(el).backgroundColor", chip.element_handle())
    assert "240" in bg, f"视频类色应为 #f0a24b（橙色），实得 {bg}"
    assert not errs


# ============================================================================
# 会话列表 / 搜索
# ============================================================================

def test_conversation_list_loads_via_loadConvs(browser_page):
    """window.__e2e__.loadConvs('chat') 渲染 cv-new 按钮 + .cv-item 列表。"""
    page, errs = browser_page
    page.evaluate("window.__e2e__.loadConvs('chat')")
    page.wait_for_timeout(500)
    assert page.locator("#conv-list #cv-new").count() == 1
    assert not errs


def test_conversation_search_filter(browser_page):
    """搜索框：输入不匹配关键词 → 列表清空；Esc 恢复。"""
    page, errs = browser_page
    page.evaluate("window.__e2e__.loadConvs('chat')")
    page.wait_for_timeout(500)
    # 会话列表默认 display:none，靠 .conv-wrap:hover 展开——用 hover 触发
    page.hover("#history-btn")
    page.wait_for_timeout(300)
    before = page.locator("#conv-list .cv-item").count()
    if before == 0:
        pytest.skip("无会话可搜（CI 隔离 DB 干净）")
    page.fill("#cv-search", "zzz不存在")
    page.wait_for_timeout(800)
    assert page.locator("#conv-list .cv-item").count() == 0
    page.focus("#cv-search")
    page.press("#cv-search", "Escape")
    page.wait_for_timeout(500)
    assert page.locator("#conv-list .cv-item").count() == before
    assert not errs


# ============================================================================
# 时钟
# ============================================================================

def test_clock_updates_after_2s(browser_page):
    """时钟元素（HH:MM 格式的 .txt）2.5s 后应更新（分钟可能不变但秒级重渲染一定发生）。

    页面有多个 .txt：时钟 HH:MM / 日期 YYYY/MM/DD / 'Fri' 静态文本。
    用正则挑出 HH:MM 格式的那个；2.5s 后分钟可能不变（如 18:31→18:31），
    但至少要验证 tickClock 每 1s 在跑——通过对比两次 innerText 抓取时间戳。
    """
    page, errs = browser_page
    # 找 HH:MM 格式的时钟元素
    clock_text = page.evaluate("""() => {
      const els = Array.from(document.querySelectorAll('.txt'));
      const clock = els.find(e => /^\\d{2}:\\d{2}$/.test(e.textContent.trim()));
      return clock ? clock.textContent.trim() : null;
    }""")
    if not clock_text:
        pytest.skip("无 HH:MM 时钟元素")
    # 验证格式合法
    import re
    assert re.match(r"^\d{2}:\d{2}$", clock_text), f"时钟格式异常: {clock_text!r}"
    # tickClock 每 1s 跑一次；等 65s 跨分钟边界太慢——改为验证
    # 秒级重渲染确实发生（textContent 节点被替换为相同值也算跑过）。
    # 用 MutationObserver 抓 tickClock 的写入事件：
    observed = page.evaluate("""() => new Promise(resolve => {
      const els = Array.from(document.querySelectorAll('.txt'));
      const clock = els.find(e => /^\\d{2}:\\d{2}$/.test(e.textContent.trim()));
      if (!clock) { resolve(null); return; }
      const obs = new MutationObserver(muts => {
        resolve({mutations: muts.length, now: clock.textContent.trim()});
        obs.disconnect();
      });
      obs.observe(clock, {childList: true, characterData: true, subtree: true});
      setTimeout(() => resolve({mutations: 0, now: clock.textContent.trim()}), 3000);
    })""")
    assert observed and observed["mutations"] > 0, f"tickClock 未在 3s 内更新时钟: {observed}"
    assert re.match(r"^\d{2}:\d{2}$", observed["now"]), f"更新后格式异常: {observed}"
    assert not errs


# ============================================================================
# 历史回灌
# ============================================================================

def test_window_restoration_pulls_history(browser_page):
    """刷新后窗口恢复：backfillAllHistories 调 /api/chat/history 拉回气泡。"""
    page, errs = browser_page
    page.evaluate("""() => {
      const orig = window.fetch;
      window.fetch = function(url, opts){
        if(String(url).includes('/api/chat/history')){
          return Promise.resolve(new Response(JSON.stringify({
            messages: [
              {role:'user', content:'历史1', created_at:'2025-01-01'},
              {role:'assistant', content:'历史2', created_at:'2025-01-01'},
            ]
          }), {status: 200, headers: {'Content-Type':'application/json'}}));
        }
        return orig.apply(this, arguments);
      };
      window.__e2e__.winCreate('hr1', '回灌', 'chat');
      const b = document.querySelector('.win-msgs[data-conv="hr1"]');
      b.innerHTML = ''; b._lastMsg = null; b.dataset.backfilled = '';
      window.__e2e__.backfillAllHistories();
    }""")
    page.wait_for_timeout(800)
    body = page.locator('.win-msgs[data-conv="hr1"]')
    assert body.locator(".win-msg").count() == 2
    assert "历史1" in body.inner_text()
    assert "历史2" in body.inner_text()
    assert body.get_attribute("data-backfilled") == "1"
    assert not errs


# ============================================================================
# 跨切：完整用户旅程
# ============================================================================

def test_full_journey_chat_then_task_with_stub(browser_page):
    """发消息 → 收流式回复 → 派任务 → 卡片进度更新（33%）。"""
    page, errs = browser_page
    page.evaluate("""() => {
      const orig = window.fetch;
      window.fetch = function(url, opts){
        if(String(url).includes('/api/chat/stream')){
          const b = JSON.parse(opts.body);
          const NL = String.fromCharCode(10);
          return Promise.resolve(new Response(
            'data: ' + JSON.stringify({delta: 'ok:' + b.message}) + NL + NL +
            'data: ' + JSON.stringify({done: true, reply: 'ok:' + b.message}) + NL + NL,
            {status: 200, headers: {'Content-Type':'text/event-stream'}}));
        }
        if(url.match(/\\/api\\/tasks\\/[^/]+$/)){
          return Promise.resolve(new Response(JSON.stringify({
            task: { id: 'j_task', status: 'running' },
            subtasks: [
              {status:'done',desc:'sub1'},
              {status:'running',desc:'sub2'},
              {status:'pending',desc:'sub3'},
            ],
          }), {status: 200, headers: {'Content-Type':'application/json'}}));
        }
        return orig.apply(this, arguments);
      };
      window.__e2e__.winCreate('j1', '旅程', 'chat');
      window.__e2e__.selectedWinId = 'j1'; window.__e2e__.updateDockPlaceholder();
    }""")
    page.fill("#dock-input", "hi")
    page.click("#dock-send")
    page.wait_for_timeout(1200)
    body = page.locator('.win-msgs[data-conv="j1"]')
    assert "ok:hi" in body.inner_text()
    page.evaluate("window.__e2e__.winAppendTaskCard('j1', 'j_task', ['embedded'], null, 'p')")
    page.wait_for_timeout(300)
    page.evaluate("""() => {
      window.__e2e__.renderTaskCard('j1', {
        task: { id: 'j_task', status: 'running' },
        subtasks: [
          {status:'done',desc:'sub1'},{status:'running',desc:'sub2'},{status:'pending',desc:'sub3'},
        ],
      });
    }""")
    page.wait_for_timeout(300)
    bar = page.evaluate("""() => {
      const el = document.querySelector('.task-card[data-task="j_task"] .tc-bar i');
      return el ? el.style.width : null;
    }""")
    assert "33" in bar
    assert not errs
