#!/usr/bin/env python3
"""本地打包入口：python packaging/build.py [--onefile] [--skip-prune]

执行：
  1) packaging/prune_assets.py --apply  （裁剪大文件）
  2) pyinstaller --clean --noconfirm packaging/maestro.spec
产物：dist/Maestro/Maestro.exe + dist/Maestro/_internal/

首次运行需：pip install pyinstaller pywebview（pywebview 仅运行时需要；
打包后由 PyInstaller 打进 _internal；Linux 端另装 webkit2gtk-4.0）。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--skip-prune", action="store_true",
                   help="跳过资源裁剪（保留开发期所有素材，打出来会更大）")
    p.add_argument("--clean", action="store_true", default=True,
                   help="打包前清空 dist/ 和 build/")
    args = p.parse_args()

    if args.clean and DIST.exists():
        shutil.rmtree(DIST, ignore_errors=True)
    build = ROOT / "build"
    if build.exists():
        shutil.rmtree(build, ignore_errors=True)

    if not args.skip_prune:
        print("==> Step 1: 裁剪冗余资源")
        subprocess.check_call([sys.executable, str(ROOT / "packaging" / "prune_assets.py"),
                               "--apply"])

    print("==> Step 2: PyInstaller")
    spec = ROOT / "packaging" / "maestro.spec"
    subprocess.check_call(["pyinstaller", "--clean", "--noconfirm", str(spec)])

    # 复制 README 到产物目录（用户首次打开能看说明）
    readme_src = ROOT / "packaging" / "README.md"
    if readme_src.exists():
        shutil.copy(readme_src, DIST / "Maestro" / "README.md")

    # 输出体积报告
    if (DIST / "Maestro").exists():
        total = sum(f.stat().st_size for f in (DIST / "Maestro").rglob("*"))
        print(f"\n产物：{DIST / 'Maestro'}  （{total / 1024 / 1024:.1f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
