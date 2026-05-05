"""意图分类节点"""
from typing import Any
from .state import AgentState


def intent_classifier_node(state: AgentState) -> AgentState:
    """
    参数:
    - state: 当前工作流状态
      来源: check_waiting_node 输出后的 state
      关键读取字段:
      1) user_input

    输出:
    - intents: 分类器返回的意图列表
    - primary_intent: 主意图类型（如 training_plan / training_guidance）
    - entities: 从训练相关意图中抽取的实体

    流向:
    - 输出进入 workflow 条件路由 `_route_by_intent`
    - 路由后流向 guidance_node / planning_node / END
    """
    from ..intent_classifier import classify_intent

    user_input = state["user_input"]
    classified = classify_intent(user_input)

    intents = classified.get("intents", [])
    primary = classified.get("primary_intent")
    primary_intent = primary.get("type") if isinstance(primary, dict) else None

    # 提取训练相关意图的实体
    entities: dict[str, Any] = {}
    for intent in intents:
        if intent.get("type") in ("training_plan", "training_guidance"):
            entities = intent.get("entities", {})
            break

    return {
        "intents": intents,
        "primary_intent": primary_intent,
        "entities": entities,
    }
