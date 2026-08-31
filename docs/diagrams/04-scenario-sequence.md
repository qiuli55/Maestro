# 场景 A/B/C 数据流时序

三个场景共享同一执行骨架：`prepare → execute_task → merge`，区别只在**拆分阶段**和**汇总方式**。

```mermaid
sequenceDiagram
    autonumber
    actor U as 用户
    participant S as Server
    participant O as Orchestrator
    participant SP as Split
    participant W as Worker
    participant L as LLM
    participant DB as SQLite
    participant M as Merge

    rect rgba(91,107,181,0.08)
    note over U,DB: 创建任务
        U->>S: POST /api/tasks<br/>{prompt, scenario, workflow}
        S->>S: apply_workflow_defaults
        S->>O: prepare_task (fast pool)
        O->>SP: split_requirements / split_document / split_plan
        SP->>L: 生成子任务（仅场景 C）
        O->>DB: add_subtasks + create_task
        O->>DB: status = ready (HITL 闸门)
        DB-->>S: task_id
        S-->>U: {task_id, scenario}
    end

    rect rgba(124,93,211,0.08)
    note over U,M: 执行
        U->>S: POST /api/tasks/{id}/execute
        S->>O: execute_task (slow pool)
        O->>DB: status = running
        alt scenario=a (串行)
            loop 每个子任务 (st1, st2, ...)
                O->>W: spawn(st)
                W->>L: 内部 LLM 调用
                L-->>W: output
                W-->>O: result.md
                O->>DB: subtask output + status
                O->>O: 检查 cancel_requested_at
            end
        else scenario=b/c (并行)
            par 并行执行
                O->>W: spawn(s1)
                W->>L: LLM
                L-->>W: output
                W-->>O: result
                O->>DB: subtask done
            and
                O->>W: spawn(s2)
                W-->>O: result
            end
            O->>O: as_completed<br/>+ 取消检查
        end
    end

    rect rgba(14,155,135,0.08)
    note over O,DB: 汇总 + 审计
        alt no_merge=false
            O->>M: merge([s1..sn])
            M->>L: LLM 汇总 + 冲突裁决
            L-->>M: {summary, conflicts, duplicates, gaps}
            M->>DB: set_task_result + status = done
        else no_merge=true
            O->>DB: status = done (各子任务独立)
        end
        O->>DB: set_task_status(done/failed)
        O-->>S: WS 广播 task 状态
    end
```

## 关键差异

| 阶段 | 场景 A | 场景 B | 场景 C |
|---|---|---|---|
| **拆分** | 按换行切（无需 LLM） | 按段落数切（无需 LLM）| LLM 拆步骤（structured output）|
| **并行** | 否（串行，避免互相干扰） | 是（max 8 worker） | 是 |
| **汇总** | 无（no_merge=true 或各段独立） | LLM 汇总 + 冲突/重复/覆盖审计 | 同 B |
| **触发场景** | 多需求清单（"加登录\n加支付") | 长内容（多段文本） | 抽象任务（"实现订单系统"） |

## `auto` 模式

`POST /api/tasks` 的 scenario='auto' 先调 `split.detect_scenario()`（LLM 路由决策），结果 `task_id` 同普通任务。决策失败回退场景 A（`detect_reason` 写事件日志）。
