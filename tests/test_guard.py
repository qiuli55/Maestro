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
    """v2 guard 升级后能拦下基础编码混淆（零宽字符）。

    早期版本（v1）确实漏判；v2 用 _normalize() 预处理：去零宽、URL 解码、
    bash 变量还原。零宽字符已能拦截；Base64 echo 单纯 echo 不算高危（无害），
    只有 `echo ... | sh` 这种"下载执行"链才算。
    """
    # 零宽字符嵌入 → v2 已拦截（_normalize 去零宽后变为正常命令）
    level, _ = scan("rm\u200b -rf /")  # 零宽空格插入
    assert level == "block", f"零宽字符绕过应被 v2 拦截: {level}"

    # 单纯 echo Base64 字符串：合法场景，不应 block
    import base64
    encoded = base64.b64encode(b"rm -rf /").decode()
    level, _ = scan(f"echo {encoded}")  # 无管道
    assert level in ("ok", "warn"), f"单纯 echo 不应 block: {level}"


def test_scan_normalize_decodes_zero_width():
    """v2 规范化必须去掉零宽字符。"""
    from maestro.guard import _normalize
    with_zwsp = "rm\u200b -rf /"
    assert "\u200b" in with_zwsp
    normalized = _normalize(with_zwsp)
    assert "\u200b" not in normalized
    assert normalized == "rm -rf /"


def test_scan_normalize_decodes_url_encoding():
    """v2 规范化必须 URL 解码。"""
    from maestro.guard import _normalize
    encoded = "rm%20-rf%20/"
    normalized = _normalize(encoded)
    assert normalized == "rm -rf /"


def test_scan_normalize_removes_bash_ifs():
    """v2 规范化必须还原 ${IFS} 为空格。"""
    from maestro.guard import _normalize
    encoded = "rm${IFS}-rf${IFS}/"
    normalized = _normalize(encoded)
    assert normalized == "rm -rf /"


def test_scan_normalize_strips_quotes():
    """v2 规范化必须去引号（避免 'rm' 形式绕过）。"""
    from maestro.guard import _normalize
    encoded = 'echo "rm" "-rf" "/"'
    normalized = _normalize(encoded)
    assert '"' not in normalized


def test_scan_catches_basic_chinese_variants():
    """中文变体应能命中（中文处理是规则级而非 Unicode 大小写）。"""
    # "关机" 中文：应 block
    level, _ = scan("执行关机")
    assert level == "block", f"中文'关机'未拦截: {level}"

    # "删除 C 盘" 中文：应 block
    level, _ = scan("删除 C 盘")
    assert level == "block", f"中文'删除盘符'未拦截: {level}"

    # "格式化 C 盘" 中文（v2 修复）：应 block
    level, _ = scan("格式化 C 盘")
    assert level == "block", f"中文'格式化 C 盘'未拦截: {level}"


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
