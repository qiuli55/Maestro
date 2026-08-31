# 架构图索引（docs/diagrams/）

本目录是 Maestro 的可视化架构/流程图，自包含 HTML（双击在浏览器打开）与可直接复制的 Mermaid 源码。

| # | 文件 | 类型 | 内容 |
|---|---|---|---|
| 1 | `01-overview.html` | SVG | 全局架构鸟瞰（5 大泳道 + 跨区连线 + 防线标注）|
| 2 | `02-task-state-machine.md` | Mermaid | 任务状态机（7 态转换 + 取消） |
| 3 | `03-subtask-state-machine.md` | Mermaid | 子任务状态机（含 retry 循环） |
| 4 | `04-scenario-sequence.md` | Mermaid sequence | 场景 A/B/C 数据流时序 |
| 5 | `05-sandbox-defense.md` | Mermaid flowchart | 双层安全防线（真攻击拦截示例） |

## archify 产品级版本（自包含交互 HTML）

| 文件 | 类型 | 说明 |
|---|---|---|
| `01b-overview-archify.html` | archify architecture | 全局架构（深浅主题/搜索/PNG·SVG·WebM 导出） |
| `01b-overview-archify.json` | JSON spec | 上图的输入规格（validate 9/9） |
| `06-task-lifecycle-archify.html` | archify workflow | 任务全生命周期（6 泳道 × 6 列 + 防线/审批/失败分支，trace 动画） |
| `06-task-lifecycle.workflow.json` | JSON spec | 上图的输入规格（showcase 0 errors 0 warnings） |

重新渲染：`node archify/bin/archify.mjs deliver <type> <spec.json> <out.html>`
（archify 安装于 `~/.zcode/cli/plugins/marketplaces/archify/`）

`docs/ARCHITECTURE.md` 已合并上述图；GitHub/GitLab/VSCode preview 自动渲染 Mermaid。
