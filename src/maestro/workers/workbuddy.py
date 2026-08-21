"""workbuddy worker：WorkBuddy 桌面 CLI（workbuddy-cli）。

重要限制：WorkBuddy 桌面 CLI 只支持 `workbuddy-cli run-task --id <任务>` 触发
**预先定义好的自动化任务**，不能动态传入子任务 prompt。因此本 worker 不消费
prompt，而是触发由 env WORKBUDDY_TASK_ID 指定的任务；子任务 prompt 会落盘到
workdir/workbuddy_task_prompt.md 作为备注，供 WorkBuddy 预定义任务参考。

使用前需两步（用户侧）：
1. WorkBuddy 设置 → 高级 → 启用本地 CLI 调用（沙箱无法替你勾选）
2. 在 WorkBuddy 里建好自动化任务模板，并把其 id 配到 WORKBUDDY_TASK_ID
"""

import os
import pathlib

from .base import SubprocessWorker, register


@register
class WorkBuddyWorker(SubprocessWorker):
    name = "workbuddy"
    bin = os.environ.get("WORKBUDDY_CLI_BIN", "workbuddy-cli")

    def build_command(self, prompt: str, workdir: str) -> list[str]:
        # 备注：把子任务 prompt 落盘，供 WorkBuddy 预定义任务参考
        try:
            pathlib.Path(workdir).joinpath("workbuddy_task_prompt.md").write_text(prompt, encoding="utf-8")
        except OSError:
            pass

        tid = os.environ.get("WORKBUDDY_TASK_ID")
        if not tid:
            # 配置缺失：返回会失败（exit 2）的命令，让子任务 FAILED 而非崩溃整个任务
            return [
                "cmd",
                "/c",
                "echo WORKBUDDY_TASK_ID 未配置：WorkBuddy 桌面 CLI 只能 run-task --id "
                "触发预定义任务，请在 env 或 subtask 配置中指定 & exit 2",
            ]
        return ["cmd", "/c", self.bin, "run-task", "--id", tid]
