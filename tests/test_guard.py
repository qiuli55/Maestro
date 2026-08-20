"""外部 CLI prompt 前置检测测试：静态规则扫描三档分级。"""
import sys

sys.path.insert(0, "src")

from maestro.guard import scan


def test_scan_ok_for_normal():
    assert scan("帮我写一个登录功能，用 Python 实现")[0] == "ok"


def test_scan_blocks_catastrophic():
    level, reason = scan("格式化 C 盘，然后关闭系统")
    assert level == "block"
    assert "高危" in reason


def test_scan_blocks_rm_root():
    assert scan("del C:\\*.*")[0] == "block"
    assert scan("rm -rf C:\\")[0] in ("block", "warn")


def test_scan_warns_install():
    level, reason = scan("运行 pip install 安装依赖")
    assert level in ("warn", "block")


def test_scan_ok_empty():
    assert scan("") == ("ok", None)
