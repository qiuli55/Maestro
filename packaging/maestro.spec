# -*- mode: python ; coding: utf-8 -*-
"""Maestro 桌面打包 spec（PyInstaller onedir）。

产物：dist/Maestro/Maestro.exe + dist/Maestro/_internal/
  - src/ → _internal/maestro/（Python 包）
  - web/ → _internal/web/（前端三件套 + pet_q / pet_q_v2 / pet_poses / vendor）
  - wallpaper/ → _internal/wallpaper/（壁纸素材，prune 后约 50MB）
  - configs/ → _internal/configs/（内置工作流 + 技能 + 模型元数据）

exclude_binaries=True + COLLECT 用 onedir：单 exe + 同级 _internal 目录，
启动快（无 50MB 临时解压），运行时数据目录（data / outputs / backups）
写在 MAESTRO_HOME（默认 exe 同级）—— 全部可写、可备份、可访问。
"""
from pathlib import Path

block_cipher = None

ROOT = Path.cwd()
# 同 .dockerignore：裁掉开发期残留 + 备份 + 大型源文件（prune_assets.py 也做一次硬过滤）
EXCLUDE_PATTERNS = [
    "**/.bak",
    "**/*.bak",
    "**/__pycache__",
    "**/*.pyc",
    "**/*.pyo",
    "**/.pytest_cache",
    "**/.ruff_cache",
    "**/.mypy_cache",
    "**/outputs/**",
    "**/backups/**",
    "**/.workbuddy/**",
    "**/ocwtest/**",
    "**/generated-images/**",
    "**/_test/**",
    "**/user_char_ref.psd",
    "**/pet_q/_old*",
    "**/*.db",
    "**/*.db-journal",
    "**/*.db-wal",
    "**/*.db-shm",
]


def _datas():
    """要打包进 _internal 的目录清单（用过滤避免带进 .bak 等残留）。"""
    return [
        (str(ROOT / "src" / "maestro"), "maestro"),
        (str(ROOT / "web"), "web"),
        (str(ROOT / "wallpaper"), "wallpaper"),
        (str(ROOT / "configs"), "configs"),
    ]


a = Analysis(
    [str(ROOT / "src" / "maestro" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=_datas(),
    hiddenimports=[
        # fastapi 子模块（server.py 动态 import CORSMiddleware 等）
        "fastapi",
        "fastapi.middleware",
        "fastapi.middleware.cors",
        "fastapi.responses",
        "fastapi.staticfiles",
        # 其他运行时依赖（server.py 顶层 import）
        "dotenv",
        "yaml",
        "openai",
        # uvicorn 本体 + 子模块（launcher --server-mode 里 uvicorn.run 是动态调用）
        "uvicorn",
        "uvicorn.main",
        # uvicorn 子模块（PyInstaller 默认钩子偶有遗漏，显式补齐）
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        # webview 兼容（webview 子包动态加载）
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
        "clr_loader",
        # maestro 内部子模块
        "maestro.api",
        "maestro.api.deps",
        "maestro.api.tasks",
        "maestro.api.workflows",
        "maestro.api.meta",
        "maestro.api.chat",
        "maestro.api.conversations",
        "maestro.api.ws",
        "maestro.workers",
    ],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        "pytest",
        "pytest_asyncio",
        "ruff",
        "mypy",
        "playwright",
    ],
    noarchive=False,
)
# 过滤 .bak 等垃圾（manifest 过滤；上面 EXCLUDE_PATTERNS 用于人工提醒）
a.datas = [d for d in a.datas if not any(Path(d[0]).match(p) for p in EXCLUDE_PATTERNS)]
a.binaries = [b for b in a.binaries if not any(Path(b[1]).match(p) for p in EXCLUDE_PATTERNS)]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Maestro",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,           # 启用 UPX 压缩（系统装了的话自动用）
    upx_exclude=[],     # webview 的 .pyd 不压（PyWebView 文档建议）
    console=False,       # GUI 应用不弹控制台
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "packaging" / "maestro.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Maestro",
)
