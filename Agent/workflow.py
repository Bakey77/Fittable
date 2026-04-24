"""
Workout Agent LangGraph Workflow

意图分类 → [路由] → 动作指导 / 训练计划 → 响应构建
"""
from typing import Literal

try:
    from langgraph.graph import StateGraph, END
except ImportError:
    raise ImportError("langgraph is required. Install with: pip install langgraph")

from .nodes.state import AgentState
from .nodes.intent_classifier import intent_classifier_node
from .nodes.guidance import guidance_node
from .nodes.planning import planning_node


def _route_by_intent(state: AgentState) -> Literal["guidance_node", "planning_node"]:
    """
    路由函数：根据意图类型路由到对应处理节点
    """
    primary_intent = state.get("primary_intent")

    if primary_intent == "training_guidance":
        return "guidance_node"
    elif primary_intent == "training_plan":
        return "planning_node"
    else:
        # 没有匹配的意图，直接结束
        return "__end__"


def build_workflow():
    """构建工作流图"""
    workflow = StateGraph(AgentState)

    # 添加节点
    workflow.add_node("intent_classifier", intent_classifier_node)
    workflow.add_node("guidance_node", guidance_node)
    workflow.add_node("planning_node", planning_node)

    # 设置入口点
    workflow.set_entry_point("intent_classifier")

    # 条件边：intent_classifier -> 根据意图路由
    workflow.add_conditional_edges(
        "intent_classifier",
        _route_by_intent,
        {
            "guidance_node": "guidance_node",
            "planning_node": "planning_node",
            "__end__": END,
        }
    )

    # 处理节点 -> 结束
    workflow.add_edge("guidance_node", END)
    workflow.add_edge("planning_node", END)

    return workflow.compile()


# 单例 workflow
_workflow_instance = None


def get_workflow():
    global _workflow_instance
    if _workflow_instance is None:
        _workflow_instance = build_workflow()
    return _workflow_instance


def run_workflow(user_input: str, profile: dict | None = None) -> dict:
    """
    运行工作流

    Args:
        user_input: 用户输入
        profile: 用户画像（可选）

    Returns:
        工作流执行结果
    """
    workflow = get_workflow()

    initial_state: AgentState = {
        "user_input": user_input,
        "profile": profile or {},
        "intents": [],
        "primary_intent": None,
        "entities": {},
        "retrieved_content": "",
        "guidance": "",
        "plan": {},
        "follow_up_questions": [],
        "status": "",
        "messages": [],
        "metadata": {},
    }

    result = workflow.invoke(initial_state)
    return result


if __name__ == "__main__":
    import json

    # 测试
    result = run_workflow("深蹲怎么做，发力点在哪？")
    print(json.dumps(result, ensure_ascii=False, indent=2))
