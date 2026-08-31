"""检查 pyproject.toml dependencies 与 requirements.txt 是否同步。

CI 跑这个脚本，不一致直接 fail。pyproject 是 source of truth，
requirements.txt 是锁定版本的镜像副本——任何漂移会被这里挡住。
"""
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 从 pyproject 读 dependencies
with (ROOT / "pyproject.toml").open("rb") as f:
    pyproject = tomllib.load(f)
py_deps = pyproject["project"]["dependencies"]

# 从 requirements 读
req_text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
req_deps = {}
for line in req_text.splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    m = re.match(r"^([A-Za-z0-9_.\-]+(?:\[[^\]]+\])?)\s*(.*)$", line)
    if not m:
        continue
    name = m.group(1).split("[")[0].lower()
    req_deps[name] = m.group(2).strip() or "*"

# 名称集合（去掉版本约束）
py_names = {
    d.split("[")[0].split(">=")[0].split("==")[0].split("~=")[0].split("!=")[0].lower()
    for d in py_deps
}

missing_in_requirements = [n for n in sorted(py_names) if n not in req_deps]

if missing_in_requirements:
    print("FAIL: pyproject 声明但 requirements.txt 缺:")
    for n in missing_in_requirements:
        print(f"  {n}")
    sys.exit(1)

print(f"OK: pyproject 与 requirements 同步（{len(py_names)} deps）")
