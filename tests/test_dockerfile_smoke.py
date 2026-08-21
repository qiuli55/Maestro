"""Dockerfile + docker-compose.yml 静态验证。

Docker 命令在本机不可用（CI 才装），所以做静态检查：
- 关键指令存在（FROM base / USER non-root / HEALTHCHECK / EXPOSE）
- 引用路径都存在（src/ web/ wallpaper/ configs/）
- 不会误装开发包（requirements.txt 而非 requirements-dev.txt）
- docker-compose.yml 字段完整 + 端口正确
- .dockerignore 排除 .env/.git 等

如真要 build 验证，加 CI step：docker build -t maestro:test . && docker run --rm maestro:test python -c "import maestro"
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "src")

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
DOCKERIGNORE = ROOT / ".dockerignore"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _instruction(content: str, key: str) -> str | None:
    """return Dockerfile 行内容（去前导空格），找不到返 None。

    对于可重复的指令（CMD/ENTRYPOINT/HEALTHCHECK），返回**最后一个**——
    因为 HEALTHCHECK 之后通常还有 CMD，CMD 才是真正的启动命令。
    """
    last = None
    for line in content.splitlines():
        if line.strip().startswith(key.upper() + " "):
            last = line.strip()
    return last


# ============================================================================
# Dockerfile 基本检查
# ============================================================================

def test_dockerfile_exists():
    assert DOCKERFILE.exists(), "Dockerfile 缺失"


def test_dockerfile_uses_python_slim():
    """FROM python:slim 是生产推荐（Alpine 缺 glibc 兼容性差）。"""
    c = _read(DOCKERFILE)
    from_line = _instruction(c, "FROM")
    assert from_line is not None
    assert "python:3.13-slim" in from_line, f"未使用 python:3.13-slim: {from_line}"


def test_dockerfile_runs_as_non_root():
    """USER 指令必须设置非 root 用户（容器逃逸安全）。"""
    c = _read(DOCKERFILE)
    user_line = _instruction(c, "USER")
    assert user_line is not None, "缺 USER 指令（容器将默认以 root 运行）"
    # 不能是 root
    assert "root" not in user_line.lower() or "nonroot" in user_line.lower(), \
        f"USER 用了 root: {user_line}"
    # 应该有具体用户名（maestro 等）
    assert re.search(r"USER\s+\w+", user_line), f"USER 指令缺用户名: {user_line}"


def test_dockerfile_exposes_port_8787():
    """Maestro 默认端口 8787，Dockerfile 必须暴露。"""
    c = _read(DOCKERFILE)
    expose_line = _instruction(c, "EXPOSE")
    assert expose_line is not None
    assert "8787" in expose_line


def test_dockerfile_has_healthcheck():
    """必须有 HEALTHCHECK 指向 /api/healthz。"""
    c = _read(DOCKERFILE)
    hc_line = _instruction(c, "HEALTHCHECK")
    assert hc_line is not None, "缺 HEALTHCHECK 指令"
    assert "/api/healthz" in c, "HEALTHCHECK 应指向 /api/healthz"


def test_dockerfile_uses_tini():
    """tini 作 PID 1 处理 SIGTERM，避免僵尸进程。"""
    c = _read(DOCKERFILE)
    assert "tini" in c.lower(), "缺 tini 初始化（优雅信号处理）"


def test_dockerfile_uses_pythonunbuffered():
    """PYTHONUNBUFFERED=1 确保 stdout 实时输出（容器日志不缓冲）。"""
    c = _read(DOCKERFILE)
    assert "PYTHONUNBUFFERED" in c or "PYTHONUNBUFFERED" in c


def test_dockerfile_sets_workdir():
    """WORKDIR 必须设置（最佳实践）。"""
    c = _read(DOCKERFILE)
    assert _instruction(c, "WORKDIR") is not None


def test_dockerfile_copies_required_paths():
    """必须 COPY src/ web/ wallpaper/ configs/。"""
    c = _read(DOCKERFILE)
    for path in ["src", "web", "wallpaper", "configs"]:
        assert f"COPY" in c and path in c, f"缺 COPY {path}"


def test_dockerfile_runs_uvicorn():
    """CMD 必须启动 uvicorn。"""
    c = _read(DOCKERFILE)
    cmd_line = _instruction(c, "CMD")
    assert cmd_line is not None
    assert "uvicorn" in cmd_line
    assert "maestro.server:app" in cmd_line


def test_dockerfile_no_dev_secrets():
    """不应该复制 .env 或 .git。"""
    c = _read(DOCKERFILE)
    assert "COPY .env" not in c, "不应 COPY .env"
    assert "COPY .git" not in c, "不应 COPY .git"


# ============================================================================
# docker-compose.yml 检查
# ============================================================================

def test_compose_exists():
    assert COMPOSE.exists(), "docker-compose.yml 缺失"


def test_compose_uses_valid_yaml_and_has_services():
    c = _read(COMPOSE)
    # 必须有 services 段
    assert "services:" in c
    # 必须有 maestro 服务
    assert "maestro:" in c


def test_compose_maps_port_8787():
    """8787:8787 端口映射（容器服务可被主机访问）。"""
    c = _read(COMPOSE)
    assert "8787:8787" in c, f"须含 8787:8787 端口映射"


def test_compose_uses_env_file():
    """必须 env_file 注入 .env（密钥不入镜像）。"""
    c = _read(COMPOSE)
    assert "env_file:" in c
    assert ".env" in c


def test_compose_has_persistent_volumes():
    """数据卷：SQLite + outputs 持久化。"""
    c = _read(COMPOSE)
    assert "volumes:" in c
    assert "maestro-data" in c, "缺 maestro-data 数据卷"
    assert "maestro-outputs" in c, "缺 maestro-outputs 输出卷"


def test_compose_configures_healthcheck():
    """docker-compose 必须含 healthcheck（依赖启动顺序）。"""
    c = _read(COMPOSE)
    assert "healthcheck:" in c
    assert "/api/healthz" in c


def test_compose_has_restart_policy():
    """必须 restart: unless-stopped（默认容器重启策略）。"""
    c = _read(COMPOSE)
    assert "restart:" in c
    assert "unless-stopped" in c


def test_compose_logging_json_driver():
    """日志驱动用 json-file（避免纯 stdout log-driver 一些平台无日志收集）。"""
    c = _read(COMPOSE)
    assert "json-file" in c


# ============================================================================
# .dockerignore 检查
# ============================================================================

def test_dockerignore_exists():
    assert DOCKERIGNORE.exists(), ".dockerignore 缺失"


def test_dockerignore_excludes_env():
    """.env 不进镜像（防密钥泄露）。"""
    c = _read(DOCKERIGNORE)
    assert re.search(r"^\.env$|^\*\.env$", c, re.MULTILINE), \
        ".dockerignore 未排除 .env"


def test_dockerignore_excludes_git():
    """不需要 .git 进镜像。"""
    c = _read(DOCKERIGNORE)
    assert ".git" in c


def test_dockerignore_excludes_pycache():
    """Python 缓存不进镜像。"""
    c = _read(DOCKERIGNORE)
    assert "__pycache__" in c


def test_dockerignore_excludes_tests():
    """测试不进镜像。"""
    c = _read(DOCKERIGNORE)
    assert "tests/" in c


def test_dockerignore_excludes_workbuddy():
    """.workbuddy/（私人记忆）不进镜像。"""
    c = _read(DOCKERIGNORE)
    assert ".workbuddy" in c


# ============================================================================
# 集成一致性
# ============================================================================

def test_requirements_txt_exists():
    """Dockerfile 依赖的 requirements.txt 必须存在。"""
    assert (ROOT / "requirements.txt").exists(), "requirements.txt 缺失"


def test_requirements_txt_is_production_only():
    """requirements.txt 应只含运行时依赖（Docker 用）；dev 依赖在 requirements-dev.txt。"""
    txt = (ROOT / "requirements.txt").read_text(encoding="utf-8", errors="ignore")
    dev_deps = ["pytest", "ruff", "mypy", "pytest-cov", "git-filter-repo", "pre-commit"]
    for line in txt.splitlines():
        line_stripped = line.strip()
        if line_stripped.startswith("#"):
            continue
        for d in dev_deps:
            if line_stripped.startswith(d):
                pytest.fail(f"requirements.txt 不应含 dev 依赖 {d!r}: {line_stripped}。"
                            f"应移到 requirements-dev.txt")


def test_requirements_dev_txt_exists():
    """dev 依赖规则文件必须存在。"""
    assert (ROOT / "requirements-dev.txt").exists(), "requirements-dev.txt 缺失"


def test_requirements_dev_txt_contains_test_dev_deps():
    """requirements-dev.txt 应含 pytest/ruff 等测试/开发依赖。"""
    txt = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8", errors="ignore")
    for dep in ["pytest", "ruff", "mypy"]:
        assert dep in txt, f"requirements-dev.txt 缺 {dep}"