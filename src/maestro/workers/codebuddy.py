"""codebuddy worker：腾讯云 CodeBuddy Code CLI，headless 非交互。

用 `codebuddy -p "prompt"` 非交互打印结果（类 Claude Code 的 -p print 模式）。
bin 指向 npm 全局装的 codebuddy.cmd；经 `cmd /c` 包装，因为 Windows 下
subprocess（不带 shell）无法直接 exec .cmd。
登录态在用户本地（首次需 `codebuddy` 唤起微信扫码），不依赖额外环境变量；
Maestro 子任务产出由编排层落盘 res.output，本 worker 只返回文本，不写本地文件。
"""

import os
import shutil

from .base import SubprocessWorker, register

# 本机 node 是 WorkBuddy 托管版，默认不在系统 PATH 上；codebuddy.cmd 内部靠 `node`
# 启动，所以 spawn 环境里必须能找到 node。这里把 node 目录显式并入 PATH，作为
# 环境层面的兜底（用户已手动把 node 加进 User PATH，但 Maestro 若以不继承该 PATH
# 的方式启动，仍要靠这里兜底）。
_NODE_CANDIDATES = [
    os.environ.get("CODEBUDDY_NODE_DIR", ""),
    r"C:\Program Files\nodejs",
    r"C:\Program Files (x86)\nodejs",
]


def _node_dir() -> str | None:
    # 1) 环境变量优先（用户可覆盖）
    env_dir = os.environ.get("CODEBUDDY_NODE_DIR")
    if env_dir and os.path.isfile(os.path.join(env_dir, "node.exe")):
        return env_dir
    # 2) 已知托管 node 候选
    for c in _NODE_CANDIDATES:
        if os.path.isfile(os.path.join(c, "node.exe")):
            return c
    # 3) 最后信任 PATH 里已有 node
    if shutil.which("node"):
        return None
    return None


@register
class CodeBuddyWorker(SubprocessWorker):
    name = "codebuddy"
    bin = os.environ.get("CODEBUDDY_BIN", r"E:\tools\codebuddy\codebuddy.cmd")
    # 经管道输出为 UTF-8（base.py 还会兜底 gbk），无需特殊编码
    encoding = "utf-8"

    def build_command(self, prompt: str, workdir: str) -> list[str]:
        # cmd /c 包装 .cmd；-p 非交互打印；-y 跳过权限确认（避免子任务卡交互直到超时）
        return ["cmd", "/c", self.bin, "-p", prompt, "-y"]

    def extra_env(self) -> dict:
        nd = _node_dir()
        if not nd:
            return {}
        path = os.environ.get("PATH", "")
        if nd in path.split(os.pathsep):
            return {}
        return {"PATH": nd + os.pathsep + path}
