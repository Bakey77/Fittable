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

#模块初始化，import时自动建表
try:
    init_db()
    logger.info(f"Database initialized at {DB_PATH}")
except Exception as e:
    logger.warning(f"Database init failed(will retry on firsrt use): {e}")