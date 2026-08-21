# 借鉴 LangGraph supervisor 的设计

>给 Maestro 的多 Agent 编排器做参考。
>读完 langgraph-supervisor-py（仓库 langchain-ai/langgraph-supervisor-py）的官方 README +
> Reference 文档，把核心模式抽离出来，对比 Maestro 当前实现，给落地建议。

---

## LangGraph supervisor 的 6 个核心模式

### 模式 1 — `create_supervisor` 工厂：StateGraph + LLM Router

```python
# 简化伪代码（来自 langgraph_supervisor.supervisor.create_supervisor）
def create_supervisor(
    agents: list[Pregel],
    *,
    model: LanguageModelLike,        # 决策用 LLM
    prompt: str,                     # "你是 supervisor，下面有 X/Y/Z 三个 worker……"
    output_mode: "last_message" | "full_history",  # ★
    parallel_tool_calls: bool = False,
    supervisor_name: str = "supervisor",
    add_handoff_messages: bool = True,
):
    # 内部：建 StateGraph(messages) → 加 supervisor 节点（LLM router）→
    # 为每个 agent 生成一个 handoff tool → 加 conditional edges 把
    # LLM tool_call 路由到对应 agent → FINISH 终止
```

**核心抽象**：
- **Supervisor 是一个 LLM 节点**，不是写死的 if/else。它用 system prompt 决定"下一个该调用哪个 agent"
- **决策方式 = tool calling**，不是 function calling。LangChain 2025 转向推荐 tool approach（"more control over context engineering"）
- **`output_mode`** 控制 worker 消息如何合并回父 state——只取最后一条（节省 token）或全量（保留中间推理）

**Maestro 对照**：
- ✅ 你有 supervisor（`orchestrator.py`），但决策是写死 if/else（场景 a/b/c/auto）
- ❌ 你**没有**让 LLM 动态决定"该派哪个 worker"——目前 worker 类型是预先分配好的
- ⚠️ 但对你小规模项目来说，写死也合理；详见"落地建议"

### 模式 2 — Handoff Tool：Agent 之间的交接

```python
# 来自 langgraph_supervisor.handoff.create_handoff_tool
def create_handoff_tool(*, agent_name: str):
    @tool
    def handoff_to_agent(state, tool_call_id):  # 实际是 ToolMessage
        # 把 agent_name 注入到 tool_call 里 → 路由边根据它跳到对应节点
        tool_message = create_message(
            AIMessage, ..., tool_calls=[{
                "name": "transfer_to_<agent_name>",
                "id": tool_call_id,
                "args": {"state": state},  # 上下文注入
            }]
        )
    return handoff_to_agent
```

**核心抽象**：
- Handoff 本身就是一个 tool——LLM 调 `transfer_to_researcher`，StateGraph 路由到 researcher 节点
- Handoff tool **能注入上下文**（state 参数）——worker 拿到的不只是任务，还有全部对话历史
- **add_handoff_back_messages**: 是否在 worker 完成后自动加一条"agent 移交回 supervisor"的消息（默认 True，让消息流双向）

**Maestro 对照**：
- ✅ 你有 subtask dispatch，但**没有"worker → supervisor 反馈"**机制——worker 失败只写 DB，supervisor 不知情
- ❌ 缺 worker 自助"我不知道，让 supervisor 来" 的能力

### 模式 3 — Handoff Back + 双工消息流

```python
# worker 完成后，回 supervisor 的边：
builder.add_edge("researcher", "supervisor")  # 回到 supervisor 重新决策
# add_handoff_back_messages=True 时，自动在消息流里加一条 ToolMessage
# "Task <tool_id>> handed off back to supervisor"，LLM 据此更新状态
```

**核心抽象**：
- Worker 完成后**永远回到 supervisor**（不是 exit）——supervisor 看 worker 结果再决定下一步
- 消息流的"上下文工程"是关键——通过 message metadata 让 LLM 知道"刚才是 worker 在跑"

**Maestro 对照**：
- ⚠️ 你目前是**单层 dispatch**——supervisor 派完就合并（merge），不会"看完 worker 结果再决定"
- 适用场景简单（A/B/C），所以够用；如果未来要做"批评修订"型（worker 写完 → supervisor 评 → 改 → 再评），这个模式就是入口

### 模式 4 — Multi-level Hierarchy（团队嵌套）

```python
research_team = create_supervisor(
    [research_agent, math_agent], model, ...
).compile(name="research_team")          # ★ 编译后当作一个 agent

writing_team = create_supervisor(
    [writing_agent, publishing_agent], model, ...
).compile(name="writing_team")

top_supervisor = create_supervisor(
    [research_team, writing_team],       # 嵌套 teams
    model, ...
).compile(name="top_supervisor")
```

**核心抽象**：
- 关键技巧：**`create_supervisor` 返回的是 StateGraph，`compile()` 后是 Pregel 对象**
- Pregel 对象**可以**作为另一个 `create_supervisor` 的 agent 参数
- 这让"supervisor 管 supervisor 管 agent"这种 N 层组织成可能

**Maestro 对照**：
- ⚠️ 你目前 worker 是平铺（opencode/octo/embedded/codebuddy/workbuddy/minimax）
- 未来若做"法律/代码/分析" 三类任务，每个类再分子任务 → 可以做两层级：top_supervisor → {legal_team, code_team} → 各自的 agent
- 但**两层就够了**——再深就要做"消息路由压缩"，成本剧增

### 模式 5 — Pre/Post Model Hook（拦截点）

```python
workflow = create_supervisor(
    agents,
    model,
    pre_model_hook=trim_history,           # LLM 调用前：截断 messages
    post_model_hook=human_review_guardrail,  # LLM 调用后：人工审查
)
```

**核心抽象**：
- **Pre-model hook** = LLM 前置处理：截断长消息、做 RAG 注入、加 system 上下文
- **Post-model hook** = LLM 后置处理：人工审查（interrupt）、输出校验、guardrail
- 都是 callable 接 state 返回 state update

**Maestro 对照**：
- ✅ 你有 **`guard.scan()`**（外部 CLI 前置检测）——但只对 SubprocessWorker 触发
- ✅ 你有 **`sandbox.request_approval()`**（embedded 的 run_command 后置审批）——已经实现了 post-hook 雏形
- ❌ 缺 **`pre_model_hook`**：LLM 调用前没做"长消息截断 / RAG 注入"
- ⚠️ 你的"闸门"只在 **拆分后**（任务级 HITL），不在 **LLM 调用后**（决策级 HITL）

### 模式 6 — Checkpoint & Memory

```python
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

app = workflow.compile(
    checkpointer=InMemorySaver(),   # 短期：当前对话
    store=InMemoryStore(),          # 长期：跨对话
)
# 然后 app.invoke(input, config={"configurable": {"thread_id": "user-123"}})
```

**核心抽象**：
- **Checkpointer** = 每次节点运行后**全 state 快照**到 KV 存储，断点续跑 = 重放快照
- **Store** = 跨 thread 的长期记忆（语义搜索）
- `thread_id` 是隔离的最小单元——每个用户/任务/会话一个 thread
- 这套机制让你能 **`interrupt_before: ["supervisor"]`** 实现人工介入

**Maestro 对照**：
- ✅ 你有 SQLite 状态机：tasks / subtasks / task_events / approvals
- ⚠️ 缺 **checkpoint 抽象**——任务中断恢复是手动 `resume_incomplete`，没自动 snapshot 节点间 state
- ✅ 你有 "thread" 等价物（`conv_id` / `task_id` 隔离）
- ❌ 缺 **长期 store**（跨任务记忆）——你只有数字人闲聊的 `user_profile`，没有"agent 经验库"

---

## 落地建议（按"性价比"排序）

### 高价值、低风险（先做）

#### A. **`output_mode` 引入到合并阶段**（30 行代码）
你现在 `merge.merge()` 直接把所有 subtask output 喂给 LLM 汇总。
- 改：增加 `mode="last_message" | "full_history"`，默认 last_message 节省 token
- 理由：worker 可能产生很长的工具调用历史，全喂给汇总 LLM 浪费

#### B. **`post_model_hook` 守门人**（100 行代码）
在 supervisor LLM 决策**之后**做一次"任务派发合理性"检查：
- 例：派了一个 `embedded` worker 去读 `/etc/passwd` → 拦截
- 例：派了一个 `opencode` worker 但 8 个 worker 已经在跑 → 限流
- 落地：把现在的 `guard.scan()` 升级，从"prompt 静态扫"变成"决策上下文扫描"

#### C. **Pre-model hook: 长消息截断**（50 行代码）
LLM 调用前检查 `messages` 总长度，超 N tokens 则丢弃最早 K 条（保留 system + 最新 R 条）
- 落地：在 `llm.complete_with_tools()` 入口加 hook
- 解决：长任务跑 1 小时后 LLM context 爆炸

### 中价值、中风险（半年内做）

#### D. **嵌套 supervisor**（200+ 行代码）
把 worker 分组成"team"：
- `research_team` = [search_agent, fetch_agent]
- `code_team` = [codegen_agent, test_agent]
- `top_supervisor` 调度 teams
- 你的实现：worker 注册时支持 `worker.group` 字段

#### E. **Checkpoint 抽象**（300+ 行代码）
每次 subtask 状态变更写一个 `task_checkpoints(thread_id, snapshot)` 表
- 断点恢复：`resume_incomplete` 改为"加载最新 snapshot 重放"
- 比纯 SQLite 状态机更细粒度（节点级 vs 任务级）

#### F. **跨任务 Store**（200+ 行代码）
`store` 表存 `key -> embedding -> value`：
- agent 学到"这个用户不爱回答技术问题" → 存 store
- 下次任何 agent 看到这条 user → 注入到 system prompt
- 比你现在的 `user_profile` 更通用（不只数字人，所有 agent 都能用）

### 低价值、高风险（暂不做）

#### G. **完整 tool-calling 决策**
让 LLM 动态决定派哪个 worker
- 风险：LLM 容易"循环派同一 worker"或"忘记已完成的 worker"
- 你的场景：worker 类型不多（6 个），写死够用
- **结论**：等 worker 数量 > 10 或场景变得很复杂再做

#### H. **Post-hook interrupt（决策级 HITL）**
LLM 决策后先暂停等用户批准再执行
- 风险：每次派任务都要用户点批准，UX 疲劳
- 你的"任务级闸门"（拆分后确认一次）已够用
- **结论**：保留任务级 HITL，不引入决策级 HITL

---

## 关键设计原则提炼

> **从 LangGraph supervisor 学到的最重要一课**：
>
> **"Multi-agent 不需要复杂的图引擎；核心是 3 个抽象**：
> 1. **Agent = 节点**（worker 函数 + 状态）
> 2. **Supervisor = LLM router**（用 prompt 描述谁能干什么，让 LLM 选）
> 3. **State = messages 列表 + 自定义字段**
>
> 你的 Maestro 已经有 1 和 3，缺的是 2 的优雅实现。**但你不一定需要 LLM router**——小项目硬编码 + 显式 trigger（场景 a/b/c）已经够用。"

---

## 落地路径建议

按上面 A → C → E → F 顺序，**先做 A 和 C**（各自 30-50 行）：

| 优先级 | 项目 | 行数 | 风险 | 价值 |
|---|---|---|---|---|
| 1 | output_mode 引入 merge | 30 | 低 | 中 |
| 2 | pre-model hook 长消息截断 | 50 | 低 | 高（防 context 爆炸）|
| 3 | post-model hook 守门人 | 100 | 中 | 中 |
| 4 | checkpoint 抽象 | 300+ | 中 | 中 |
| 5 | 跨任务 Store | 200+ | 中 | 中 |

---

## 参考资料

- 仓库：`https://github.com/langchain-ai/langgraph-supervisor-py`
- 官方文档：`https://reference.langchain.com/python/langgraph-supervisor/supervisor/create_supervisor`
- LangChain 多 Agent 指南（推荐 tool-calling 模式）：`https://docs.langchain.com/oss/python/langgraph/multi-agent`

---

## 更新历史

- 2026-08-21：初版（基于 README + Reference 文档提取）