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


def _get_redis_client():
    global _redis_client, _redis_was_down

    if redis is None:
        return None

    client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
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
        "memory_summary": "",
    }


def _get_memory_impl(session_id: str) -> dict[str, Any]:
    """获取记忆结构（Redis优先，失败回退到内存）"""
    client = _get_redis_client()

    if client is None:
        if session_id not in _fallback_store:
            _fallback_store[session_id] = _empty_memory()
        return _fallback_store[session_id]

    key = _key(session_id)
    data = client.get(key)

    if data is None:
        memory = _empty_memory()
        client.setex(key, _SESSION_TTL, json.dumps(memory))
        return memory

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

def _llm_summarize(older_turns: list[dict[str, str]]) -> str:
    """
    调用 LLM 对旧对话进行语义压缩

    参数:
    - older_turns: 需要压缩的旧对话列表

    返回:
    - 压缩后的摘要字符串
    """
    from backend.services.llm import get_llm

    conversation_text = ""
    for turn in older_turns:
        role = "用户" if turn["role"] == "user" else "助手"
        conversation_text += f"{role}：{turn['text']}\n"

    prompt = f"""请对以下健身助手对话进行压缩摘要，保留关键信息（用户目标、偏好、已提取的实体、当前任务进度等）：

---
{conversation_text}
---

要求：
1. 用简洁的中文概括对话核心内容
2. 保留所有关键实体和参数（训练目标、频率、动作等）
3. 保留当前任务进度（如是否在等待用户补充信息）
4. 长度控制在200字以内
5. 直接输出摘要，不要额外的解释或格式标记
6. 不能编造或假设对话内容，只能根据对话记录生成摘要

摘要："""

    llm = get_llm()
    response = llm.invoke([{"role": "user", "content": prompt}])
    summary = response.content if hasattr(response, "content") else str(response)
    return summary.strip()


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
    """追加一轮对话到 recent_turns。role = "user" | "assistant"。"""
    memory = _get_memory_impl(session_id)
    memory["recent_turns"].append({"role": role, "text": text})
    _save_memory(session_id, memory)


def update_working_memory(
    session_id: str,
    waiting_info: dict | None,
    pending_intent: str | None,
    pending_entities: dict | None,
) -> None:
    """更新 working_memory（waiting_info / pending_intent / pending_entities）。"""
    memory = _get_memory_impl(session_id)
    memory["working_memory"] = {
        "waiting_info": waiting_info,
        "pending_intent": pending_intent,
        "pending_entities": pending_entities,
    }
    _save_memory(session_id, memory)


def trim_and_summarize(session_id: str, max_turns: int = 10) -> None:
    """
    裁剪 recent_turns 到 max_turns 条。

    注意：
    - 当前已禁用 memory_summary 内容沉淀。
    - 本函数只维护 recent_turns 窗口，不再调用 LLM 生成摘要。
    """
    memory = _get_memory_impl(session_id)
    turns = memory["recent_turns"]

    # 始终清空 memory_summary（兼容历史数据清理）
    if memory.get("memory_summary"):
        memory["memory_summary"] = ""

    if len(turns) <= max_turns:
        _save_memory(session_id, memory)
        return

    memory["recent_turns"] = turns[-max_turns:]
    _save_memory(session_id, memory)


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

    # memory_summary 已禁用：清除旧备份/标记并返回成功
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


def get_memory_summary(session_id: str) -> str:
    """
    获取历史摘要（当前固定返回空字符串）。
    同时清理历史遗留的 memory_summary 内容。
    """
    memory = _get_memory_impl(session_id)
    if memory.get("memory_summary"):
        memory["memory_summary"] = ""
        _save_memory(session_id, memory)
    return ""


def get_working_memory(session_id: str) -> dict[str, Any]:
    """获取 working_memory（waiting_info / pending_intent / pending_entities）。"""
    memory = _get_memory_impl(session_id)
    return memory.get("working_memory", {})
