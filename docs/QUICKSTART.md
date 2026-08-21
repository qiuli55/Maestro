# Quickstart

> 5 分钟从 0 到跑起来。

---

## 前置要求

| 工具 | 版本 | 用途 |
|---|---|---|
| Python | 3.13+ | 编排器后端 |
| Node.js | 18+ | MiniMax worker 调用 mmx CLI |
| Git | 任意 | 克隆仓库 |
| Windows / macOS / Linux | — | 已测试 Windows 11 / macOS 14 |

---

## 1. 克隆与装依赖（60 秒）

```bash
git clone https://github.com/qiuli55/Maestro.git
cd Maestro

# 推荐：venv 隔离
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

---

## 2. 配环境变量（30 秒）

```bash
cp .env.example .env
# 编辑 .env，填至少一个 LLM key：
```

最小 `.env`：

```bash
# 主用 LLM（拆分/汇总/闲聊）
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxx

# MiniMax worker（可选，用于图像/视频/语音）
MMX_API_KEY=sk-cp-xxxxxxxxxxxx

# 编码 agent（可选，用于重型代码任务）
OPENCODE_BIN=E:\tools\opencode\node_modules\opencode-ai\bin\opencode.exe
```

**没有 key 也能跑**：会跳过对应 worker，embedded 走默认 DeepSeek。

---

## 3. 启动（10 秒）

```bash
cd src
python -m maestro.server
```

输出类似：

```
Maestro Web 启动于 http://127.0.0.1:8787
INFO:     Uvicorn running on http://127.0.0.1:8787 (Press CTRL+C to quit)
```

浏览器打开 **http://127.0.0.1:8787**，看到芙莉莲壁纸 + 底部输入框。

---

## 4. 第一个任务（30 秒）

### 聊天模式
1. 切到 `聊天` tab
2. 输入 `你好` → 回车
3. 看到芙莉莲气泡回复（流式打字机效果）

### 任务模式
1. 切到 `任务` tab
2. 输入 `echo hello world`
3. 看到 status: ready（拆分完成）→ 点击 ⚙ 确认
4. 看到 status: running → done → 顶部显示结果

### 派多任务（场景 A）
```
加登录功能
加支付功能
加报表功能
```
三行 → 自动拆 3 子任务 → 串行执行 → 汇总报告。

---

## 5. 跑测试（验证安装正确）

```bash
pytest tests/ -q
```

期望：`132 passed`（约 15 秒）。

---

## 6. 常见问题

### Q: `ModuleNotFoundError: No module named 'maestro'`
**A**: 当前在 `Maestro/src/` 下，Python 在 `Maestro/`` 找不到。改用：
```bash
cd src && python -m maestro.server
```
或装为可编辑包：
```bash
pip install -e .
```

### Q: `DEEPSEEK_API_KEY not set` 警告
**A**: 编辑 `.env` 设置，或临时用环境变量：
```bash
export DEEPSEEK_API_KEY=sk-xxx  # Linux/macOS
set DEEPSEEK_API_KEY=sk-xxx     # Windows CMD
$env:DEEPSEEK_API_KEY="sk-xxx"  # Windows PowerShell
```

### Q: 浏览器打开 127.0.0.1:8787 空白
**A**: 检查服务是否在跑（终端日志）；检查防火墙；换 `localhost:8787`。

### Q: 桌宠没显示
**A**: 桌宠是 P3 阶段，进度未完成；当前只显示首页壁纸 + 输入框。

### Q: 想看任务执行细节
**A**: 浏览器开 `http://127.0.0.1:8787/api/tasks` 看 JSON 列表，或 WebSocket `/ws/task/{id}` 看实时进度。

---

## 下一步

- 📖 读 [ARCHITECTURE.md](ARCHITECTURE.md) 理解模块依赖与状态机
- 🛡️ 读 [PRE_PUSH_CHECKLIST.md](PRE_PUSH_CHECKLIST.md) 准备提交
- 🧪 读 [LANGGRAPH_BORROW.md](LANGGRAPH_BORROW.md) 了解与 LangGraph 的对比与借鉴
- 💬 在 [Issues](https://github.com/qiuli55/Maestro/issues) 反馈问题