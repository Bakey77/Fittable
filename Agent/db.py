"""
数据库模块：SQLite 持久化（长期记忆 + 聊天日志）。
使用python内置标准库 sqlite3，零外部依赖。
"""
import sqlite3
import json
import logging
import threading
from pathlib import Path
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)    
# 数据库文件路径
DB_DIR = Path(__file__).resolve().parent / "data" #当前项目文件夹里的data文件夹
DB_PATH = DB_DIR / "fit_agent.db"   #数据库放在data文件夹里，叫fit_agent.db

_local = threading.local() #线程本地连接（每个线程拥有自己的连接，避免多线程竞争）

def _get_conn() -> sqlite3.Connection:
    """
    获取当前线程的sqlite连接（自动创建）
    """
    conn = getattr(_local,"conn",None) #先看看当前线程有没有连接了
    if conn is None:
        DB_DIR.mkdir(parents=True, exist_ok=True) #确保目录存在，不存在就创建
        conn = sqlite3.connect(str(DB_PATH),check_same_thread=False) #打开数据库.db，false的意思是，我是跨线程的，我自己管好
        conn.row_factory = sqlite3.Row  #查询结果可以用dict方式访问 用列名访问
        conn.execute("PRAGMA journal_mode=WAL") #WAL 读写同时进行
        conn.execute("PRAGMA foreign_keys=ON") #打开外键检查 
        _local.conn = conn #把建立好的连接存到当前线程的命名空间，下次来直接复用
    return conn #返回连接给调用方

def close_connection():
    """
    关闭当前线程的sqlite连接
    """
    conn = getattr(_local, "conn", None)
    if conn:
        conn.close()
        _local.conn = None

###表的初始化

def init_db():
    """
    创建所有需要的表（幂等操作，存在则跳过）
    """
    conn = _get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS user_profile(
            session_id TEXT PRIMARY KEY,
            name TEXT,
            gender TEXT,
            age TEXT,
            height TEXT,
            weight TEXT,
            training_level TEXT,
            goal TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS user_attributes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            category TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES user_profile(session_id),
            UNIQUE(session_id, category, key)
        );

        CREATE INDEX IF NOT EXISTS idx_attr_session ON user_attributes(session_id,category);
        -- 在 user_attributes 表上建索引，覆盖 (session_id, category) 两列，加速按 session_id 和 category 查询

        CREATE TABLE IF NOT EXISTS conflict_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            source TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES user_profile(session_id)
        );

        CREATE INDEX IF NOT EXISTS idx_conflict_session ON conflict_log(session_id);

        CREATE TABLE IF NOT EXISTS chat_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL, -- user/agent
            content TEXT NOT NULL,
            intent TEXT, -- 用户意图，预留字段，暂不使用
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES user_profile(session_id)
        );

        CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_log(session_id,created_at);

        CREATE TABLE IF NOT EXISTS historical_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            content TEXT NOT NULL,
            source_turns TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_historical_events_session ON historical_events(session_id,created_at);

        CREATE TABLE IF NOT EXISTS historical_event_sync_jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_sync_jobs_status ON historical_event_sync_jobs(status);
        """
    )
    conn.commit()

#用户档案 CRUD

def upsert_profile(session_id:str,fields:dict[str,str])->None:
    """
    插入或更新用户档案。
    fields: name, gender, age, height, weight, training_level, goal...
    """
    if not fields:
        return
    conn = _get_conn()
    # 先检查用户档案是否存在 这个人存不存在 INSERT
    conn.execute(
        "INSERT OR IGNORE INTO user_profile(session_id) VALUES (?)",
        (session_id,)
    )
    #构建SET子句  把刚才填的表单信息写入
    set_clause = ",".join(f"{k}=?" for k in fields.keys())
    sql = f"UPDATE user_profile SET {set_clause}, updated_at=datetime('now') WHERE session_id = ? "
    conn.execute(sql,list(fields.values()) + [session_id])
    conn.commit()

def get_profile(session_id:str)->Optional[dict]:
    """
    获取用户档案
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM user_profile WHERE session_id = ?",
        (session_id,)
    ).fetchone()
    return dict(row) if row else None

#用户属性 CRUD （偏好、约束、计划事实）

def upsert_attribute(session_id:str,category:str,key:str,value:str)->None:
    """
    插入或更新一个属性 category: preference | constraint
    """
    conn = _get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO user_profile(session_id) VALUES (?)",
        (session_id,)
    )
    conn.execute(
        """
        INSERT INTO user_attributes (session_id,category,key,value)
        VALUES (?,?,?,?)
        ON CONFLICT(session_id,category,key) DO UPDATE
        SET value = excluded.value,
            updated_at = datetime('now') 
        """,
        (session_id,category,key,value)
    )
    conn.commit()

def get_attributes(session_id:str,category:Optional[str]=None)-> list[dict]:
    """
    获取属性列表。 不指定category则返回全部
    """
    conn = _get_conn()
    if category:
        rows = conn.execute(
            "SELECT * FROM user_attributes WHERE session_id = ? AND category = ?",
            (session_id,category)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM user_attributes WHERE session_id = ?",
            (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]
    
#冲突日志

def append_conflict(session_id:str,filed_name:str,old_value:str,new_value:str,source:str)->None:
    """
    记录冲突日志
    """
    conn = _get_conn()
    conn.execute(
        "INSERT INTO conflict_log (session_id,field_name,old_value,new_value,source) VALUES (?,?,?,?,?)",
        (session_id,filed_name,old_value,new_value,source)
    )
    conn.commit()

def get_conflicts(session_id:str,limit:int = 20) -> list[dict]:
    """
    获取最近20条冲突日志
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM conflict_log WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
        (session_id,limit)
    ).fetchall()
    return [dict(r) for r in rows]


def get_session_last_updated(session_id: str) -> Optional[str]:
    """
    获取一个 session 在长期记忆相关表中的最近更新时间。
    优先使用真实 updated_at / created_at，而不是读取时生成时间。
    """
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT MAX(ts) AS last_updated
        FROM (
            SELECT updated_at AS ts FROM user_profile WHERE session_id = ?
            UNION ALL
            SELECT updated_at AS ts FROM user_attributes WHERE session_id = ?
            UNION ALL
            SELECT created_at AS ts FROM conflict_log WHERE session_id = ?
        )
        """,
        (session_id, session_id, session_id)
    ).fetchone()
    if not row:
        return None
    return row["last_updated"]

#聊天日志

def append_chat_log(session_id:str,role:str,content:str,intent:Optional[str]=None)->None:
    """
    追加一条聊天日志
    """
    conn = _get_conn()
    conn.execute(
        "INSERT INTO chat_log (session_id,role,content,intent) VALUES (?,?,?,?)",
        (session_id,role,content,intent)
    )
    conn.commit()

def get_chat_history(session_id:str,limit:int=50)->list[dict]:
    """
    获取最近N条聊天日志
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM chat_log WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
        (session_id,limit)
    ).fetchall()
    return [dict(r) for r in rows]

#统计查询
def get_stats()->dict:
    """
    返回数据库统计信息
    """
    conn = _get_conn()
    profile_count = conn.execute("SELECT COUNT(*) FROM user_profile").fetchone()[0]
    chat_count = conn.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0]
    conflict_count = conn.execute("SELECT COUNT(*) FROM conflict_log").fetchone()[0]
    return{
        "total_sessions": profile_count,
        "total_chats_logs": chat_count,
        "total_conflicts": conflict_count,
        "db.path": str(DB_PATH)
    }

# ---------------------------------------------------------------------------
# 历史事件 CRUD
# ---------------------------------------------------------------------------

def insert_historical_event(session_id: str, content: str, source_turns: list[int] | str) -> int:
    """
    插入一条历史事件。

    Args:
        session_id: 会话标识
        content: 事件摘要文本
        source_turns: 来源轮次（list 或 JSON 数组字符串）

    Returns:
        新插入的 event id (int)
    """
    conn = _get_conn()
    if isinstance(source_turns, list):
        source_turns = json.dumps(source_turns, ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO historical_events (session_id, content, source_turns) VALUES (?, ?, ?)",
        (session_id, content, source_turns)
    )
    conn.commit()
    return cur.lastrowid


def get_historical_events(session_id: str, limit: int = 50) -> list[dict]:
    """获取指定 session 的历史事件，按时间倒序。"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM historical_events WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
        (session_id, limit)
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("source_turns"):
            try:
                d["source_turns"] = json.loads(d["source_turns"])
            except (json.JSONDecodeError, TypeError):
                pass
        result.append(d)
    return result


def count_historical_events(session_id: str) -> int:
    """统计某个 session 的历史事件数量。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM historical_events WHERE session_id = ?", (session_id,)
    ).fetchone()
    return row[0] if row else 0


def prune_old_events(session_id: str, max_events: int = 50) -> list[int]:
    """
    裁剪最旧的历史事件，仅保留最新 max_events 条。

    返回被裁剪的 event id 列表（用于同步删除 Qdrant point）。
    """
    conn = _get_conn()
    total = count_historical_events(session_id)
    if total <= max_events:
        return []

    to_delete = total - max_events
    # 选出最旧的 to_delete 条
    rows = conn.execute(
        "SELECT id FROM historical_events WHERE session_id = ? ORDER BY created_at ASC LIMIT ?",
        (session_id, to_delete)
    ).fetchall()
    deleted_ids = [r["id"] for r in rows]

    conn.execute(
        "DELETE FROM historical_events WHERE id IN ({})".format(
            ",".join("?" for _ in deleted_ids)
        ),
        deleted_ids
    )
    conn.commit()
    logger.info(f"[DB] pruned {len(deleted_ids)} old events for session={session_id}")
    return deleted_ids


# ---------------------------------------------------------------------------
# 同步补偿 (双写失败补偿)
# ---------------------------------------------------------------------------

def create_sync_job(event_id: int, session_id: str, error: str = "") -> int:
    """为写入 Qdrant 失败的事件创建补偿任务。"""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO historical_event_sync_jobs (event_id, session_id, status, retry_count, last_error) VALUES (?, ?, 'pending', 0, ?)",
        (event_id, session_id, error)
    )
    conn.commit()
    return cur.lastrowid


def get_pending_sync_jobs(limit: int = 20) -> list[dict]:
    """获取待处理的同步补偿任务。"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM historical_event_sync_jobs WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?",
        (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def mark_sync_job_done(job_id: int) -> None:
    """标记补偿任务为完成。"""
    conn = _get_conn()
    conn.execute(
        "UPDATE historical_event_sync_jobs SET status='done', updated_at=datetime('now') WHERE id=?",
        (job_id,)
    )
    conn.commit()


def mark_sync_job_failed(job_id: int, error: str) -> None:
    """递增重试计数并记录错误。重试超过 5 次则标记为 failed。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT retry_count FROM historical_event_sync_jobs WHERE id=?", (job_id,)
    ).fetchone()
    if not row:
        return
    new_count = row["retry_count"] + 1
    new_status = "failed" if new_count >= 5 else "pending"
    conn.execute(
        "UPDATE historical_event_sync_jobs SET retry_count=?, last_error=?, status=?, updated_at=datetime('now') WHERE id=?",
        (new_count, error, new_status, job_id)
    )
    conn.commit()


def get_historical_event_by_id(event_id: int) -> dict | None:
    """通过 ID 获取单条历史事件（用于补偿重试）。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM historical_events WHERE id = ?", (event_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("source_turns"):
        try:
            d["source_turns"] = json.loads(d["source_turns"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


#模块初始化，import时自动建表
try:
    init_db()
    logger.info(f"Database initialized at {DB_PATH}")
except Exception as e:
    logger.warning(f"Database init failed(will retry on firsrt use): {e}")
