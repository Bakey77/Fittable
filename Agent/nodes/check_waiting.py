"""检查等待状态节点 - 多轮对话状态恢复"""
from typing import Any
from .state import AgentState


def check_waiting_node(state: AgentState) -> AgentState:
    """
    参数:
    - state: 当前工作流状态
      来源: workflow entry 初始状态，或上游节点传递
      关键读取字段:
      1) waiting_info
      2) pending_intent
      3) pending_entities

    输出:
    - AgentState 增量字段（两种情况）
      1) waiting 恢复场景:
         primary_intent: 从 pending_intent 恢复
         entities: 从 pending_entities + missing 字段补 None 合并
         waiting_info: 置空（避免重复跳过）
      2) 非 waiting 场景:
         返回 {}（不改写 state）

    流向:
    - 输出进入 workflow 条件路由 `_should_skip_intent_classifier`
    - 若恢复成功，通常会直接进入 planning_node
    - 若非恢复，进入 intent_classifier_node
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
