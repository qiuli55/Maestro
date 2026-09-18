"""子进程文件沙箱：用 bubblewrap 把 agent 可达的子进程关进专属文件夹。

启用条件：环境变量 MAESTRO_JAIL_ROOT 指向专属文件夹（如 /opt/maestro-workspace），
且系统装了 bwrap（bubblewrap）。二者缺一 → 原样返回命令（Windows 开发机零影响）。

沙箱内可见：系统目录只读（/usr 等）+ 专属文件夹可读写 + 最小 /etc 白名单；
服务器其余路径（/root、/home、/var/www、/opt/Maestro、密钥文件）在沙箱内
不存在——任何命令、任何参数都访问不到（内核 mount namespace 保证）。
同时 --clearenv 清空环境变量，防止 agent 读到服务进程的 API key。

接入点（agent 可达的子进程必须全部经此收口；新逻辑集中在本文件，老代码只加
一行调用）：
- workers/runcmd._execute（内嵌 run_command 工具）
- workers/base.SubprocessWorker.spawn（外部 CLI worker）
- workers/minimax._run_mmx
"""

from __future__ import annotations

import os
import shutil

from . import observability

log = observability.get_logger(__name__)

# 沙箱内只读挂载的 /etc 路径（联网/DNS/CA/用户解析所需最小集合；
# 刻意不含 /etc/shadow、/etc/sudoers 等敏感文件）
_RO_ETC = (
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/alternatives",
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
    "/etc/ld.so.cache",
    "/etc/localtime",
)


def root() -> str | None:
    """专属文件夹路径；未配置返回 None（= 不启用沙箱）。"""
    return os.environ.get("MAESTRO_JAIL_ROOT") or None


def wrap(argv: list[str], workdir: str | None = None) -> list[str]:
    """把命令 argv 包成 bwrap 沙箱调用；未启用/工具缺失则原样返回。

    workdir 用作沙箱内起始目录（必须在专属文件夹内，越界则退回文件夹根）。
    """
    jail = root()
    if not jail:
        return argv
    if not os.path.isdir(jail):
        log.warning("沙箱文件夹不存在，跳过隔离: %s", jail)
        return argv
    bwrap = shutil.which("bwrap")
    if not bwrap:
        log.warning("bwrap 未安装，跳过隔离（安装: apt-get install -y bubblewrap）")
        return argv
    return [bwrap, *_bwrap_args(jail, workdir), "--", *argv]


def _bwrap_args(jail: str, workdir: str | None) -> list[str]:
    """构造 bwrap 参数：系统只读 + 专属文件夹可写 + 最小 /etc + 清空环境变量。"""
    args = [
        "--die-with-parent",   # Maestro 退出时沙箱子进程一并回收
        "--unshare-pid",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",     # 私有临时目录（HOME 也指向这里）
        "--clearenv",          # 防 agent 读取服务进程环境变量中的 API key
        "--setenv", "PATH", "/usr/bin:/usr/sbin:/bin:/sbin",
        "--setenv", "HOME", "/tmp",
        "--setenv", "LANG", "C.UTF-8",
    ]
    args += _usr_binds()
    for p in _RO_ETC:
        if os.path.exists(p):
            args += ["--ro-bind", p, p]
    args += ["--bind", jail, jail]
    args += ["--chdir", _workdir_in(jail, workdir)]
    return args


def _usr_binds() -> list[str]:
    """挂载系统程序目录；usrmerge 系统上 /bin 等是符号链接，在沙箱内重建链接。"""
    args = ["--ro-bind", "/usr", "/usr"]
    for name in ("/bin", "/sbin", "/lib", "/lib64"):
        if os.path.islink(name):
            args += ["--symlink", os.path.realpath(name).lstrip("/"), name]
        elif os.path.isdir(name):
            args += ["--ro-bind", name, name]
    return args


def _workdir_in(jail: str, workdir: str | None) -> str:
    """命令起始目录：必须在专属文件夹内（防御式兜底：越界则退回文件夹根）。"""
    if not workdir:
        return jail
    real = os.path.realpath(workdir)
    if real == jail or real.startswith(jail + os.sep):
        return real
    log.warning("workdir 不在沙箱内，退回文件夹根: %s", workdir)
    return jail
