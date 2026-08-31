"""内嵌 worker 的受控命令执行：三档分级 + 审批流 + 超时 + 目录限制。

严格安全边界（对称 filetools.safe_read）：
- 白名单只读命令：直接执行（shell=False，列表式 argv，避免命令链注入）
- 写/改/删/安装类命令：进审批队列，用户批准才执行（WorkBuddy 越权审批模型）
- 灾难性命令 / 命令链逃逸（重定向、管道、&、;、` 等）/ 删盘符根目录：直接拒绝
- 超时 kill、cwd 限定在 workdir 内
- **绝不**使用 shell=True：shell 解析下 LLM 可拼接 `&calc`、`^`、`%` 绕过黑名单。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

from .. import sandbox

# 允许的命令（只读/查看类）。不含任何会修改状态、删除、下载执行的命令。
_ALLOWED_COMMANDS = {
    "git",
    "dir",
    "ls",
    "where",
    "type",
    "echo",
    "tasklist",
    "systeminfo",
    "findstr",
    "tree",
    "ver",
    "hostname",
    "whoami",
    "path",
    "set",
}

# cmd.exe 内建命令（Windows 上没有同名 exe，必须经 cmd /c 间接调用）；
# shell=False 模式下 subprocess.run(["cmd.exe","/c",...]) 走 CreateProcessW
# argv 直接传递，shell 解析完全跳过，安全等同。非 Windows 平台无此概念。
if sys.platform == "win32":
    _CMD_BUILTINS = {
        "dir", "type", "echo", "whoami", "ver", "hostname",
        "path", "set", "tree", "findstr",
    }
else:
    _CMD_BUILTINS = set()

# cmd.exe 内建命令的子集：Windows 上 shell=False + 列表式 argv 直接跑会
# FileNotFoundError（没有 dir.exe / type.exe 等独立可执行）。git / where /
# findstr / tasklist / systeminfo 是真实 exe 不在内建集，需要 PATH 探测。
# 非 Windows 平台没有这些概念，分隔符无关。
if sys.platform == "win32":
    _CMD_BUILTINS = {
        "dir", "type", "echo", "whoami", "ver", "hostname",
        "path", "set", "tree", "findstr", "copy", "move", "del",
    }
else:
    _CMD_BUILTINS = set()

# 白名单内 git 允许的子命令（只读）。git clone/push/pull 等会改状态，归审批。
_ALLOWED_GIT_SUB = {
    "status",
    "log",
    "diff",
    "show",
    "branch",
    "remote",
    "rev-parse",
    "tag",
    "shortlog",
    "blame",
    "ls-files",
}

# 审批命令（写/改/删/安装/网络/执行脚本）。命令名精确匹配（去 .exe / ./ 前缀）。
# 命中即进审批队列，用户批准才执行；其余未知命令一律拒绝（默认受限）。
_APPROVAL_COMMANDS = {
    "del",
    "erase",
    "rmdir",
    "rd",
    "rm",
    "copy",
    "xcopy",
    "move",
    "ren",
    "rename",
    "mkdir",
    "md",
    "attrib",
    "icacls",
    "robocopy",
    "replace",
    "tar",
    "pip",
    "pip3",
    "npm",
    "pnpm",
    "yarn",
    "uv",
    "python",
    "node",
    "net",
    "sc",
    "curl",
    "wget",
    "certutil",
}

# git 写操作子命令（审批）。git 整体在白名单里做子命令分级，见 validate()。
_APPROVAL_GIT_SUB = {
    "add",
    "commit",
    "push",
    "pull",
    "clone",
    "merge",
    "rebase",
    "reset",
    "checkout",
    "switch",
    "restore",
    "clean",
    "stash",
    "rm",
    "mv",
    "config",
}

# 灾难性/逃逸标记：命中任一即直接拒绝（无论命令是否在白名单/审批名单）。
# 子串匹配但尽量带空格精确化，避免误伤（如 "reg" 命中 "regedit"、"start" 命中 "restart"）。
_BLOCKED_TOKENS = (
    "format ",
    "shutdown",
    "taskkill",
    "diskpart",
    "bcdedit",
    "schtasks",
    "wmic",
    "msiexec",
    "powershell",
    "cmd /c",
    "reg add",
    "reg delete",
    "reg import",
    "reg save",
    "net user",
    "net localgroup",
    ">",
    ">>",
    "|",
    "&",
    "&&",
    "||",
    ";",
    "`",
    "$(",
)

# 删除类命令指向盘符根目录（C:\、D:/ 后跟分隔符/通配/结尾）——直接拒绝，不给审批机会。
_ROOT_DELETE = re.compile(
    r"\b(?:rm|del|erase|rd|rmdir)\b[^\n]{0,60}[a-zA-Z]:[\\/]\s*(?:[\\/]|\*|$)",
    re.IGNORECASE,
)

# 单条命令输出上限（字符）
MAX_OUTPUT_CHARS = 6000

# 超时（秒）
TIMEOUT = 30


def _command_name(cmd: str) -> str:
    """取命令名：小写、去前导点斜杠（./ 或 .\\）前缀与 .exe 后缀。"""
    tokens = cmd.strip().lower().split()
    if not tokens:
        return ""
    name = tokens[0]
    while name.startswith(("./", ".\\")):
        name = name[2:]
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def validate(cmd: str) -> tuple[str, str | None]:
    """命令分级。返回 (level, reason)：
    - (sandbox.ALLOWED, None)：白名单只读，可直接执行
    - (sandbox.APPROVAL, reason)：需用户批准
    - (sandbox.BLOCKED, reason)：直接拒绝
    """
    low = cmd.lower().strip()
    if not low:
        return sandbox.BLOCKED, "空命令。"

    for tok in _BLOCKED_TOKENS:
        if tok in low:
            return sandbox.BLOCKED, f"命令含灾难性/逃逸操作（{tok.strip()}），已拒绝。"

    if _ROOT_DELETE.search(low):
        return sandbox.BLOCKED, "删除盘符根目录的操作被禁止。"

    name = _command_name(low)
    if not name:
        return sandbox.BLOCKED, "无法识别命令。"

    if name in _ALLOWED_COMMANDS:
        if name == "git":
            tokens = low.split()
            if len(tokens) >= 2:
                sub = tokens[1]
                if sub not in _ALLOWED_GIT_SUB:
                    return sandbox.APPROVAL, f"git 子命令 '{sub}' 会修改仓库状态，需用户批准。"
        return sandbox.ALLOWED, None

    if name in _APPROVAL_COMMANDS:
        return sandbox.APPROVAL, f"命令 '{name}' 会修改状态，需用户批准。"

    return sandbox.BLOCKED, f"命令 '{name}' 不在白名单（只允许查看/只读类命令）。"


def _split_cmd(cmd: str) -> list[str]:
    """把命令字符串拆成 argv 列表（绝不调 shell）。

    Windows 下走 shlex.posix=False（保留引号内的空格，但不展开变量/转义）。
    shlex 不支持 `cmd /c x`，因此对 cmd.exe 单独走 _split_cmd_exe 切。
    """
    import shlex

    s = cmd.strip()
    if not s:
        return []
    # cmd.exe 调内置命令：保留 `cmd /c` 第一个 token，再 token 化后面的命令
    low = s.lower()
    if low.startswith(("cmd /c ", "cmd.exe /c ", "cmd /c:", "cmd.exe /c:")):
        # 强制 shell=False 模式下 cmd /c 仍可执行：把整段 `cmd /c <subcmd>` 作为
        # ["cmd.exe", "/c", "<subcmd>"] 一次性传入，shell 解析被完全跳过。
        parts = shlex.split(s, posix=False)
        # 合并多余空格
        return [p for p in parts if p]
    return [t for t in shlex.split(s, posix=False) if t]


def _ensure_cwd(workdir: str) -> str:
    """规范化 workdir 并限制在传入路径内（防止 ../ 逃逸）。"""
    try:
        wd = os.path.realpath(workdir)
    except OSError:
        wd = workdir
    return wd


def run(cmd: str, workdir: str, task_id: str | None = None, subtask_id: str | None = None) -> str:
    """执行命令（三档分级），返回输出。违规/拒绝/超时/异常返回带标记文本（不抛异常）。

    task_id 由编排器传入（embedded worker 的 tool_executor 上下文）；
    审批命令缺少任务上下文时直接拒绝（安全默认）。
    """
    level, reason = validate(cmd)
    if level == sandbox.BLOCKED:
        return f"[run_command 拒绝] {reason}"
    if level == sandbox.APPROVAL:
        if not task_id:
            return f"[run_command 拒绝] {reason}（命令需人工批准，但缺少任务上下文）"
        decision = sandbox.request_approval(cmd, task_id, subtask_id)
        if decision == "rejected":
            return f"[run_command 拒绝] 用户拒绝执行该命令：{cmd}"
        if decision == "timedout":
            return f"[run_command 审批超时] 命令未获批准，已放弃执行：{cmd}"
        # approved：放行继续执行

    # 安全加固：列表化 argv + shell=False，避免 shell 注入（&calc / | / ^ / % 等）。
    argv = _split_cmd(cmd)
    if not argv:
        return "[run_command 失败] 无法解析命令参数。"

    # Windows 内建命令（dir / type / echo / ...）在 PATH 下无同名 exe——
    # 必须经 cmd.exe /c 间接调用。shell=False 模式下 ["cmd.exe","/c",...argv]
    # 直接走 CreateProcessW argv，shell 解析完全跳过，安全等同。
    name = _command_name(cmd)
    if sys.platform == "win32" and name in _CMD_BUILTINS and argv[0].lower() not in ("cmd", "cmd.exe"):
        argv = ["cmd.exe", "/c", *argv]

    # Windows 内建命令（dir / type / echo / ...）在 PATH 下没有同名 exe，必须经
    # cmd.exe /c 间接调用——shell=False 模式下 ["cmd.exe", "/c", "dir"] 是
    # subprocess 直接传给 CreateProcessW 的 argv，shell 解析完全跳过，安全等同。
    # 非 Windows 平台无此问题。
    name = _command_name(cmd)
    if sys.platform == "win32" and name in _CMD_BUILTINS and argv[0].lower() not in ("cmd", "cmd.exe"):
        argv = ["cmd.exe", "/c", *argv]

    # Windows：补全 .exe 后缀便于 PATH 查找（系统 PATH 下 dir.exe / git.exe / etc.）。
    # shlex 已经按原样保留 .exe 不变，未带的后缀靠 PATH 解析，subprocess.run 不依赖后缀。
    # CREATE_NO_WINDOW：避免在用户桌面弹黑色 cmd 窗口（仅 Windows 生效）。
    creationflags = (
        subprocess.CREATE_NO_WINDOW if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    )

    cwd = _ensure_cwd(workdir)

    try:
        proc = subprocess.run(
            argv,  # 列表形式 → 绝不经过 shell 解析
            shell=False,
            cwd=cwd,
            capture_output=True,
            timeout=TIMEOUT,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        return f"[run_command 超时] 命令超过 {TIMEOUT}s 未完成，已终止。"
    except FileNotFoundError as e:
        return f"[run_command 失败] 命令未找到（{e.filename}）"
    except OSError as e:
        return f"[run_command 失败] {type(e).__name__}: {e}"

    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    err = (proc.stderr or b"").decode("utf-8", errors="replace")
    text = (out + ("\n" + err if err else "")).strip()
    if not text:
        text = f"(无输出，退出码 {proc.returncode})"
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "\n…[已截断，输出过长]"
    return text
