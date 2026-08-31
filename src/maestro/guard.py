"""外部 CLI prompt 前置检测：归一化 + 危险原语匹配 + 中文补充规则。



设计要点：
- 归一化：去引号/去零宽字符/URL decode/bash IFS 还原/反斜杠统一
- 危险原语白名单（精确 token 化）：format/shutdown/reboot/rm-rf/reg*/powershell 等
- 中文补充规则覆盖本地化场景（删除系统盘、重启电脑、密钥外传等）
- 恶意工具名（ransomware/sqlmap/metasploit/矿）独立短路返回 block

历史：v1 用子串匹配漏判率 46%，已替换为 token + 危险原语白名单；
中文场景作补充规则与英文 OR 决策（_CN_BLOCK_PATTERNS）。
"""
from __future__ import annotations

import re

# ============================================================================
# 阶段 1：预处理（解码混淆 → 还原真实意图）
# ============================================================================

def _normalize(prompt: str) -> str:
    """还原混淆：去引号/去零宽字符/URL 解码/bash IFS 还原/反斜杠统一。

    不修改 ASCII 字母/中文/数字；只去掉攻击者常用的混淆字符。
    """
    s = prompt

    # 零宽字符：U+200B（ZWSP）、U+200C（ZWNJ）、U+200D（ZWJ）、U+FEFF（BOM）
    s = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", s)

    # URL 编码 %xx（多次）：注意 urllib.parse.unquote 会把 \x72 也"解码"成 r，
    # 这是它的设计行为（处理 hex escape），不是我们想要的。所以只对 %xx 解码，
    # 其它形式不动。
    for _ in range(3):
        new = re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), s)
        if new == s:
            break
        s = new

    # bash 变量还原：${IFS}、$IFS、\x20 等（注意：\\x20 字面是 \x20，这里 regex
    # 必须 escape \\ 才能匹配真正的 \x20 字节）
    s = re.sub(r"\$\{?IFS\}?", " ", s)
    s = re.sub(r"\\x20", " ", s)  # 字面 "\x20" 4 字符 → 空格
    s = re.sub(r"\\u00[aA]0", " ", s)  # \u00a0 (NBSP) 或 \u0020

    # 引号统一（无论什么引号都去）
    s = re.sub(r"[\"'`´]", "", s)

    # Windows 路径双反斜杠 → 单空格（便于 token 化）。但不动单反斜杠（避免误伤 \x 等）
    s = re.sub(r"\\\\", " ", s)

    # 多空格合一
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ============================================================================
# 阶段 2：危险原语白名单（v2 核心）
# ============================================================================

# 系统盘符（Windows + Unix）：识别"删除/格式化 C 盘"、"rm -rf /" 等
_CRITICAL_PATHS_RE = (
    # Windows 系统盘/关键目录
    r"(?:c|d|e|f|g):",  # 任意盘符：
    r"/(?:bin|boot|dev|etc|home|lib|opt|proc|root|sbin|sys|usr|var)(?:/|\b)",  # Unix 系统目录
    r"windows",
    r"system32",
    r"program files",
    r"appdata",
    r"users",
)
_CRITICAL_PATHS = "|".join(_CRITICAL_PATHS_RE)

# # 命令名危险原语（启动后接特定参数 → block）
# # 格式：(命令起始正则, 描述)
_BLOCKING_PRIMITIVES: list[tuple[re.Pattern, str]] = [
    # === 磁盘格式化（任何空白都匹配）===
    (re.compile(rf"^format\b.*(?:{_CRITICAL_PATHS})", re.I),
     "格式化磁盘（format <drive> 或 format 系统目录）"),

    # === 磁盘擦除/删除关键路径 ===
    # `rm -rf /` / `rm -rf /etc` / `rm -rf C:\Windows` 等
    (re.compile(rf"^rm\b\s+(?:-[rf]+\s+)?(?:--\s+)?{_CRITICAL_PATHS}", re.I),
     "rm 删除关键路径"),
    # 单独的 `rm -rf /`（Unix 根目录）：rm 命令后是 `-rf /`
    (re.compile(r"\brm\b\s+(?:-[rf]+\s+)+/\s*(?:\s|$)", re.I),
     "rm 删除 Unix 根目录"),
    (re.compile(r"\brm\b\s+(?:-[rf]+\s+)+/\s+\S+", re.I),
     "rm 删除根目录下文件"),
    (re.compile(rf"^(?:del|erase|rmdir|rd)\b.*(?:{_CRITICAL_PATHS})", re.I),
     "del 删除关键路径"),
    # "请帮我 rm -rf /"（句首有"请帮我"+rm）
    (re.compile(rf"\brm\b\s+(?:-[rf]+\s+)?(?:--\s+)?{_CRITICAL_PATHS}", re.I),
     "rm 删除关键路径（含前导语）"),

    # === 关机/重启（任意系统） ===
    (re.compile(r"^(?:shutdown|reboot|halt|poweroff|init\s+[06])\b", re.I),
     "关机/重启系统"),
    # "重启服务/电脑/系统" 中文（v1 规则不覆盖的）
    (re.compile(r"(?:系统|电脑)\s*(?:重启|关机|关闭)", re.I),
     "中文'系统重启/关机'"),
    (re.compile(r"(?:重启|关闭)\s*(?:服务|电脑|系统)", re.I),
     "中文'重启服务/电脑/系统'"),

    # === 间接命令执行 ===
    (re.compile(r"^eval\b", re.I),
     "eval 间接执行"),
    (re.compile(r"^exec\b", re.I),
     "exec 间接执行"),
    # python -c / node -e / perl -e 等脚本内执行命令
    (re.compile(r"^(?:python|python3|node|perl|ruby)\s+-[ce]\b", re.I),
     "脚本语言内联代码执行"),

    # === Shell 绕道（powershell / cmd /c / bash -c）===
    (re.compile(r"^powershell(?:\.exe)?\s+(?:-c|-command|-enc|invoke|iex|/c)\b", re.I),
     "PowerShell 执行命令"),
    (re.compile(r"^cmd(?:\.exe)?\s+/c\b", re.I),
     "cmd /c 执行命令"),
    (re.compile(r"^bash\s+-c\b", re.I),
     "bash -c 执行命令"),
    (re.compile(r"^sh\s+-c\b", re.I),
     "sh -c 执行命令"),

    # === 下载并执行（curl/wget/iwr + pipe shell / iex）===
    (re.compile(
        r"^(?:curl|wget)\b.*\|\s*(?:bash|sh|zsh|fish|node|python|cmd|powershell|iex)\b", re.I),
     "下载并执行（curl/wget | bash）"),
    (re.compile(r"^Invoke-WebRequest\b.*\|\s*iex\b", re.I),
     "PowerShell 下载并执行"),

    # === 注册表修改（v1 漏了 regedit /s）===
    (re.compile(r"^regedit\b.*/s\b", re.I),
     "regedit 静默导入注册表"),
    (re.compile(r"^reg\s+(?:add|delete|import|save)\b", re.I),
     "reg 注册表修改"),

    # === 密钥外传（覆盖 key / token / secret 等单独词）===
    # "key 发到 http://evil.com"、"api_key 上传至 http://..."
    (re.compile(
        r"(?:api[_-]?key|secret|token|password|\.env|密钥)\b.{0,40}"
        r"(?:发送|上传|提交|外传|发到|上传至|发至)",
        re.I),
     "密钥外传（key/token/.env）"),
    # "把 X 发到 http://..." / "把 X 上传到 http://..."
    (re.compile(r"(?:把|将)\s*.{0,30}(?:发送|上传|提交|发到|发至)", re.I),
     "中文'把/将+X 发到/上传'"),

    # === 提权/账户创建 ===
    (re.compile(r"^runas\b", re.I),
     "runas 提权"),
    (re.compile(r"^net\s+user\s+\S+\s+\S+\s*/add\b", re.I),
     "net user 创建账户"),
    (re.compile(r"^(?:sudo\s+)?(?:su\s+-?\s*root|chmod\s+[47]\d{3})\b", re.I),
     "Linux 提权"),
]

# 恶意工具名（独立一类，出现即 block）
_MALICIOUS_TOOLS = re.compile(
    r"\b(?:ransomware|cryptolocker|sqlmap|metasploit|msfvenom|hydra|nikto|aircrack|sqlninja)\b",
    re.I,
)


# ============================================================================
# 阶段 3：中文 v1 规则（保留作为补充）
# ============================================================================

_CN_BLOCK_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 格式化（含空格/不带空格都支持）
    (re.compile(r"格式化\s*(?:磁盘|硬盘|系统盘|[c-zA-Z]\s*盘)", re.I),
     "中文格式化"),
    # 删除/删掉/清空 [盘符]盘
    (re.compile(r"(?:删除|删掉|清空)\s*[c-zA-Z]\s*盘", re.I),
     "中文删除盘符"),
    # 关机/重启（中文）
    (re.compile(r"(?:关机|重启电脑|关闭系统)", re.I),
     "中文关机/重启"),
    # 修改注册表/hosts/系统配置
    (re.compile(r"(?:修改|编辑|改)\s*(?:注册表|hosts|系统配置|启动项|环境变量)", re.I),
     "中文修改系统配置"),
]

# 警告级（不动盘符的删除/安装/下载等，正当场景也存在）
_WARN_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 任何删除/擦除/rmdir 命令
    (re.compile(r"\b(?:del|erase|rm|rmdir|rd)\b", re.I),
     "涉及删除文件"),
    # "删除 X 文件" 中文
    (re.compile(r"(?:删除|覆盖|清空)\s*\S*文件", re.I),
     "中文删除文件"),
    # 安装
    (re.compile(r"(?:pip|npm|pnpm|yarn|uv)\s+install\b", re.I),
     "涉及安装软件包"),
    # 网络下载（不含 pipe 到 shell）
    (re.compile(r"\b(?:curl|wget|git\s+clone)\b", re.I),
     "涉及网络下载"),
    # 读敏感文件
    (re.compile(r"\.env|id_rsa|id_ed25519|credentials", re.I),
     "涉及读取敏感文件"),
]


# ============================================================================
# 主入口
# ============================================================================

def _scan_normalized(norm: str) -> tuple[str, str | None]:
    """对规范化后的文本跑所有规则（v2 核心）。

    先 token 化首 token 判断命令类型，再针对具体模式扫描。
    """
    tokens = norm.split()
    if not tokens:
        return ("ok", None)

    tokens[0]

    # 0. 恶意工具名（独立检查）
    if _MALICIOUS_TOOLS.search(norm):
        return ("block", "检测到恶意工具名（ransomware/sqlmap/metasploit 等），已拦截。")

    # 1. 命令起始规则（更精确，针对具体命令+参数）
    for pat, desc in _BLOCKING_PRIMITIVES:
        if pat.search(norm):
            return ("block", f"检测到高危指令：{desc}。已拦截，请人工审查后单独派发。")

    # 2. 中文补充规则（覆盖 v1 中文场景 + v2 漏掉的）
    for pat, desc in _CN_BLOCK_PATTERNS:
        if pat.search(norm):
            return ("block", f"检测到高危指令：{desc}。已拦截，请人工审查后单独派发。")

    # 3. 警告级
    for pat, desc in _WARN_PATTERNS:
        if pat.search(norm):
            return ("warn", f"检测到中危操作：{desc}。请留意 agent 行为。")

    return ("ok", None)


def scan(prompt: str) -> tuple[str, str | None]:
    """扫描 prompt。返回 (level, reason)：
    - ("ok", None)：无风险
    - ("warn", reason)：中危，建议写事件日志警示
    - ("block", reason)：高危，应拦截子任务
    """
    if not prompt:
        return ("ok", None)

    # 规范化：去掉混淆 → 还原真实意图
    normalized = _normalize(prompt)

    # 扫描
    return _scan_normalized(normalized)


# ============================================================================
