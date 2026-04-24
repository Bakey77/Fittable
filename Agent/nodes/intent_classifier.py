"""意图分类节点"""
from typing import Any
from .state import AgentState


def intent_classifier_node(state: AgentState) -> AgentState:
    """
    节点1: 意图分类

    仅在 waiting_info 为空时执行（首次对话或有完整上下文时）
    输入: user_input
    输出: intents, primary_intent, entities
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
