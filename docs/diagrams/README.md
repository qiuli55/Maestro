# 架构图索引（docs/diagrams/）

本目录是 Maestro 的可视化架构/流程图，自包含 HTML（双击在浏览器打开）与可直接复制的 Mermaid 源码。

| # | 文件 | 类型 | 内容 |
|---|---|---|---|
| 1 | `01-overview.html` | SVG | 全局架构鸟瞰（5 大泳道 + 跨区连线 + 防线标注）|
| 2 | `02-task-state-machine.md` | Mermaid | 任务状态机（7 态转换 + 取消） |
| 3 | `03-subtask-state-machine.md` | Mermaid | 子任务状态机（含 retry 循环） |
| 4 | `04-scenario-sequence.md` | Mermaid sequence | 场景 A/B/C 数据流时序 |
| 5 | `05-sandbox-defense.md` | Mermaid flowchart | 双层安全防线（真攻击拦截示例） |

`docs/ARCHITECTURE.md` 已合并上述图；GitHub/GitLab/VSCode preview 自动渲染 Mermaid。
