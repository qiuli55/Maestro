#!/usr/bin/env python3
"""打包前资源裁剪：与 .dockerignore 对齐，删除 .bak / _test / 备份等残留。

执行：python packaging/prune_assets.py [--apply]
  --apply：实际删除（默认 dry-run，只打印）
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 要裁的目录/文件 pattern（相对于 ROOT；用 .match 匹配）
PRUNE_PATTERNS = [
    # 开发备份与源 PSD
    "wallpaper/models/source/user_char_ref.psd",
    "wallpaper/models/live2d/avatar/**/.*bak",
    "wallpaper/models/live2d/avatar/**/*.bak",
    # 调试脚本
    "web/pet_q_v2/_test/**",
    "web/pet_q_v2/qchibi.html",
    # 备份与开发者残留
    "web/pet_q/_old*",
    "ocwtest/**",
    "outputs/**",
    "backups/**",
    ".workbuddy/**",
    "generated-images/**",
    "**/__pycache__/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
    "**/.mypy_cache/**",
    "**/*.bak",
    "**/*.pyc",
    "**/*.pyo",
    "**/*.db",
    "**/*.db-journal",
    "**/*.db-wal",
    "**/*.db-shm",
]


def iter_targets():
    for pat in PRUNE_PATTERNS:
        # glob 风格
        for p in ROOT.glob(pat):
            if p.is_file():
                yield p
            elif p.is_dir():
                for child in p.rglob("*"):
                    if child.is_file():
                        yield child


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="实际删除（默认 dry-run）")
    args = p.parse_args()

    targets = sorted(set(iter_targets()))
    total_bytes = sum(t.stat().st_size for t in targets)
    print(f"{'DELETING' if args.apply else 'DRY-RUN'} {len(targets)} files, "
          f"{total_bytes / 1024 / 1024:.1f} MB")
    for t in targets:
        size_kb = t.stat().st_size / 1024
        print(f"  {t.relative_to(ROOT)}  ({size_kb:.1f} KB)")
        if args.apply:
            try:
                t.unlink()
            except OSError as e:
                print(f"    ERR: {e}")
    if not args.apply:
        print("\n传 --apply 真正执行删除。")


if __name__ == "__main__":
    main()
