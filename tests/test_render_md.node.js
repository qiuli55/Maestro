// 前端 renderMd 冒烟测试（零依赖）：node tests/test_render_md.node.js
// 验证升级后的 markdown 渲染：代码块/表格/加粗/链接/转义防 XSS。
global.window = global;
const path = require("path");
require(path.join(__dirname, "..", "web", "app.js"));
const M = global.Maestro;

let pass = 0, fail = 0;
function check(cond, msg) {
  if (cond) { pass++; console.log("  ok  - " + msg); }
  else { fail++; console.error("  FAIL - " + msg); }
}
function has(html, sub) { return html.indexOf(sub) !== -1; }

// 1) 围栏代码块：内容被转义、包成 <pre><code>
const codeOut = M.renderMd("```\nconst a = <b>;\n```");
check(has(codeOut, "<pre class=\"code\"><code>const a = &lt;b&gt;;</code></pre>"), "围栏代码块转义并包裹 <pre><code>");

// 2) 行内代码
check(has(M.renderMd("用 `pip` 安装"), "<code>pip</code>"), "行内代码 <code>");

// 3) 加粗
check(has(M.renderMd("这是 **重点** 内容"), "<strong>重点</strong>"), "加粗 <strong>");

// 4) 表格（GFM 基础）
const tbl = M.renderMd("| 名称 | 数量 |\n|---|---|\n| A | 1 |\n| B | 2 |");
check(has(tbl, "<table class=\"md-table\">") && has(tbl, "<th>名称</th>") && has(tbl, "<td>A</td>"), "GFM 表格渲染");

// 5) 安全链接放行
check(has(M.renderMd("[官网](https://example.com)"), "href=\"https://example.com\""), "https 链接放行");

// 6) 危险协议链接被丢弃（仅留文字）
const bad = M.renderMd("[点我](javascript:alert(1))");
check(!has(bad, "href=") && has(bad, "点我"), "javascript: 链接被丢弃，仅留文字");

// 7) 裸 HTML 被转义（防 XSS）
check(has(M.renderMd("<script>alert(1)</script>"), "&lt;script&gt;") && !has(M.renderMd("<img src=x onerror=1>"), "<img"), "裸 HTML 转义防 XSS");

// 8) 标题 / 列表
check(has(M.renderMd("# 标题"), "<h1>标题</h1>"), "一级标题");
check(has(M.renderMd("- 项目一\n- 项目二"), "<ul>") && has(M.renderMd("- 项目一\n- 项目二"), "<li>项目一</li>") && has(M.renderMd("- 项目一\n- 项目二"), "</ul>"), "无序列表");

// 9) 代码块内的 markdown 不被二次处理
check(!has(M.renderMd("```\n**不是加粗**\n```"), "<strong>"), "代码块内 ** 不被当作加粗");

console.log(`\nrenderMd 测试: ${pass} 通过, ${fail} 失败`);
process.exit(fail ? 1 : 0);
