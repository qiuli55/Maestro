# Maestro 使用手册

Maestro 是桌面编排器：聊天窗口 + 任务工作流 + 芙莉莲主题，所有逻辑在
**本地运行**（数据/任务都存在你机器上，不上传）。

读完本文 ≈ 10 分钟，照着走完一遍你就能上手。

---

## 1. 快速上手（5 分钟）

### 1.1 安装

```bash
git clone <repo-url> maestro && cd maestro
pip install -r requirements.txt -r requirements-dev.txt   # 开发版
# 生产最小：pip install -r requirements.txt
```

### 1.2 配置 LLM key

复制环境变量模板，至少填一个 LLM provider：

```bash
cp .env.example .env
# 编辑 .env，至少填一个：
# DEEPSEEK_API_KEY=sk-xxx        # 推荐（国内/英文都强）
# KIMI_API_KEY=sk-xxx            # 备选
# MMX_API_KEY=sk-xxx             # 创意任务（图片/视频/音频）
```

### 1.3 启动

```bash
python -m maestro.server
# 浏览器打开 http://127.0.0.1:8787
```

启动成功会看到：
```
maestro starting version='0.2' workers=['codebuddy', 'embedded', 'minimax', ...]
Uvicorn running on http://127.0.0.1:8787
```

### 1.4 第一次对话

1. 页面右下角输入框打“你好”，回车
2. 桌面应出现一个芙莉莲头像的回复气泡
3. 顶部状态卡 "编排器运行中" 表示连接正常

### 1.5 第一次任务

1. 切到 dock 底部标签 `任务`（默认在聊天）
2. 工作流下拉选 `🔍 深度研究`，🧩 编排面板里的卡片可调
3. 输入框打“调研 DeepSeek 与 Kimi 的对比”，回车
4. 窗口里出现一个**任务卡片**：7 段进度条、每段状态实时变化，完成后结果区渲染 Markdown

> 📌 **更多示例看第 4 节**。遇到问题看第 6 节排错。

---

## 2. 界面一览

```
┌──────────────────────────────────────────────┐
│        芙莉莲壁纸 / Live2D 数字人           │  ← 背景层
│                                              │
│   ┌─ 窗口A ─────┐  ┌─ 窗口B ─────┐             │  ← 多窗口
│   │ 消息气泡   │  │ 任务进度卡   │             │
│   └────────────┘  └────────────┘             │
│                                              │
│   ──────────── Dock 输入区 ─────────────      │
│   [聊天|任务] [发送]  [合并]  [历史]           │
└──────────────────────────────────────────────┘
```

### 2.1 窗口

每个对话/任务是独立窗口（拖头改位置、拖右下角改大小、点 ●/□/× 分别最小化/最大化/关闭）。

### 2.2 Dock 输入区

- **聊天/任务** 标签：决定输入是聊天消息还是任务下发
- **工作流** 选择器（仅任务模式）：快速选预设
- **🧩 编排** 按钮（仅任务模式）：打开可视化工作流编辑器
- **发送** / **合并** / **历史**

### 2.3 多窗口合并

开了 ≥2 个窗口后，`合并` 按钮出现——一键把多个对话塞进**一个大窗口**，每个对话一个标签：

- 点击标签 → 切换激活的对话
- **按住标签拖出去** → 那个对话还原成独立小窗口（落在鼠标位置）
- ✕ 关闭大窗口 → 全部安全拆回独立窗口（不丢任何会话）

---

## 3. 聊天（带 Markdown）

### 3.1 基础

- 输入 → Enter 发送；Shift+Enter 换行
- 输入框自动增高，最多 5 行
- 中文友好（标签/列名都中文）

### 3.2 Markdown 渲染

助手消息自动渲染：

| 写 | 渲染 |
|---|---|
| `# 标题` ~ `#### 四级` | 蓝灰色标题 |
| `**粗**` `*斜*` `~~删~~` | 粗/斜/删 |
| `- 列表` `1. 列表` | 项目/编号列表 |
| `> 引用` | 灰色引用块 |
| ``` `行内代码` ``` | 浅蓝底 |
| ```围栏代码``` + 语言 | 深色代码块 + 语言标签 + 复制按钮 |
| `[文字](https://...)` | 蓝色超链接（新窗口打开） |

### 3.3 互动

- 悬停气泡 → 出现操作条：复制 / 重发（用户消息）/ 引用到输入框（助手消息）
- `> 引用` 把消息原文自动塞到输入框开头
- 长回复自动折叠 `展开全文`
- 代码块右上角 `复制代码`
- 回复中显示打字光标（流式 SSE）

### 3.4 数字人长期记忆

芙莉莲会从你的消息里抽取：

- 名字（“我叫小明”）
- 身份（“我是学生”）
- 喜欢/讨厌（“我最喜欢…”、“我讨厌…”）

记忆存 SQLite，**跨会话有效**。查看：`GET /api/chat/profile`，清空：`DELETE /api/chat/profile`。

### 3.5 跨对话上下文关联

点窗口标题栏的 🔗 按钮：

1. 弹出当前打开的其他会话列表
2. 勾选要参考的会话
3. 此后本窗口发消息时，**被勾选会话的最近 6 轮**会作为参考上下文一起发给模型

适用场景：任务窗口引用聊天讨论的关键决定，多窗口协同不丢信息。关联存 localStorage，按会话记忆。

---

## 4. 任务模式与工作流

### 4.1 工作流预设（6 选 1）

| 工作流 | 图标 | 适合场景 |
|---|---|---|
| 标准编排 | 🎼 | 啥都不指定时的默认：识别输入类型后自动路由 a/b/c |
| 深度研究 | 🔍 | LLM 拆步骤 → 并行调研 → 汇总 + 审计 |
| 快速单发 | ⚡ | 不拆分不汇总，单 agent 一步到位 |
| 多智能体流水线 | 🎭 | 段落轮发给多个 agent，汇总时裁决冲突 |
| 深度评审 | 🔬 | 推理模型串行深读，适合代码/方案评审（自动用 deepseek-reasoner） |
| 人工确认流水线 | 🛂 | 拆分后停在待确认页，人工编辑子任务再执行 |

选完预设就能直接点发送；想微调点 **🧩 编排**。

### 4.2 可视化编排器（🧩 编排）

打开后看到一个卡片编辑器：

- **环节**（不同颜色，蓝色/绿色/橙色/紫色/粉色...）：可增删、可改名
- **卡片**（环节内的格子）：每张 = 一个子任务
  - **亮** = 启用（运行时会跑这张卡）
  - **暗** = 停用（透明置灰）→ 点击卡片本身切换
  - **⚙**：配置智能体/模型/任务描述/技能
- **＋ 添加卡片** 在环节内增加任务
- **＋ 添加环节** 在流程末尾增加新环节

#### 4.2.1 卡片 ⚙ 配置

- **智能体**：下拉选本机可用的 worker（embedded/opencode/octo/workbuddy/codebuddy/minimax）
- **模型**：默认使用全局模型，可下拉选其他（DeepSeek/Kimi/Reasoner 等）
- **任务描述**：会与你的主输入合并下发给 worker；支持换行
- **引用上一环节产出**：勾选后，本卡执行前会自动把上一环节所有启用卡片已 DONE 的输出拼起来喂给本卡（4000 字上限）——**多阶段流水线不用手抄**
- **技能**：输入技能名回车即添加；自动按名称归类（设计/代码/文档/视频/音频/数据/图像/网络/通用），分类色作为标签背景

内置技能（18 项）包括：代码生成、代码评审、Python、SQL、Word 文档、Excel、PPT 大纲、报告撰写、翻译、UI 设计、海报设计、Logo 创意、视频剪辑、字幕生成、配音脚本、数据图表、网络调研。

#### 4.2.2 运行

点 ▶ 运行：
- 输入框文字作为主目标
- 启用卡片按环节顺序展开（环节内并发、环节间串行）
- 自动在窗口里生成**任务进度卡片**：7 段进度条 + 子任务实时状态 + 完成时 Markdown 渲染的结果区

### 4.3 自定义工作流

- 点编排面板的 **保存** → 起个名字 → 自动存到 SQLite `user_workflows` 表
- 重新加载：在编排器顶部下拉选 **我的工作流**
- 删除：`DELETE /api/workflows/{id}`

---

## 5. 任务卡片（运行视图）

任务窗口收到 `winAppendTaskCard(id,task_id,...)` 就会显示：

```
┌────────────────────────────┐
│ ▶ 运行中        调研 DeepSeek 与 Kimi 对比 │
│ embedded · deepseek-chat · task_abc123      │
│ ███████░░░░░░  4/8 子任务完成             │
│ [子任务 4/8]                                │
│   ▌ 完成  调研 DeepSeek 文档                  │
│   ▌ 运行  对比 Kimi 长文本能力                │
│   ▌ 失败  截图官网（原因：网络 502）         │
│                                           ▼ 展开全文  │
└────────────────────────────────────────────┘
```

- **进度条** 实时反映子任务完成比例
- **悬停气泡** 可 `复制结果`
- **2 秒轮询**子任务状态（终态自动停）
- 任务完成 / 失败 → 标题加 ⚙ / 标红

---

## 6. 排错

### 6.1 启动失败

- **找不到 `maestro` 模块** → `cd src && python -m maestro.server`
- **端口 8787 占用** → `MAESTRO_PORT=8790 python -m maestro.server`
- **DB 锁定** → 另一个 Maestro 进程在跑，ps 找到杀掉

### 6.2 聊天没回复

- 右上角 ⚠️ “未连接” → 检查后端进程
- 页面 F12 Network → `POST /api/chat/stream` 看错误
- 常见原因：`DEEPSEEK_API_KEY` 未设 / 余额不足 / 网络受限

### 6.3 任务失败

- 点开子任务看 `error` 字段
- `connection timeout/网络/429/5xx` → 自动重试一次（瞬时故障）
- `参数错误/bin 找不到` → 永久失败，不重试——检查 worker 配置
- **审批卡住**（sandbox 越权命令）→ 任务卡片内嵌审批按钮，120 秒内必须批准

### 6.4 工作流不触发某些卡片

- 卡片是否被点亮（暗=停用）→ 点击卡片本身切换
- 卡片描述是否填写（空描述会被跳过）

### 6.5 看不到任何桌宠

这是正常的——桌宠功能在生产级重构中已移除（仍保留在 `feat/bubble-pet` 分支，如需恢复 `git merge`）。

---

## 7. 部署

### 7.1 本地启动

```bash
python -m maestro.server  # 默认 127.0.0.1:8787
```

### 7.2 Docker

`Dockerfile` + `docker-compose.yml` 已就绪：

```bash
docker compose up -d
```

### 7.3 Wallpaper Engine 壁纸

打包好的壁纸在 `dist/Maestro-Frieren.wallpaper`（1.9MB）。Wallpaper Engine Workshop 拖进去即可。后端需在 `127.0.0.1:8787` 运行。

### 7.4 多实例 / API key

设 `MAESTRO_API_KEY=<你的密钥>` 后除白名单（healthz/ready/workers/health/ws/*/静态）外所有 `/api/*` 需要 `X-API-Key` 头；WebSocket 连接 `?key=<MAESTRO_API_KEY>`。

### 7.5 数据备份

服务启动时自动备份 `<db>/backups/maestro_<时间戳>.db`，每 24 小时一次，保留最近 7 份：

```bash
ls -lt maestro.db/backups/ | head
```

手工备份：`python -c "from maestro import db; print(db.backup_database())"`

### 7.6 监控

```bash
curl http://127.0.0.1:8787/metrics                   # Prometheus 文本
curl http://127.0.0.1:8787/metrics?format=json       # JSON
```

关键指标：`maestro_tasks_total` / `maestro_subtasks_total` / `maestro_approvals_pending` / `maestro_ws_connections` / `maestro_pool_*` / `maestro_uptime_seconds`。

### 7.7 性能调优

- `MAESTRO_DB_POOL=1` 启用 SQLite 连接池（thread-local，跨线程独立连接）
- 生产建议 4 线程以内长任务 + 设 `MAESTRO_API_KEY` 启用鉴权
- 监控 `maestro_uptime_seconds` 与 `maestro_ws_connections` 评估负载

---

## 8. 高级话题

### 8.1 自定义 provider

`configs/providers.json` 增加 OpenAI 兼容端点：

```json
{
  "providers": {
    "custom": {
      "base_url": "https://api.example.com/v1",
      "api_key_env": "CUSTOM_API_KEY",
      "model": "gpt-4"
    }
  }
}
```

### 8.2 工作流市场（团队内）

`configs/workflows.json` 是预设 6 条；用户写在 SQLite `user_workflows` 表。要做团队市场：写个端点 `GET /api/workflows?scope=team` 拉共享表，UI 加个团队标签。**当前未实现**。

### 8.3 CI 集成

```bash
# 单测：完全离线
pytest tests/ --ignore=tests/test_integration_smoke.py

# 集成 smoke：设 key 才跑真链路
export DEEPSEEK_API_KEY=...
pytest tests/test_integration_smoke.py
```

### 8.4 API 总览

| 模块 | 端点 | 说明 |
|---|---|---|
| 聊天 | POST `/api/chat`, `/api/chat/stream` | 单条/流式 |
| 聊天 | GET/DELETE `/api/chat/history`, `/api/chat/profile` | 历史/档案 |
| 任务 | GET/POST `/api/tasks`, `/api/tasks/{id}/...` | CRUD/执行/取消/重试 |
| 任务 | POST `/api/tasks/{id}/approvals/{a}` | 审批 |
| 工作流 | GET/POST/DELETE `/api/workflows[/...]` | 预设 + 用户 |
| 技能 | GET/POST/DELETE `/api/skills[/...]` | 内置 + 用户 |
| 会话 | GET/POST/PUT/DELETE `/api/conversations[/...]` | CRUD/导出 |
| 元 | GET `/api/healthz`, `/api/ready`, `/api/workers/health` | 探针 |
| 元 | GET `/api/agents`, `/api/keys`, `/api/models` | 配置 |
| 监控 | GET `/metrics[?format=json]` | Prometheus/JSON |
| WS | WS `/ws[?key=...]` | 订阅 / 推送 |

---

## 9. 反馈

发现 bug 或有功能建议：直接提 issue / PR。当前限制见 `feat/desktop-packaging` 之前的评审文档。
