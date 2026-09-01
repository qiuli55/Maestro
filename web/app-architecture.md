# web/app.js 架构

> **目的**：让新进开发者能快速定位代码而不必通读 2397 行 IIFE。

`web/app.js` 是单文件 IIFE（行 1-2399），按职责分 13 段。运行期行为零外部依赖：HTML `<script src="app.js">` 加载即跑；HTML 内 `onclick="toggleList(event)"` 等直接调到 IIFE 内提升的全局函数。

## 段位索引

| 行号 | 段名 | 关键导出（HTML 或其他段直接调的全局） |
|---|---|---|
| 4-30 | WindowManager 多窗口系统 | `winCreate` / `winActivate` / `winClose` / `winMinimize` / `winMaximize` / `saveWindowsState` / `restoreWindows` / `winTabsRender` / `winSelectRender` / `newWin` / `winCreateFromConv` / `mergeWindows` |
| 31-90 | 会话 ↔ 窗口宿主关系 | `msgsEl` / `winOfConv` / `ensureMsgsContainer` / `parkMsgs` |
| 91-499 | 合并窗口（多标签大窗口） | （无 HTML 直接调用，被 winCreate 触发） |
| 500-608 | 消息渲染引擎（Markdown / 头像 / 时间分隔 / 操作按钮） | `mdRender` / `buildMsgEl` / `applyFold` / `maybeAppendDivider` / `winAppendMsg` / `showTyping` / `hideTyping` / `backfillAllHistories` |
| 609-767 | 跨对话上下文关联 UI | `loadConvs` / `bindConvSearch` / `renderConvItem` / `createNewConv` / `setCurrentConv` / `loadSkillLib` |
| 768-809 | WebSocket 事件通道 | `connectEvtWs` / `__evtWsFeed`（e2e 调试） |
| 810-878 | 审批卡片（沙箱越权命令批准/拒绝） | `showApprovalCard` |
| 879-1077 | 任务进度卡片 | `winAppendTaskCard` / `renderTaskCard` / `pollTaskCard` / `taskConvOf` |
| 1078-1334 | 拖拽 + 调整大小 + 仪表盘 + 时钟 + 视差 | `toggleList`（HTML `onclick="toggleList(event)"`） / `tickClock` / `applyParallax` / `fetchDashboard` / `poll` / `statusText` / `fmtRelTime` |
| 1335-1715 | 跨对话上下文关联（数据层 + 历史回灌） | `saveConvLinks` / `relTime` / `refreshPlaceholder` / `setCurrentConv` |
| 1716-1785 | 工作流：选择器 + 可视化卡片编排器 | `loadWorkflows` / `loadSkillLib` / `onWorkflowChange` |
| 1786-1983 | 可视化编排器 | `wfOpen` / `closeWfBuilder` / `renderWfBuilder` / `wfCardEl` / `wfAgentLabel` / `wfCollectSubtasks` / `wfSave` / `wfRun` |
| 1984-2327 | 运行视图：编排器面板内实时显示环节卡片状态/产出 | `wfRunViewStart` / `wfRunViewStop` / `wfRunViewTick` / `wfRunViewRender` |
| 2328-2397 | WebSocket 消息路由 + 主 IIFE 关闭 | （最终 `setTimeout(connectWS,1000)`） |

## 跨段依赖

- **winCreate**（908 行）调 `bindWinEvents`/`winActivate`/`winTabsRender`/`saveWindowsState`——全部同段或 winOfConv（msg-helpers）；
- **winAppendMsg**（540 行）调 `hideTyping`/`maybeAppendDivider`/`buildMsgEl`/`applyFold`——后两个在 chat 段；
- **winAppendTaskCard**（899 行）调 `maybeAppendDivider`/`hideTyping`（chat 段）+ `renderTaskCard`（同段）；
- **wfRun**（2101 行）调 `winAppendTaskCard`（wstask 段）+ `wfRunViewStart`（workflows 段）；
- **wfCollectSubtasks**（1970 行）调 `winCreate`（windows 段）。

**依赖方向**单调无环——任何 ES module 拆分必须保持这个顺序。**这是为什么本项目暂不拆模块**的核心理由：6 个 IIFE 段互相通过 104 个函数紧密耦合，强拆会引入 30+ 微观回归而无明显收益。

## 状态共享

| 变量 | 含义 | 所在段 |
|---|---|---|
| `MAESTRO` | API 基址 | IIFE 顶层（1102） |
| `wins` / `winZ` / `WIN_OFFSET` | 窗口注册表 + z-index + 堆叠偏移 | windows |
| `convHost` / `convMeta` / `convLinks` / `convWf` | 会话→窗口映射 / 元数据 / 关联 / 工作流记忆 | windows + chat（共享 IIFE 作用域） |
| `activeWinId` / `selectedWinId` | 当前焦点 | windows |
| `parking` / `winContainer` / `winTabsEl` | DOM 引用 | windows |
| `wfState` | 工作流编排器状态（环节/卡片/启用） | workflows |
| `TC_FINAL` / `TC_APPROVALS` | 任务终态判断 + 审批记录 | wstask |
| `TC_STATUS` | 状态码→中文映射 | wstask |
| `MD_FOLD_LEN` / `AVATAR_HTML` | Markdown 折叠阈值 / 头像 HTML | chat |
| `evtWs` / `evtWsTimer` | WebSocket 客户端 + 重连计时 | wstask |
| `_main_loop` (server.py) / 各项 IIFE 内部 | 跨进程状态只在 lifespan 启动时设一次 | — |

## 维护指南

- **修改 Markdown 渲染逻辑** → 500-608 行的 `mdRender` / `mdInline` / `mdCodeBlock` / `applyFold`；改完跑 `pytest tests/test_security_headers.py -k 折叠` 验（无对应测试则跑 e2e）；
- **修改任务卡片** → 879-1077 行的 `winAppendTaskCard` / `renderTaskCard` / `pollTaskCard`；
- **修改工作流编排器** → 1716-2327 行（最复杂的一段，含选择器/编排器/运行视图三段）；改完用 `pytest tests/test_workflows.py` 验；
- **修改窗口合并/拖拽** → 91-499 行；
- **修改 WebSocket 订阅逻辑** → 768-809 行（很短，注意 `__evtWsFeed` 是 e2e 调试入口，别改签名）。

## 拆分路线图（暂不执行）

如未来要拆 ES modules（`<script type="module">`），按以下顺序保证可平移：

1. `core.js`：50-1102 行的 `MAESTRO`/`Wins`/`convMeta`/`wfState` 等状态对象 → `window.Maestro.state`
2. `windows.js`：4-499 行
3. `chat.js`：500-767 行
4. `wstask.js`：768-1077 行
5. `workflows.js`：1716-2327 行
6. `dashboard.js`：1078-1715 行
7. `tail.js`：2328-2397 行 + 跨段胶水

每段暴露的全局函数必须显式 `export`（否则 HTML `onclick` 找不到）。**改完后 e2e 用例 `test_frontend_smoke` 必须仍通过**。
