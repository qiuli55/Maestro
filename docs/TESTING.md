# 测试

> Maestro 的测试结构、写新测试模板、覆盖率策略。

---

## 一、跑测试

```bash
# 基本跑（132+ 测试）
pytest tests/ -q

# 覆盖率（总体 60% 门槛，核心 80%+）
pytest tests/ --cov=src --cov-report=term-missing

# HTML 报告（生成 htmlcov/）
pytest tests/ --cov=src --cov-report=html

# 单文件
pytest tests/test_orchestrator_edges.py -v

# 关键字
pytest -k sandbox -v
```

---

## 二、测试结构

```
tests/
├── conftest.py              # 共用 fixture（FakeWorker / FlakyWorker / BoomWorker / tmp_db / outputs 隔离）
├── test_chat.py             # 数字人闲聊
├── test_cli.py              # CLI 入口
├── test_db.py               # SQLite 状态层
├── test_embedded.py         # 内嵌 worker（DeepSeek）
├── test_embedded_edges.py   # embedded 边界（write_file 校验 / image 失败）
├── test_filetools.py        # 安全读文件
├── test_guard.py            # 外部 CLI prompt 静态扫描
├── test_injection_guard.py  # prompt 注入防护
├── test_merge.py            # 汇总合并
├── test_orchestrator.py     # 编排器主路径
├── test_orchestrator_edges.py # 编排器边界（取消 / retry / resume）
├── test_providers.py        # LLM provider
├── test_runcmd.py           # runcmd 三档分级 + 审批流
├── test_sandbox.py          # 沙箱审批中心
├── test_server.py           # FastAPI 端点
├── test_split.py            # 拆分（A/B 场景）
├── test_split_plan.py       # 拆分（场景 C：AI 智能拆分）
└── fixtures/                # 静态测试资源
    ├── b_checklist.md
    ├── b_docs/conflict_review.txt
    └── ...
```

---

## 三、覆盖目标

| 模块 | 目标 | 现状 |
|---|---|---|
| `guard.py` | 100% | ✅ 100% |
| `merge.py` | 100% | ✅ 100% |
| `split.py` | ≥ 85% | ✅ 82%（含 detect_scenario 边界）|
| `chat.py` | ≥ 90% | ✅ 96% |
| `db.py` | ≥ 90% | ✅ 95% |
| `filetools.py` | ≥ 90% | ✅ 90% |
| `runcmd.py` | ≥ 80% | ✅ 77% |
| `orchestrator.py` | ≥ 80% | ⚠️ 73%（cancel 路径还有缺口）|
| `server.py` | ≥ 75% | ⚠️ 73%（API 端点覆盖率可继续提）|
| `llm.py` | ≥ 70% | ⚠️ 55%（_extract_json 边界）|
| `embedded.py` | ≥ 70% | ⚠️ 49%（generate_image 真实路径）|
| **总体** | ≥ 60% | **✅ 75%** |

CI 配置（`.github/workflows/ci.yml`）会在覆盖率 < 60% 时 fail。

---

## 四、测试约定

### 1. 用 fixture 而不是真 db
```python
def test_xxx(tmp_db, monkeypatch):
    """tmp_db fixture 提供独立 SQLite db（conftest.py 已定义）。
    monkeypatch 设 MAESTRO_DB env（避免污染项目 db）。"""
```

### 2. mock LLM/Worker，不真调
```python
from unittest.mock import patch

# mock LLM
with patch("maestro.llm.complete", return_value="mock reply"):
    result = chat.chat(conn, "hello")

# mock worker（已有 FakeWorker fixture 自动注册）
with patch("maestro.orchestrator._execute_subtask", fake_exec):
    orchestrator.execute_task(conn, task_id, timeout=10)
```

### 3. 真实子进程只测"快速失败"路径
- ✅ mock subprocess.run 验证参数
- ❌ 不要真跑 mmx / opencode（CI 太慢 + 依赖外部 binary）

### 4. 时间/超时测试用 short timeout
```python
sandbox.request_approval(cmd, task_id="t", timeout=0.5)  # 0.5s 超时测试
```

### 5. SQLite 多连接测试必须 commit
```python
def fake_exec(db_path, task_id, subtask, timeout, model):
    conn = db.init_db(db_path)
    conn.execute("UPDATE subtasks SET status='failed' WHERE id=?", (subtask["id"],))
    conn.commit()  # ★ 不 commit 主连接看不到
    conn.close()
```

---

## 五、写新测试的模板

### 单元测试模板（函数级）
```python
"""<module> <行为> 测试。"""
import pytest
from maestro import <module>


def test_<功能>_<条件>_<期望>():
    """<一句话描述行为>"""
    # Arrange（准备数据）
    input_data = "..."
    # Act（执行）
    result = <module>.<function>(input_data)
    # Assert（验证）
    assert result == "<期望>"
```

### 集成测试模板（带 fixture）
```python
"""<module> 集成测试：跨组件行为。"""
import pytest
from unittest.mock import patch
from maestro import db, orchestrator


def test_<场景>_<预期>(tmp_db, monkeypatch):
    conn = tmp_db

    # 准备任务
    db.create_task(conn, "t_test", "test prompt")
    db.set_task_params(conn, "t_test", scenario="b", worker_type="fake", parallel=True)
    db.add_subtasks(conn, "t_test", [{"id": "t_test_s0", "desc": "x", "worker_type": "fake"}])
    db.set_task_status(conn, "t_test", db.READY)

    # mock 关键路径
    with patch.object(orchestrator, "_execute_subtask", fake_exec):
        orchestrator.execute_task(conn, "t_test", timeout=10)

    # 验证
    assert db.get_task(conn, "t_test")["status"] == "done"
```

### 边界用例 checklist（必跑）
- [ ] 空输入
- [ ] 超长输入
- [ ] None 替代值
- [ ] 编码边界（Base64 / 零宽字符 / 同形 Unicode）
- [ ] 并发（多线程同时调用）
- [ ] 超时
- [ ] 失败重试
- [ ] 状态机边界（pending→ready→running→done 的所有转换）

---

## 六、覆盖率提升优先级

### 已计划
- ⏳ `embedded.py` 49% → 70%：补 generate_image 真实 OpenAI API mock
- ⏳ `llm.py` 55% → 70%：补 `_extract_json` 边界（注释 `[`/`]` 干扰）
- ⏳ `server.py` 73% → 80%：补审批 API 端点（`/approvals/{id}`）

### 长期
- ⏳ end-to-end：用 Playwright 跑完整 UI（看 [live2d-web-wallpaper skill](../../C:/Users/A/.workbuddy/skills/live2d-web-wallpaper)）
- ⏳ 性能压测：1000 任务并发派发 / LLM mock 统计

---

## 七、CI 集成

`.github/workflows/ci.yml`：
- `fast` job：lint + ruff format check + mypy + pytest + coverage（每次 PR）
- `matrix` job：Python 3.11 / 3.12 / 3.13 多版本（仅 main push）
- `secrets-scan` job：TruffleHog + git grep（防密钥）
- `commit-lint` job：Conventional Commits 格式（仅 PR）

覆盖 < 60% 会 fail。测试失败必须修，不允许 skip。

---

## 八、相关资源

- [pytest 文档](https://docs.pytest.org/)
- [pytest-cov 文档](https://pytest-cov.readthedocs.io/)
- [ARCHITECTURE.md](ARCHITECTURE.md) — 模块依赖与状态机
- [LANGGRAPH_BORROW.md](LANGGRAPH_BORROW.md) — 借鉴 LangGraph supervisor 设计