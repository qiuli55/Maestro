"""SQLite 状态层：任务 / 子任务 / 事件，支持重启恢复。"""

import json
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

# 状态机合法取值
PENDING = "pending"
READY = "ready"  # 拆分完成，等待用户确认/编辑（人工闸门）
RUNNING = "running"
DONE = "done"
FAILED = "failed"
RETRY = "retry"
CANCELLED = "cancelled"  # 用户手动取消（终态）

VALID_STATUS = {PENDING, READY, RUNNING, DONE, FAILED, RETRY, CANCELLED}

DEFAULT_DB = Path(__file__).resolve().parents[2] / "maestro.db"

DEFAULT_CONV_ID = "conv_default"  # 存量消息的默认会话


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ====== 连接池（thread-local） ======
# SQLite 连接不能跨线程，所以用 threading.local 按 thread 缓存 path -> conn。
# 同一线程多次 init_db(same_path) 复用同一连接，省去 connect 开销（毫秒级）。
# 不同线程独立连接。close_thread() 清理当前线程缓存。
_pool = threading.local()
_pool_stats = {"open": 0, "reuse": 0, "close": 0}


def _pool_stats_snapshot() -> dict:
    """连接池统计（只读副本，供监控/测试用）。"""
    return dict(_pool_stats)


def close_thread() -> None:
    """关闭当前线程缓存的所有连接（线程退出前调，避免 fd 泄漏）。

    通常用 atexit 注册，或在 worker 子线程 done 时调。
    """
    if not hasattr(_pool, "conns"):
        return
    for path, conn in list(_pool.conns.items()):
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        _pool_stats["close"] += 1
    _pool.conns.clear()


def init_db(db_path: Path | str | None = None, *, use_cache: bool = False) -> sqlite3.Connection:
    """建表（幂等）。返回连接。

    db_path 不传时调用时读取 MAESTRO_DB 环境变量（不冻结在导入期），
    否则用项目根默认库。

    use_cache=False（默认）：每次新建连接——安全、避免 tmp_path 测试
    inode 重用导致 stale 连接。SQLite connect 开销毫秒级，无池可接受。
    use_cache=True：thread-local 缓存（同 path 复用同连接）—— 生产可
    显式启用，监控用 _pool_stats_snapshot() 看 open/reuse 计数。
    """
    if db_path is None:
        db_path = os.environ.get("MAESTRO_DB", DEFAULT_DB)
    db_path = Path(db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if use_cache:
        if not hasattr(_pool, "conns"):
            _pool.conns = {}
        cached = _pool.conns.get(str(db_path))
        if cached is not None:
            _pool_stats["reuse"] += 1
            return cached

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")  # 并发连接写同一文件时等待而非立即失败
    # WAL 模式：读写并发不互斥（默认 rollback 模式下，写锁会阻塞所有读）。
    # 并发测试偶发 `database is locked` 即因此——WAL 缓解之。
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass  # 临时文件系统不支持 WAL 时忽略，回退默认模式
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id           TEXT PRIMARY KEY,
            user_prompt TEXT,
            status       TEXT NOT NULL DEFAULT 'pending',
            result       TEXT,
            result_path  TEXT,
            scenario     TEXT,
            worker_type  TEXT,
            parallel     INTEGER NOT NULL DEFAULT 1,
            no_merge     INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            cancel_requested_at  TEXT  -- 用户发起取消的时间（ISO8601）；子任务 spawn 前检查，未走完的子任务被打断
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subtasks (
            id              TEXT PRIMARY KEY,
            task_id         TEXT NOT NULL REFERENCES tasks(id),
            idx             INTEGER NOT NULL,
            desc            TEXT NOT NULL,
            worker_type     TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            output          TEXT,
            error           TEXT,
            result_path     TEXT,
            source_segments TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL,
            subtask_id TEXT,
            ts         TEXT NOT NULL,
            event      TEXT NOT NULL,
            data       TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS approvals (
            id         TEXT PRIMARY KEY,
            task_id    TEXT NOT NULL,
            subtask_id TEXT,
            cmd        TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            decided_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_profile (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id         TEXT PRIMARY KEY,
            title      TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    # 存量库（如已运行的 maestro.db）可能没有 data 列，幂等补列
    _ensure_event_data_column(conn)
    _ensure_task_columns(conn)
    _ensure_conv_column(conn)
    _ensure_conv_kind_column(conn)
    _ensure_default_conversation(conn)
    # 索引（高频查询加速）。CREATE INDEX IF NOT EXISTS 已是幂等。
    # subtasks.task_id：每次执行任务都按 task_id 查子任务列表
    # task_events.task_id：每个任务详情页都按 task_id 查事件
    _ensure_index(conn, "subtasks", "subtasks_task_id_idx", "(task_id)")
    _ensure_index(conn, "task_events", "task_events_task_id_idx", "(task_id)")
    _ensure_index(conn, "task_events", "task_events_subtask_id_idx", "(subtask_id)")
    _ensure_index(conn, "approvals", "approvals_task_id_idx", "(task_id)")
    _ensure_index(conn, "chat_messages", "chat_messages_conv_id_idx", "(conv_id)")
    conn.commit()
    if use_cache:
        _pool.conns[str(db_path)] = conn
        _pool_stats["open"] += 1
    return conn


def _ensure_index(conn: sqlite3.Connection, table: str, index_name: str, cols: str) -> None:
    """CREATE INDEX IF NOT EXISTS 幂等创建（SQLite 3.8+ 支持）。"""
    conn.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} {cols}")


def _safe_alter(conn: sqlite3.Connection, sql: str) -> None:
    """ALTER TABLE 容错：列已存在时静默忽略（并发场景下 PRAGMA 缓存可能短暂不一致）。"""
    try:
        conn.execute(sql)
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            return  # 列已存在，幂等跳过
        raise


def _ensure_event_data_column(conn: sqlite3.Connection):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(task_events)").fetchall()}
    if "data" not in cols:
        _safe_alter(conn, "ALTER TABLE task_events ADD COLUMN data TEXT")


def _ensure_task_columns(conn: sqlite3.Connection):
    """存量库补 tasks 表的执行参数列（scenario/worker_type/parallel/no_merge/conv_id）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    for col, ddl in (
        ("scenario", "TEXT"),
        ("worker_type", "TEXT"),
        ("parallel", "INTEGER NOT NULL DEFAULT 1"),
        ("no_merge", "INTEGER NOT NULL DEFAULT 0"),
        ("conv_id", "TEXT"),
        ("cancel_requested_at", "TEXT"),  # 用户发起取消时间（协作式取消标志）
    ):
        if col not in cols:
            _safe_alter(conn, f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")


def _ensure_conv_column(conn: sqlite3.Connection):
    """存量库补 chat_messages 的 conv_id 列（对话隔离）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_messages)").fetchall()}
    if "conv_id" not in cols:
        _safe_alter(conn, "ALTER TABLE chat_messages ADD COLUMN conv_id TEXT")


def _ensure_default_conversation(conn: sqlite3.Connection):
    """迁移：确保默认会话存在（旧消息无 conv_id 归入它）。

    用 INSERT OR IGNORE 替代"先查后插"两步走：并发场景下 SELECT
    都返回 0 时两个连接都尝试 INSERT，第二个触发 UNIQUE 失败。
    """
    conn.execute(
        "INSERT OR IGNORE INTO conversations (id, title, created_at, updated_at) VALUES (?,?,?,?)",
        (DEFAULT_CONV_ID, "默认对话", _now(), _now()),
    )
    conn.execute(
        "UPDATE chat_messages SET conv_id=? WHERE conv_id IS NULL OR conv_id=''",
        (DEFAULT_CONV_ID,),
    )


def _ensure_conv_kind_column(conn: sqlite3.Connection):
    """存量库补 conversations.kind 列（chat/task，默认 chat）+ 按标题迁移旧数据。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()}
    if "kind" not in cols:
        _safe_alter(conn, "ALTER TABLE conversations ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'")
        # 旧数据迁移：标题以"任务："开头的会话归为 task
        conn.execute("UPDATE conversations SET kind='task' WHERE title LIKE '任务：%'")
        conn.commit()


def _set_status(cur: sqlite3.Cursor, table: str, row_id: str, status: str):
    if status not in VALID_STATUS:
        raise ValueError(f"非法状态: {status}")
    cur.execute(
        f"UPDATE {table} SET status=?, updated_at=? WHERE id=?",
        (status, _now(), row_id),
    )


def create_task(conn: sqlite3.Connection, task_id: str, user_prompt: str, conv_id: str | None = None) -> dict:
    now = _now()
    conn.execute(
        "INSERT INTO tasks (id, user_prompt, status, created_at, updated_at, conv_id) VALUES (?,?,?,?,?,?)",
        (task_id, user_prompt, PENDING, now, now, conv_id),
    )
    conn.commit()
    return get_task(conn, task_id)


def get_task(conn: sqlite3.Connection, task_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def set_task_status(conn: sqlite3.Connection, task_id: str, status: str):
    _set_status(conn.cursor(), "tasks", task_id, status)
    conn.commit()


def set_task_result(conn: sqlite3.Connection, task_id: str, result: str, result_path: str | None = None):
    conn.execute(
        "UPDATE tasks SET result=?, result_path=?, updated_at=? WHERE id=?",
        (result, result_path, _now(), task_id),
    )
    conn.commit()


def add_subtasks(conn: sqlite3.Connection, task_id: str, subtasks: list[dict]) -> list[dict]:
    """subtasks: [{id, desc, worker_type, source_segments?}]"""
    if not subtasks:
        return []
    now = _now()
    rows = [
        (st["id"], task_id, idx, st["desc"], st["worker_type"], PENDING, st.get("source_segments"), now, now)
        for idx, st in enumerate(subtasks)
    ]
    # executemany 一次 INSERT 多行，比循环单条 INSERT 快 ~10x
    conn.executemany(
        """INSERT INTO subtasks
           (id, task_id, idx, desc, worker_type, status, source_segments, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    return subtasks


def get_subtasks(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM subtasks WHERE task_id=? ORDER BY idx", (task_id,)).fetchall()
    return [dict(r) for r in rows]


def get_subtask(conn: sqlite3.Connection, subtask_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM subtasks WHERE id=?", (subtask_id,)).fetchone()
    return dict(row) if row else None


def set_task_params(
    conn: sqlite3.Connection,
    task_id: str,
    scenario: str | None = None,
    worker_type: str | None = None,
    parallel: bool | None = None,
    no_merge: bool | None = None,
):
    """记录任务的执行参数（prepare 阶段写入，execute 阶段读取）。"""
    sets, vals = [], []
    if scenario is not None:
        sets.append("scenario=?")
        vals.append(scenario)
    if worker_type is not None:
        sets.append("worker_type=?")
        vals.append(worker_type)
    if parallel is not None:
        sets.append("parallel=?")
        vals.append(1 if parallel else 0)
    if no_merge is not None:
        sets.append("no_merge=?")
        vals.append(1 if no_merge else 0)
    if not sets:
        return
    sets.append("updated_at=?")
    vals += [_now(), task_id]
    conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()


def replace_subtasks(conn: sqlite3.Connection, task_id: str, subtasks: list[dict]) -> list[dict]:
    """整体替换某任务的子任务列表（人工闸门编辑：增删改 / 调序）。

    仅允许在任务未开始执行（status=ready/pending）时调用，调用方负责校验。
    subtasks: [{id?, desc, worker_type}]——id 缺省则按 idx 生成 st_N。
    """
    now = _now()
    conn.execute("DELETE FROM subtasks WHERE task_id=?", (task_id,))
    rows = []
    for idx, st in enumerate(subtasks):
        sid = st.get("id") or f"st_{idx + 1}"
        conn.execute(
            """INSERT INTO subtasks
               (id, task_id, idx, desc, worker_type, status, source_segments, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (sid, task_id, idx, st["desc"], st["worker_type"], PENDING, st.get("source_segments"), now, now),
        )
        rows.append({**st, "id": sid})
    conn.commit()
    return rows


def set_subtask_status(conn: sqlite3.Connection, subtask_id: str, status: str):
    _set_status(conn.cursor(), "subtasks", subtask_id, status)
    conn.commit()


def set_subtask_output(
    conn: sqlite3.Connection,
    subtask_id: str,
    status: str,
    output: str | None = None,
    error: str | None = None,
    result_path: str | None = None,
):
    conn.execute(
        """UPDATE subtasks SET status=?, output=?, error=?, result_path=?, updated_at=?
           WHERE id=?""",
        (status, output, error, result_path, _now(), subtask_id),
    )
    conn.commit()


def log_event(
    conn: sqlite3.Connection,
    task_id: str,
    event: str,
    subtask_id: str | None = None,
    data=None,
    *,
    _commit: bool = True,
):
    """记录事件。data 为可 JSON 序列化的任意对象（dict/list/str/int…），
    存入前序列化为 JSON 字符串；序列化失败则退化为 str(data)。

    _commit=False：跳过 commit（用于批量调用场景，由调用方统一提交）。
    默认 _commit=True 保持向后兼容（旧测试不修改）。
    """
    data_json = None
    if data is not None:
        try:
            data_json = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            data_json = str(data)
    conn.execute(
        "INSERT INTO task_events (task_id, subtask_id, ts, event, data) VALUES (?,?,?,?,?)",
        (task_id, subtask_id, _now(), event, data_json),
    )
    if _commit:
        conn.commit()


def log_events_batch(conn: sqlite3.Connection, events: list[dict]) -> None:
    """批量记录事件（一次 commit，多次 insert）。

    events: [{"task_id": "...", "subtask_id": "...", "event": "...", "data": ...}, ...]
    所有事件共享同一时间戳（批量语义：同一时刻发生）。
    """
    if not events:
        return
    now = _now()
    rows = []
    for e in events:
        data = e.get("data")
        data_json = None
        if data is not None:
            try:
                data_json = json.dumps(data, ensure_ascii=False)
            except (TypeError, ValueError):
                data_json = str(data)
        rows.append((e["task_id"], e.get("subtask_id"), now, e["event"], data_json))
    conn.executemany(
        "INSERT INTO task_events (task_id, subtask_id, ts, event, data) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()


def get_events(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
    return [dict(r) for r in rows]


def cancel_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """置任务为 CANCELLED（终态）。仅允许未终态任务；返回是否生效。

    同时写 cancel_requested_at = now() — 协作式取消标志：
    执行中的子任务跑完当前 step 后会检测到，下次 dispatch 时立即停止。
    """
    task = get_task(conn, task_id)
    if not task or task["status"] in (DONE, FAILED, CANCELLED):
        return False
    cur = conn.cursor()
    _set_status(cur, "tasks", task_id, CANCELLED)
    cur.execute(
        "UPDATE tasks SET cancel_requested_at = COALESCE(cancel_requested_at, ?) WHERE id = ?",
        (_now(), task_id),
    )
    conn.commit()
    log_event(conn, task_id, "task cancelled")
    return True


def delete_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """级联删除任务及其子任务、事件。返回是否删除成功。"""
    task = get_task(conn, task_id)
    if not task:
        return False
    conn.execute("DELETE FROM task_events WHERE task_id=?", (task_id,))
    conn.execute("DELETE FROM subtasks WHERE task_id=?", (task_id,))
    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()
    return True


def add_chat_message(conn: sqlite3.Connection, role: str, content: str, conv_id: str = DEFAULT_CONV_ID) -> int:
    """追加一条闲聊记录（role: user/assistant），归入指定会话。返回消息 id。"""
    cur = conn.execute(
        "INSERT INTO chat_messages (role, content, created_at, conv_id) VALUES (?,?,?,?)",
        (role, content, _now(), conv_id),
    )
    conn.commit()
    return cur.lastrowid


def get_chat_history(conn: sqlite3.Connection, limit: int = 20, conv_id: str | None = None) -> list[dict]:
    """取某会话最近 limit 条闲聊（时间正序）。conv_id=None 返回全部（兼容）。"""
    if conv_id is None:
        rows = conn.execute("SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE conv_id=? ORDER BY id DESC LIMIT ?",
            (conv_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def clear_chat_history(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM chat_messages")
    conn.commit()


# ---------- 会话（对话隔离） ----------


def create_conversation(conn: sqlite3.Connection, title: str | None = None, kind: str = "chat") -> str:
    """创建新会话，返回 conv_id。kind: chat/task（聊天/任务历史分开）。"""
    conv_id = f"conv_{os.urandom(4).hex()}"
    now = _now()
    conn.execute(
        "INSERT INTO conversations (id, title, created_at, updated_at, kind) VALUES (?,?,?,?,?)",
        (conv_id, (title or "").strip() or "新对话", now, now, kind),
    )
    conn.commit()
    return conv_id


def get_conversation(conn: sqlite3.Connection, conv_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
    return dict(r) if r else None


def list_conversations(conn: sqlite3.Connection, kind: str | None = None) -> list[dict]:
    """会话列表（按最近活跃倒序，可只取某类），带消息数与最后一条内容预览。"""
    if kind:
        rows = conn.execute(
            """
            SELECT c.id, c.title, c.kind, c.updated_at,
                   (SELECT COUNT(*) FROM chat_messages m WHERE m.conv_id = c.id) AS msg_count,
                   (SELECT content FROM chat_messages m WHERE m.conv_id = c.id
                     ORDER BY m.id DESC LIMIT 1) AS last_content
            FROM conversations c
            WHERE c.kind = ?
            ORDER BY c.updated_at DESC
            """,
            (kind,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT c.id, c.title, c.kind, c.updated_at,
                   (SELECT COUNT(*) FROM chat_messages m WHERE m.conv_id = c.id) AS msg_count,
                   (SELECT content FROM chat_messages m WHERE m.conv_id = c.id
                     ORDER BY m.id DESC LIMIT 1) AS last_content
            FROM conversations c
            ORDER BY c.updated_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def touch_conversation(conn: sqlite3.Connection, conv_id: str) -> None:
    """更新会话活跃时间（对话发生时调用）。"""
    conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (_now(), conv_id))
    conn.commit()


def rename_conversation(conn: sqlite3.Connection, conv_id: str, title: str) -> bool:
    """重命名会话。返回是否生效。"""
    cur = conn.execute(
        "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
        (title.strip(), _now(), conv_id),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_conversation(conn: sqlite3.Connection, conv_id: str) -> bool:
    """删除会话及其消息。返回是否生效。"""
    if not get_conversation(conn, conv_id):
        return False
    conn.execute("DELETE FROM chat_messages WHERE conv_id=?", (conv_id,))
    conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
    conn.commit()
    return True


def get_user_profile(conn: sqlite3.Connection) -> dict:
    """读取数字人记住的用户档案（key → value）。"""
    rows = conn.execute("SELECT key, value FROM user_profile ORDER BY key").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_user_profile(conn: sqlite3.Connection, key: str, value: str) -> None:
    """写入/更新一条用户档案（覆盖式，只保留最新）。"""
    conn.execute(
        "INSERT INTO user_profile (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value.strip(), _now()),
    )
    conn.commit()


def clear_user_profile(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM user_profile")
    conn.commit()
