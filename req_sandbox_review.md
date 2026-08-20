【任务】为 Maestro 项目完善 WorkBuddy 式安全沙箱（方案 B：纯软件审批流 + 外部 CLI 前置防护）。

【背景】F:\Maestro 是多 Agent 编排器（Orchestrator-Workers）。workers 有：embedded（进程内 DeepSeek，工具白名单）、opencode/octo（外部 CLI 黑盒 agent）、minimax。目标：按 WorkBuddy 沙箱模型"默认受限、越权必须用户批准"加固。当前大部分实现已完成，请你 Review 并补全缺口。

【已完成的实现】（供 review，不要重写）：
1. guard.py：外部 CLI 派发前对 prompt 做前置风险检测（block=高危拦截 / warn=中危警示 / ok）
2. sandbox.py：审批中心（命令三档：allowed 直跑 / approval 进审批队列 / blocked 拒绝；SQLite 持久化 + 内存 Event 跨线程唤醒，默认 120s 超时）
3. runcmd.py：embedded 的 run_command 升级为三档分级 + 审批流（只读白名单直跑；del/copy/pip install/git commit 等进审批队列等用户批准；格式化/关机/注册表/重定向/管道直接拒绝）
4. server.py：POST /api/tasks/{id}/approvals/{id} 审批 API（approve/reject）；db.py 新增 approvals 表
5. orchestrator.py：防线 1 = 外部 CLI 派发前 guard.scan 拦截；防线 2 = spawn 带 task_id/subtask_id 归属审批
6. 测试 132 个全过（含 guard/sandbox/审批流新增用例）

【请你输出】（Markdown，逐条给结论 + 关键代码片段）：
1. Review 上述设计的完整性与漏洞：审批流并发安全、超时后行为、guard 正则的绕过方式（给出修补建议）
2. opencode worker 仍带 --auto 自动批准权限：评估非交互 CLI 下去掉 --auto 的可行性（会否挂起等待输入），给出推荐方案
3. minimax worker 的 spawn 签名未适配 task_id/subtask_id 新参数（派发会 TypeError），给出修复代码
4. 外部 CLI（opencode/octo）的受限运行方案：Windows 低权限账户 vs Sandboxie Plus 的成本/收益对比与推荐
5. 未提交改动（工作区约 400 行）提交前应补哪些测试/检查
