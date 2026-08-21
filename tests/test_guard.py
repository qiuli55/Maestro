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


# ============================================================================
# 编码混淆绕过测试（mmx 审查指出 4 个漏洞之一）
# ============================================================================

def test_scan_known_limitations_documented():
    """当前规则是字符串匹配，明确已知会漏掉的场景（编码混淆）。

    这些不是 bug 测试，而是文档测试：明确写出"不应被规则拦下"的输入，
    提醒未来重构时必须升级到 AST 解析 / 危险原语白名单。
    """
    # Base64 编码的 rm 命令——当前规则匹配不到（应该是 block 但实际 ok）
    import base64
    encoded = base64.b64encode(b"rm -rf /").decode()
    level, _ = scan(f"echo {encoded} | base64 -d | sh")
    # 当前实现：确实漏掉（应是 'ok' 或 'warn'）；记录以备修复追踪
    assert level in ("ok", "warn"), f"Base64 混淆未拦截（已知）: {level}"

    # 零宽字符嵌入——当前规则匹配不到
    level, _ = scan("rm\u200b -rf /")  # 零宽空格插入
    assert level in ("ok", "warn"), f"零宽字符绕过未拦截（已知）: {level}"


def test_scan_catches_basic_chinese_variants():
    """中文变体应能命中（中文处理是规则级而非 Unicode 大小写）。"""
    # "关机" 中文：应 block
    level, _ = scan("执行关机")
    assert level == "block", f"中文'关机'未拦截: {level}"

    # "删除 C 盘" 中文：应 block
    level, _ = scan("删除 C 盘")
    assert level == "block", f"中文'删除盘符'未拦截: {level}"


def test_scan_warns_network_access():
    """curl/wget 应 warn（正当场景也存在，不应 block）。"""
    level, _ = scan("用 curl 下载这个 URL")
    assert level == "warn", f"网络下载未警示: {level}"

    level, _ = scan("wget https://example.com/file.zip")
    assert level == "warn", f"wget 未警示: {level}"


def test_scan_does_not_block_normal_business_keywords():
    """正常业务关键词（'下载' 出现但不下载）不应误报。"""
    # 包含 "下载" 但实际是"下载文章阅读"
    level, _ = scan("帮我下载文章读给我听")
    # "下载" 触发 warn，但不应 block
    assert level in ("ok", "warn"), f"正常'下载'被误判: {level}"
