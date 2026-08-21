"""外部 CLI prompt 前置检测：静态规则扫描，高危拦截、中危警示。

黑盒 agent（opencode/octo）一旦放行就无法细粒度约束（不经过 runcmd 的
工具白名单/审批流），所以在派发前对 prompt 做静态风险分级，把明显的
灾难性指令挡在门外：
- block：删盘/格式化/关机/下载执行/注册表/提权/密钥外泄/恶意程序 —— 直接拦截子任务
- warn：文件删除/安装/下载/外发请求/读敏感文件 —— 写事件日志警示（不拦截）
- ok：正常

注意：这是规则级防线（快速、确定），不是语义级。复杂/隐晦的恶意意图
仍需用户审查任务结果；审批流只覆盖 embedded worker 的 run_command。
"""
from __future__ import annotations

import re

# (正则, 说明)。命中即 block：危险意图明确，无正当工作场景。
_BLOCK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"format\s+[a-z]:", re.I), "格式化磁盘/盘符"),
    (re.compile(r"格式化(?:磁盘|硬盘|[c-z]盘)", re.I), "格式化磁盘"),
    (re.compile(
        r"\b(?:rm|del|erase|rd|rmdir)\b[^\n]{0,40}[a-z]:[\\/]\s*(?:[\\/]|\*|$)", re.I),
     "删除盘符根目录"),
    (re.compile(r"(?:删除|删掉|清空)\s*[c-zC-Z]\s*盘", re.I), "删除系统盘内容"),
    (re.compile(r"\bshutdown\b", re.I), "关机/重启系统"),
    (re.compile(r"(?:关机|重启电脑|关闭系统)", re.I), "关机/重启系统"),
    (re.compile(
        r"(?:curl|wget|certutil|bitsadmin|mshta)[^\n]{0,80}(?:-o\s|-O\s|-OutFile|/urlcache|download)",
        re.I), "下载文件到本机"),
    (re.compile(r"(?:下载|获取).{0,20}(?:并)?(?:执行|运行|安装)", re.I), "下载并执行"),
    (re.compile(r"reg\s+(?:add|delete|import|save)", re.I), "修改注册表"),
    (re.compile(
        r"(?:bypass\s+uac|getsystem|提权|创建(?:管理员)?账户|net\s+user\s+\S+\s+\S+\s*/add)",
        re.I), "权限提升/创建账户"),
    (re.compile(r"(?:ransomware|勒索|挖矿|miner|sqlmap|metasploit)", re.I), "恶意程序/攻击工具"),
    (re.compile(
        r"(?:api[_-]?key|secret|token|password).{0,30}(?:发送|上传|提交|外传|post到|发到)",
        re.I), "密钥外传"),
    (re.compile(
        r"(?:把|将).{0,20}(?:\.env|密钥|token|api[_-]?key).{0,20}(?:发送|上传|提交)",
        re.I), "密钥外传"),
]

# (正则, 说明)。命中即 warn：正当场景也存在，仅警示不拦截。
_WARN_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:del|erase|rm|rmdir|rd)\b", re.I), "涉及删除文件"),
    (re.compile(r"(?:删除|覆盖|清空)\s*\S*文件", re.I), "涉及删除/覆盖文件"),
    (re.compile(r"(?:pip|npm|pnpm|yarn|uv)\s+install", re.I), "涉及安装软件包"),
    (re.compile(r"(?:curl|wget|git\s+clone|下载)", re.I), "涉及网络下载"),
    (re.compile(r"(?:发送|上传|提交|post|push)\s*(?:到|至)?\s*(?:http|api|服务器|远程)", re.I),
     "涉及外发请求"),
    (re.compile(r"\.env|id_rsa|id_ed25519|credentials", re.I), "涉及读取敏感文件"),
    (re.compile(r"(?:修改|编辑|改)(?:注册表|hosts|系统配置|启动项|环境变量)", re.I), "涉及系统配置修改"),
]


def scan(prompt: str, worker_type: str | None = None) -> tuple[str, str | None]:
    """扫描 prompt。返回 (level, reason)：
    - ("ok", None)：无风险
    - ("warn", reason)：中危，建议写事件日志警示
    - ("block", reason)：高危，应拦截子任务
    """
    if not prompt:
        return "ok", None
    for pat, desc in _BLOCK_PATTERNS:
        if pat.search(prompt):
            return "block", f"检测到高危指令：{desc}。已拦截，请人工审查后单独派发。"
    for pat, desc in _WARN_PATTERNS:
        if pat.search(prompt):
            return "warn", f"检测到中危操作：{desc}。请留意 agent 行为。"
    return "ok", None
