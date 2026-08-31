# 🎼 Maestro

**A Wallpaper-Native Multi-Agent Orchestrator — split long tasks, dispatch to workers, merge, all on your desktop.**

> *"Don't make one agent do everything. Make many agents do a little, then merge."*

[![Tests](https://img.shields.io/badge/tests-523%20passed-brightgreen)]()
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)]()
[![License](https://img.shields.io/badge/license-MIT-green)]()

---

## 这是什么？

**Maestro** 是一个**把多 Agent 编排器藏进桌面壁纸**的实验性项目：

- 🧠 **拆分-汇总（Map-Reduce）**：长任务拆成子任务、并行/串行派发给不同 worker、合并汇总
- 🧩 **可视化工作流编排**：环节→卡片，点卡片亮/暗启用，⚙ 配置智能体/模型/任务/技能；环节间串行、环节内并行，上一环节产出自动注入下一环节
- 🛡️ **双层安全防线**：guard.py 静态扫描 + sandbox.py 越权审批流（审批卡片实时推送到聊天窗口）
- 💬 **聊天窗口**：Markdown 渲染、打字指示、消息操作（复制/重发/引用）、跨对话上下文关联
- 🪟 **多窗口系统**：合并为多标签大窗口、Tab 拖出还原、历史回灌、窗口状态持久化
- 🖼️ **桌面壁纸模式**（Windows）：透明壁纸层挂 WorkerW（鼠标穿透、不抢焦点）+ PyWebView 主交互窗口
- 🔌 **可插拔 Worker**：embedded（本地 LLM+工具）/ opencode / octo / codebuddy / workbuddy / minimax（图/视频/语音/音乐）
- 🗃️ **SQLite 状态机**：任务/子任务/事件/审批全部落库，断点续跑 + 全程可审计

## ✨ 亮点

| # | 亮点 | 说明 |
|---|---|---|
| 1 | Orchestrator-Workers 状态机 | pending/ready/running/done/failed/retry/cancelled 全落库，人工闸门一等公民 |
| 2 | 工作流卡片编排器 | 环节分色、卡片亮暗启用、技能自动分类（设计/代码/文档/视频/音频/数据/网络） |
| 3 | 环节间数据传递 | 前一环节产出自动注入下一环节（4000 字上限防上下文爆炸） |
| 4 | 双层安全 | guard 静态扫描外部 CLI prompt + sandbox 越权审批（WS 实时推送审批卡片） |
| 5 | 生产化底座 | /metrics（Prometheus）、SQLite 在线备份（24h/保留7份）、限流/熔断/重试分级、快慢线程池 |

## 🚀 快速开始

### 源码运行

```bash
pip install -r requirements.txt
cp .env.example .env   # 填 DEEPSEEK_API_KEY
cd src && python -m maestro.server
# 打开 http://127.0.0.1:8787
```

### 桌面版（Windows）

```bash
pip install pyinstaller pywebview
python packaging/build.py
# 双击 dist/Maestro/Maestro.exe
```

详见 [packaging/README.md](packaging/README.md)。

### Docker

```bash
docker compose up -d    # 0.0.0.0:8787；生产请设 MAESTRO_API_KEY
```

## 🏗️ 架构（5 分钟看懂）

```mermaid
graph LR
    User[用户] -->|输入需求| WebUI[web/index.html]
    WebUI -->|POST /api/tasks| Server[server.py 组装器]
    Server --> Router[api/ 按域路由]
    Router -->|prepare_task| Orchestrator[orchestrator.py]
    Orchestrator -->|split| Split[split.py 场景 A/B/C/auto]
    Orchestrator -->|HITL 闸门 ready| Server
    Server -->|execute_task| Workers{Workers}
    Workers --> Embedded[embedded 本地LLM+工具]
    Workers --> OpenCode / Octo / CodeBuddy / WorkBuddy / MiniMax
    Orchestrator -->|guard.py 扫描| Guard[防线1 静态规则]
    Workers -->|越权命令| Sandbox[sandbox.py 防线2 审批流]
    Sandbox -->|WS 实时推送| WebUI
    Orchestrator -->|merge| Merge[merge.py 汇总+审计]
```

**代码结构**（后端 ~5600 行 + 前端 ~2700 行）：

```
src/maestro/
├── server.py            # FastAPI 组装器（中间件/路由注册/静态挂载）
├── api/                 # 按域路由：tasks / workflows / meta / chat / conversations / ws / deps
├── orchestrator.py      # 状态机（串行/并行/工作流 stage 分组）
├── split.py / merge.py  # 拆分（A/B/C/auto）/ 汇总+审计
├── chat.py / llm.py     # 数字人闲聊（SSE 流式）/ 多 provider LLM 封装
├── skills.py            # 技能库（自动分类 + snippet 注入）
├── guard.py / sandbox.py # 双层安全
├── resilience.py        # 重试/熔断/全局限流
├── db.py                # SQLite 状态层（迁移/备份/连接池）
├── metrics.py           # /metrics 指标采集
├── runtime.py           # 源码/打包路径解析
├── desktop.py           # Windows WorkerW 壁纸层（非 Win stub）
├── launcher.py          # 桌面启动器（PyWebView 双窗口）
└── workers/             # 可插拔 worker（base/embedded/runcmd/filetools/minimax/...）

web/
├── index.html           # 结构标记（143 行）
├── style.css            # 样式（460 行）
└── app.js               # 窗口系统/聊天/工作流编排/仪表盘（2100 行）
```

## 🧪 测试

```bash
pytest tests/ -q                    # 520+ 单元/集成测试
pytest tests/test_e2e_smoke.py -q   # Playwright 浏览器冒烟（需 playwright）
pytest tests/ --cov=src --cov-report=term
```

## 📚 文档

- [packaging/README.md](packaging/README.md) — 打包/桌面版运行说明
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 架构详解
- [docs/QUICKSTART.md](docs/QUICKSTART.md) — 3 步启动
- [.env.example](.env.example) — 全部环境变量说明

## 🛠️ 路线图

- **P1** ✅ Orchestrator + Workers + 拆分/汇总
- **P2** ✅ Web UI + 任务窗口 + 多 worker
- **P3** ✅ 壁纸层 + 桌宠 + 双窗口架构（Windows）
- **P4** ✅ 可视化工作流 + 环节数据传递 + 审批实时卡片
- **P5** ✅ 生产化（安全/性能/可观测/打包）
- **P6** 🚧 工作流运行视图（编排器面板实时状态）、历史消息增量同步、多模型路由

## 📜 许可证

MIT

---

## 🙏 致谢

- **LangGraph** 的 state machine + supervisor 设计哲学
- **WorkBuddy** 沙箱的"默认受限 + 越权审批"模型
- **Cubism** Live2D SDK
- **芙莉莲**——陪我写代码的伙伴
