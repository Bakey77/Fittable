"""
短期记忆模块（Session Memory）
进程内存实现，不依赖外部存储。

压缩失败备份：
- Backup Key: session_id + "_backup"
- Flag Key: session_id + "_compress_failed"

Redis 挂了时回退到进程内存 _fallback_store，恢复后第一个请求自动同步。
"""
from datetime import datetime
from typing import Any
import json
import hmac
import secrets
import threading
import time
from Agent.memory.metrics import record_redis_op

try:
    import redis
except ImportError:
    redis = None


# ---------------------------------------------------------------------------
# Redis 配置
# ---------------------------------------------------------------------------
_redis_client = None
_redis_was_down = False  # 标记 Redis 是否曾经挂过
_SESSION_TTL = 30 * 24 * 60 * 60  # 30天

# ---------------------------------------------------------------------------
# Lua 脚本（原子读-改-写）
# ---------------------------------------------------------------------------

# append_turn: 读出 memory，追加一轮，再写回
_LUA_APPEND_TURN = """
local key = KEYS[1]
local role = ARGV[1]
local text = ARGV[2]
local ttl = tonumber(ARGV[3])

local data = redis.call('GET', key)
local memory
if data then
    memory = cjson.decode(data)
else
    memory = cjson.decode('{"recent_turns":[],"working_memory":{"waiting_info":null,"pending_intent":null,"pending_entities":null},"metadata":{},"session_token":null}')
end

table.insert(memory.recent_turns, {role=role, text=text})
redis.call('SETEX', key, ttl, cjson.encode(memory))
return 1
"""

# update_working_memory: 读出 memory，按字段级 op 更新 working_memory，再写回
# 每个字段独立控制：KEEP（保留当前值）/ CLEAR（清空）/ SET（写入新值）
_LUA_UPDATE_WORKING_MEMORY = """
local key = KEYS[1]
local wi_op = ARGV[1]
local wi_val = ARGV[2]
local pi_op = ARGV[3]
local pi_val = ARGV[4]
local pe_op = ARGV[5]
local pe_val = ARGV[6]
local ttl = tonumber(ARGV[7])

local data = redis.call('GET', key)
local memory
if data then
    memory = cjson.decode(data)
else
    memory = cjson.decode('{"recent_turns":[],"working_memory":{"waiting_info":null,"pending_intent":null,"pending_entities":null},"metadata":{},"session_token":null}')
end

-- waiting_info
if wi_op == "SET" then
    memory.working_memory.waiting_info = cjson.decode(wi_val)
elseif wi_op == "CLEAR" then
    memory.working_memory.waiting_info = nil
end
-- KEEP: 不修改，保留 Redis 当前值

-- pending_intent
if pi_op == "SET" then
    memory.working_memory.pending_intent = pi_val
elseif pi_op == "CLEAR" then
    memory.working_memory.pending_intent = nil
end
-- KEEP: 不修改

-- pending_entities
if pe_op == "SET" then
    memory.working_memory.pending_entities = cjson.decode(pe_val)
elseif pe_op == "CLEAR" then
    memory.working_memory.pending_entities = nil
end
-- KEEP: 不修改

redis.call('SETEX', key, ttl, cjson.encode(memory))
return 1
"""

# trim_and_summarize: 读出 memory，裁剪 recent_turns，再写回
_LUA_TRIM = """
local key = KEYS[1]
local max_turns = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])

local data = redis.call('GET', key)
if not data then
    return 0
end

local memory = cjson.decode(data)

if #memory.recent_turns > max_turns then
    local new_turns = {}
    for i = #memory.recent_turns - max_turns + 1, #memory.recent_turns do
        table.insert(new_turns, memory.recent_turns[i])
    end
    memory.recent_turns = new_turns
end

redis.call('SETEX', key, ttl, cjson.encode(memory))
return 1
"""

# update_metadata: 读出 memory，原子更新 metadata 字段
_LUA_UPDATE_METADATA = """
local key = KEYS[1]
local operation = ARGV[1]
local ttl = tonumber(ARGV[2])
local batch_size = tonumber(ARGV[3])

local data = redis.call('GET', key)
local memory
if data then
    memory = cjson.decode(data)
else
    memory = cjson.decode('{"recent_turns":[],"working_memory":{"waiting_info":null,"pending_intent":null,"pending_entities":null},"metadata":{},"session_token":null}')
end

if not memory.metadata then
    memory.metadata = {}
end

if operation == "increment_total_turns" then
    memory.metadata.total_turns = (memory.metadata.total_turns or 0) + 1
elseif operation == "mark_batch_processed" then
    local total_turns = memory.metadata.total_turns or 0
    memory.metadata.long_memory_batch_processed = math.floor(total_turns / batch_size)
end

redis.call('SETEX', key, ttl, cjson.encode(memory))
return 1
"""

# fallback 线程锁（保护 _fallback_store 的并发读写）
_fallback_lock = threading.Lock()


_registered_scripts: dict[str, str] = {}  # name -> sha

_pool = None  # Redis 连接池

def _get_pool():
    """返回 Redis 连接池单例。池是线程安全的，多线程可共享。"""
    global _pool
    if _pool is not None:
        return _pool
    if redis is None:
        return None

    from config import Config
    _pool = redis.ConnectionPool(
        host=Config.REDIS_HOST,
        port=Config.REDIS_PORT,
        db=Config.REDIS_DB,
        password=Config.REDIS_PASSWORD or None,
        decode_responses=True,
        max_connections=Config.REDIS_MAX_CONNECTIONS,
        socket_timeout=Config.REDIS_SOCKET_TIMEOUT,
        socket_connect_timeout=2,
        retry_on_timeout=True,
        health_check_interval=30,
    )
    return _pool



def _get_redis_client():
    global _redis_client, _redis_was_down, _registered_scripts

    if redis is None:
        return None

    # client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)  #创建socket文件描述符 tcp握手 发redis auth命令 链接就绪
    pool = _get_pool()
    if pool is None:
        return None
    client = redis.Redis(connection_pool=pool)  # 从池中获取连接（线程安全）
    try:
        client.ping()
    except (redis.ConnectionError, redis.TimeoutError, redis.RedisError):
        _redis_was_down = True
        return None

    # Redis 恢复了！把 _fallback_store 同步回去
    if _redis_was_down and _fallback_store:
        for session_id, memory in _fallback_store.items():
            key = _key(session_id)
            client.setex(key, _SESSION_TTL, json.dumps(memory))
        _fallback_store.clear()
        _redis_was_down = False

    _redis_client = client

    # 注册 Lua 脚本
    scripts = {
        "append_turn": _LUA_APPEND_TURN,
        "update_working_memory": _LUA_UPDATE_WORKING_MEMORY,
        "trim": _LUA_TRIM,
        "update_metadata": _LUA_UPDATE_METADATA,
    }
    for name, script in scripts.items():
        if name not in _registered_scripts:
            _registered_scripts[name] = client.script_load(script)

    return client


def _key(session_id: str) -> str:
    """Redis session key"""
    return f"fit_agent:session:{session_id}"


def _backup_key(session_id: str) -> str:
    """Redis 压缩备份 key"""
    return f"fit_agent:session:backup:{session_id}"


def _flag_key(session_id: str) -> str:
    """Redis 压缩失败标记 key"""
    return f"fit_agent:session:compress_failed:{session_id}"


# ---------------------------------------------------------------------------
# 内存兜底（Redis 不可用时回退）
# ---------------------------------------------------------------------------
_fallback_store: dict[str, dict[str, Any]] = {}


def _empty_memory() -> dict[str, Any]:
    """返回空记忆结构"""
    return {
        "recent_turns": [],
        "working_memory": {
            "waiting_info": None,
            "pending_intent": None,
            "pending_entities": None,
        },
        "metadata": {},
        "session_token": None,
    }


def _get_memory_impl(session_id: str) -> dict[str, Any]:
    """获取记忆结构（Redis优先，失败回退到内存）"""
    client = _get_redis_client()
    start = time.monotonic()

    if client is None:
        if session_id not in _fallback_store:
            _fallback_store[session_id] = _empty_memory()
        elapsed = (time.monotonic() - start) * 1000
        record_redis_op(session_id, "get_memory_fallback", elapsed)
        return _fallback_store[session_id]

    key = _key(session_id)
    data = client.get(key)

    if data is None:
        memory = _empty_memory()
        client.setex(key, _SESSION_TTL, json.dumps(memory))
        elapsed = (time.monotonic() - start) * 1000
        record_redis_op(session_id, "get_memory", elapsed, client)
        return memory

    elapsed = (time.monotonic() - start) * 1000
    record_redis_op(session_id, "get_memory", elapsed, client)
    return json.loads(data)


def _save_memory(session_id: str, memory: dict[str, Any]) -> None:
    """保存记忆到 Redis（同步 TTL）"""
    client = _get_redis_client()

    if client is None:
        _fallback_store[session_id] = memory
        return

    key = _key(session_id)
    client.setex(key, _SESSION_TTL, json.dumps(memory))


# ---------------------------------------------------------------------------
# LLM 语义压缩
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 备份与标记管理
# ---------------------------------------------------------------------------

def save_compress_backup(session_id: str, older_turns: list[dict[str, str]]) -> None:
    """
    保存压缩失败时的备份（older_turns 暂存）

    参数:
    - session_id: 会话标识
    - older_turns: 需要压缩的旧对话列表
    """
    client = _get_redis_client()
    backup = {"older_turns": older_turns, "saved_at": datetime.now().isoformat()}

    if client is None:
        _fallback_store[f"_backup_{session_id}"] = backup
        return

    client.setex(_backup_key(session_id), _SESSION_TTL, json.dumps(backup))


def get_compress_backup(session_id: str) -> dict | None:
    """获取压缩备份，若不存在返回 None"""
    client = _get_redis_client()

    if client is None:
        return _fallback_store.get(f"_backup_{session_id}")

    data = client.get(_backup_key(session_id))
    return json.loads(data) if data else None


def clear_compress_backup(session_id: str) -> None:
    """清除压缩备份"""
    client = _get_redis_client()

    if client is None:
        _fallback_store.pop(f"_backup_{session_id}", None)
        return

    client.delete(_backup_key(session_id))


def set_compress_failed(session_id: str) -> None:
    """标记压缩失败"""
    client = _get_redis_client()

    if client is None:
        _fallback_store[f"_compress_failed_{session_id}"] = True
        return

    client.setex(_flag_key(session_id), _SESSION_TTL, "1")


def get_compress_failed(session_id: str) -> bool:
    """检查压缩是否标记失败"""
    client = _get_redis_client()

    if client is None:
        return _fallback_store.get(f"_compress_failed_{session_id}", False)

    return client.exists(_flag_key(session_id)) > 0


def clear_compress_failed(session_id: str) -> None:
    """清除压缩失败标记"""
    client = _get_redis_client()

    if client is None:
        _fallback_store.pop(f"_compress_failed_{session_id}", None)
        return

    client.delete(_flag_key(session_id))


def check_compress_status(session_id: str) -> dict[str, Any]:
    """
    检查压缩状态

    返回:
    - needs_retry: bool  是否需要重试
    - backup_info: dict | None  备份信息
    """
    backup = get_compress_backup(session_id)
    failed = get_compress_failed(session_id)

    return {
        "needs_retry": failed and backup is not None,
        "backup_info": {
            "turns_count": len(backup.get("older_turns", [])) if backup else 0,
            "saved_at": backup.get("saved_at") if backup else None,
        } if backup else None,
    }


def discard_compress_backup(session_id: str) -> None:
    """丢弃压缩备份，释放空间（不再重试压缩）"""
    clear_compress_backup(session_id)
    clear_compress_failed(session_id)


# ---------------------------------------------------------------------------
# 核心记忆读写
# ---------------------------------------------------------------------------

def get_session_memory(session_id: str) -> dict[str, Any]:
    """获取 session 的完整记忆结构，不存在则返回空结构。"""
    return _get_memory_impl(session_id)


def append_turn(session_id: str, role: str, text: str) -> None:
    """追加一轮对话到 recent_turns。role = "user" | "assistant"。原子操作。"""
    client = _get_redis_client()
    start = time.monotonic()

    if client is None:
        with _fallback_lock:
            memory = _fallback_store.get(session_id, None)
            if memory is None:
                memory = _empty_memory()
                _fallback_store[session_id] = memory
            memory["recent_turns"].append({"role": role, "text": text})
        elapsed = (time.monotonic() - start) * 1000
        record_redis_op(session_id, "append_turn_fallback", elapsed)
        return

    sha = _registered_scripts.get("append_turn")
    if sha:
        client.evalsha(sha, 1, _key(session_id), role, text, _SESSION_TTL)
    else:
        # 降级：理论上不会走到这里（脚本未注册说明 Redis 有问题）
        client.eval(_LUA_APPEND_TURN, 1, _key(session_id), role, text, _SESSION_TTL)
    elapsed = (time.monotonic() - start) * 1000
    record_redis_op(session_id, "append_turn", elapsed, client)


def update_working_memory(
    session_id: str,
    waiting_info_op: str = "KEEP",
    waiting_info: dict | None = None,
    pending_intent_op: str = "KEEP",
    pending_intent: str | None = None,
    pending_entities_op: str = "KEEP",
    pending_entities: dict | None = None,
) -> None:
    """原子更新 working_memory，每个字段独立控制。

    每个字段的 op:
    - "KEEP":  不修改，保留 Redis 当前值（并发安全——在 Lua 内读最新值决定）
    - "CLEAR": 显式清空该字段
    - "SET":   写入新值（waiting_info / pending_entities 由 value 参数传入）
    """
    client = _get_redis_client()
    start = time.monotonic()
    if client is None:
        with _fallback_lock:
            memory = _fallback_store.get(session_id, None)
            if memory is None:
                memory = _empty_memory()
                _fallback_store[session_id] = memory
            wm = memory.setdefault("working_memory", {})
            if waiting_info_op == "SET":
                wm["waiting_info"] = waiting_info
            elif waiting_info_op == "CLEAR":
                wm["waiting_info"] = None
            if pending_intent_op == "SET":
                wm["pending_intent"] = pending_intent
            elif pending_intent_op == "CLEAR":
                wm["pending_intent"] = None
            if pending_entities_op == "SET":
                wm["pending_entities"] = pending_entities
            elif pending_entities_op == "CLEAR":
                wm["pending_entities"] = None
        elapsed = (time.monotonic() - start) * 1000
        record_redis_op(session_id, "update_working_memory_fallback", elapsed)
        return

    sha = _registered_scripts.get("update_working_memory")
    wai = json.dumps(waiting_info) if waiting_info is not None else ""
    pen = pending_intent if pending_intent is not None else ""
    pent = json.dumps(pending_entities) if pending_entities is not None else ""
    if sha:
        client.evalsha(sha, 1, _key(session_id),
                       waiting_info_op, wai,
                       pending_intent_op, pen,
                       pending_entities_op, pent,
                       _SESSION_TTL)
    else:
        client.eval(_LUA_UPDATE_WORKING_MEMORY, 1, _key(session_id),
                    waiting_info_op, wai,
                    pending_intent_op, pen,
                    pending_entities_op, pent,
                    _SESSION_TTL)
    elapsed = (time.monotonic() - start) * 1000
    record_redis_op(session_id, "update_working_memory", elapsed, client)


def trim_and_summarize(session_id: str, max_turns: int = 10) -> None:
    """
    裁剪 recent_turns 到 max_turns 条。原子操作。
    """
    client = _get_redis_client()
    start = time.monotonic()

    if client is None:
        with _fallback_lock:
            memory = _fallback_store.get(session_id)
            if memory is None:
                elapsed = (time.monotonic() - start) * 1000
                record_redis_op(session_id, "trim_fallback", elapsed)
                return
            if len(memory["recent_turns"]) > max_turns:
                memory["recent_turns"] = memory["recent_turns"][-max_turns:]
        elapsed = (time.monotonic() - start) * 1000
        record_redis_op(session_id, "trim_fallback", elapsed)
        return

    sha = _registered_scripts.get("trim")
    if sha:
        client.evalsha(sha, 1, _key(session_id), max_turns, _SESSION_TTL)
    else:
        client.eval(_LUA_TRIM, 1, _key(session_id), max_turns, _SESSION_TTL)
    elapsed = (time.monotonic() - start) * 1000
    record_redis_op(session_id, "trim", elapsed, client)


def update_metadata(session_id: str, operation: str, batch_size: int = 10) -> None:
    """原子更新 metadata 字段。

    operation:
    - "increment_total_turns": metadata.total_turns += 1
    - "mark_batch_processed": metadata.long_memory_batch_processed = total_turns // batch_size
    """
    client = _get_redis_client()
    start = time.monotonic()

    if client is None:
        with _fallback_lock:
            memory = _fallback_store.get(session_id, None)
            if memory is None:
                memory = _empty_memory()
                _fallback_store[session_id] = memory
            if "metadata" not in memory:
                memory["metadata"] = {}
            if operation == "increment_total_turns":
                memory["metadata"]["total_turns"] = memory["metadata"].get("total_turns", 0) + 1
            elif operation == "mark_batch_processed":
                total_turns = memory["metadata"].get("total_turns", 0)
                memory["metadata"]["long_memory_batch_processed"] = total_turns // batch_size
        elapsed = (time.monotonic() - start) * 1000
        record_redis_op(session_id, "update_metadata_fallback", elapsed)
        return

    sha = _registered_scripts.get("update_metadata")
    if sha:
        client.evalsha(sha, 1, _key(session_id), operation, _SESSION_TTL, batch_size)
    else:
        client.eval(_LUA_UPDATE_METADATA, 1, _key(session_id), operation, _SESSION_TTL, batch_size)
    elapsed = (time.monotonic() - start) * 1000
    record_redis_op(session_id, "update_metadata", elapsed, client)


def retry_compress(session_id: str) -> dict[str, Any]:
    """
    重试压缩（兼容旧接口）。

    参数:
    - session_id: 会话标识

    返回:
    - 结果字典 { success: bool, summary: str | None, error: str | None }
    """
    backup = get_compress_backup(session_id)
    if backup is None:
        return {"success": False, "summary": None, "error": "No backup found"}

    # memory_summary 字段已删除：清除旧备份/标记并返回成功
    clear_compress_backup(session_id)
    clear_compress_failed(session_id)
    return {"success": True, "summary": "", "error": None}


def clear_session_memory(session_id: str) -> None:
    """删除指定 session 的全部记忆数据（包括备份）"""
    client = _get_redis_client()

    if client is None:
        _fallback_store.pop(session_id, None)
        _fallback_store.pop(f"_backup_{session_id}", None)
        _fallback_store.pop(f"_compress_failed_{session_id}", None)
        return

    client.delete(_key(session_id))
    client.delete(_backup_key(session_id))
    client.delete(_flag_key(session_id))


def get_recent_turns(session_id: str) -> list[dict[str, str]]:
    """获取最近对话轮次（不含 summary）。"""
    memory = _get_memory_impl(session_id)
    return memory.get("recent_turns", [])


def get_working_memory(session_id: str) -> dict[str, Any]:
    """获取 working_memory（waiting_info / pending_intent / pending_entities）。"""
    memory = _get_memory_impl(session_id)
    return memory.get("working_memory", {})


def generate_and_store_session_token(session_id: str) -> str:
    """生成随机 session_token 并写入 Redis/fallback，返回 token。

    调用时机：新 session 创建时（get_or_create_session_id 中 session_id 为新生成）。
    token 通过 httponly Cookie 下发给客户端，作为后续请求的"会话密码"。
    """
    token = secrets.token_urlsafe(32)

    client = _get_redis_client()

    if client is None:
        with _fallback_lock:
            memory = _fallback_store.get(session_id)
            if memory is None:
                memory = _empty_memory()
                _fallback_store[session_id] = memory
            memory["session_token"] = token
        return token

    key = _key(session_id)
    data = client.get(key)
    if data:
        memory = json.loads(data)
    else:
        memory = _empty_memory()

    memory["session_token"] = token
    client.setex(key, _SESSION_TTL, json.dumps(memory))
    return token


def validate_session_token(session_id: str, token: str | None) -> None:
    """校验 session_token：与 Redis 中存储值比对。

    参数:
    - session_id: 会话标识
    - token: 客户端 Cookie 中的 session_token（可能为 None）

    抛出:
    - ValueError: token 为空、不匹配、或 session 尚未初始化 token
    """
    memory = _get_memory_impl(session_id)
    stored_token = memory.get("session_token")

    if stored_token is None:
        raise ValueError("Session token not initialized")

    if not token or not secrets_compare(token, stored_token):
        raise ValueError("Session token mismatch")


def secrets_compare(a: str, b: str) -> bool:
    """恒定时间字符串比较，防止时序攻击。"""
    return hmac.compare_digest(a.encode(), b.encode())


def has_session_token(session_id: str) -> bool:
    """检查 session 是否已初始化 token（旧会话可能没有）。"""
    memory = _get_memory_impl(session_id)
    return bool(memory.get("session_token"))
