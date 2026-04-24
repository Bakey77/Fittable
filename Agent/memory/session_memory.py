"""
短期记忆模块（Session Memory）
进程内存实现，不依赖外部存储。
"""
from typing import Any

# ---------------------------------------------------------------------------
# 进程内存储（生产环境建议替换为 Redis/DB）
# 结构: {
#   "session_id": {
#       "recent_turns": [{"role": "user", "text": "..."}, ...],
#       "working_memory": {
#           "waiting_info": {...} | None,
#           "pending_intent": str | None,
#           "pending_entities": {...} | None,
#       },
#       "memory_summary": "...",
#   }
# }
# ---------------------------------------------------------------------------
_store: dict[str, dict[str, Any]] = {}


def get_session_memory(session_id: str) -> dict[str, Any]:
    """获取 session 的完整记忆结构，不存在则返回空结构。"""
    if session_id not in _store:
        _store[session_id] = {
            "recent_turns": [],
            "working_memory": {
                "waiting_info": None,
                "pending_intent": None,
                "pending_entities": None,
            },
            "memory_summary": "",
        }
    return _store[session_id]


def append_turn(session_id: str, role: str, text: str) -> None:
    """追加一轮对话到 recent_turns。role = "user" | "assistant"。"""
    memory = get_session_memory(session_id)
    memory["recent_turns"].append({"role": role, "text": text})


def update_working_memory(
    session_id: str,
    waiting_info: dict | None,
    pending_intent: str | None,
    pending_entities: dict | None,
) -> None:
    """更新 working_memory（waiting_info / pending_intent / pending_entities）。"""
    memory = get_session_memory(session_id)
    memory["working_memory"] = {
        "waiting_info": waiting_info,
        "pending_intent": pending_intent,
        "pending_entities": pending_entities,
    }


def trim_and_summarize(session_id: str, max_turns: int = 10) -> None:
    """
    裁剪 recent_turns 至 max_turns 条，超出部分合并为 summary 字符串。
    不依赖 LLM，采用规则拼接。
    """
    memory = get_session_memory(session_id)
    turns = memory["recent_turns"]

    if len(turns) <= max_turns:
        return

    # 保留最近 max_turns 条
    recent = turns[-max_turns:]
    # 更早的内容合并为 summary
    older = turns[:-max_turns]

    # 简单摘要模板
    summary_parts = []
    for turn in older:
        role_label = "用户" if turn["role"] == "user" else "助手"
        summary_parts.append(f"{role_label}: {turn['text'][:50]}{'...' if len(turn['text']) > 50 else ''}")

    memory["memory_summary"] = f"[历史摘要（{len(older)} 轮）] " + " | ".join(summary_parts)
    memory["recent_turns"] = recent


def clear_session_memory(session_id: str) -> None:
    """清空指定 session 的全部记忆。"""
    if session_id in _store:
        del _store[session_id]


def get_recent_turns(session_id: str) -> list[dict[str, str]]:
    """获取最近对话轮次（不含 summary）。"""
    return get_session_memory(session_id).get("recent_turns", [])


def get_memory_summary(session_id: str) -> str:
    """获取历史摘要。"""
    return get_session_memory(session_id).get("memory_summary", "")


def get_working_memory(session_id: str) -> dict[str, Any]:
    """获取 working_memory（waiting_info / pending_intent / pending_entities）。"""
    return get_session_memory(session_id).get("working_memory", {})
