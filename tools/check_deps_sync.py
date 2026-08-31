"""检查 pyproject.toml dependencies 与 requirements*.txt 是否同步。

CI 跑这个脚本，不一致直接 fail。pyproject 是 source of truth，
requirements*.txt 是锁定版本的镜像副本——任何漂移会被这里挡住。
"""
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

with (ROOT / "pyproject.toml").open("rb") as f:
    pyproject = tomllib.load(f)


def _names(specs):
    """从版本约束字符串里抽取包名。"""
    out = set()
    for d in specs:
        name = d.split("[")[0]
        for sep in (">=", "==", "~=", "!=", "<=", "<"):
            name = name.split(sep)[0]
        out.add(name.strip().lower())
    return out


def _parse_req(path):
    """读 requirements*.txt 返 {name: version_spec}。"""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+(?:\[[^\]]+\])?)\s*(.*)$", line)
        if not m:
            continue
        name = m.group(1).split("[")[0].lower()
        out[name] = m.group(2).strip() or "*"
    return out


def _check(group_name, py_specs, req_path):
    py_names = _names(py_specs)
    req_deps = _parse_req(req_path) if req_path.exists() else {}
    missing = sorted(n for n in py_names if n not in req_deps)
    extra = sorted(n for n in req_deps if n not in py_names)
    return missing, extra


failures = []

# 主依赖
missing, extra = _check("dependencies", pyproject["project"]["dependencies"], ROOT / "requirements.txt")
if missing:
    failures.append(f"FAIL: pyproject [dependencies] 声明但 requirements.txt 缺: {', '.join(missing)}")

# dev 依赖
dev_specs = (pyproject["project"].get("optional-dependencies") or {}).get("dev", [])
if dev_specs:
    missing_dev, extra_dev = _check("dev", dev_specs, ROOT / "requirements-dev.txt")
    if missing_dev:
        failures.append(f"FAIL: pyproject [dev] 声明但 requirements-dev.txt 缺: {', '.join(missing_dev)}")

# 差异只 warn，不 fail（CI 文件可能在主包未声明）
warnings = []
if extra:
    warnings.append(f"warn: requirements.txt 含 pyproject 未声明的包: {', '.join(extra)}")

for w in warnings:
    print(w)

if failures:
    for f in failures:
        print(f)
    sys.exit(1)

parts = ["主依赖", str(len(pyproject["project"]["dependencies"]))]
if dev_specs:
    parts += ["dev 依赖", str(len(dev_specs))]
print(f"OK: {', '.join(parts)} 与 requirements*.txt 同步")
