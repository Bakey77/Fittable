"""检查等待状态节点 - 多轮对话状态恢复"""
from typing import Any
from .state import AgentState


def check_waiting_node(state: AgentState) -> AgentState:
    """
    节点0: 检查等待状态

    如果 waiting_info 有值（上一轮追问了用户），说明需要跳过意图分类，
    直接用 pending_intent 和 pending_entities 继续处理

    输入: waiting_info, pending_intent, pending_entities
    输出: primary_intent, entities, (清空 waiting_info)
    """
    waiting_info = state.get("waiting_info")

    if waiting_info and waiting_info.get("missing"):
        # 有缺失信息，跳过 intent_classifier
        # 从 waiting_info 恢复缺失的实体字段（用 None 填充）
        pending_entities = state.get("pending_entities") or {}
        missing_fields = waiting_info.get("missing", [])

        # 合并：已有实体 + 缺失字段（值为 None，等待用户补充）
        merged_entities = {**pending_entities}
        for field in missing_fields:
            if field not in merged_entities:
                merged_entities[field] = None

        return {
            "primary_intent": state.get("pending_intent"),
            "entities": merged_entities,
            "waiting_info": None,  # 清空，本轮处理完后会重新设置
        }

    # 没有 waiting_info，正常流程
    return {}
