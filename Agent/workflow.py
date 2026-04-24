"""
Workout Agent LangGraph Workflow

多轮状态机：
- 首次进入: check_waiting → intent_classifier → router → guidance/planning → END
- 追问后恢复: check_waiting → planning → END（外部存储状态，下轮继续）
"""
from typing import Literal

try:
    from langgraph.graph import StateGraph, END
except ImportError:
    raise ImportError("langgraph is required. Install with: pip install langgraph")

from .nodes.state import AgentState
from .nodes.intent_classifier import intent_classifier_node
from .nodes.check_waiting import check_waiting_node
from .nodes.guidance import guidance_node
from .nodes.planning import planning_node


def _should_skip_intent_classifier(state: AgentState) -> bool:
    """判断是否应该跳过 intent_classifier（处于 waiting 恢复状态）"""
    waiting_info = state.get("waiting_info")
    if waiting_info and waiting_info.get("missing"):
        return True
    return False


def _route_by_intent(state: AgentState) -> Literal["guidance_node", "planning_node", "__end__"]:
    """根据意图路由"""
    primary_intent = state.get("primary_intent")

    if primary_intent == "training_guidance":
        return "guidance_node"
    elif primary_intent == "training_plan":
        return "planning_node"
    else:
        return "__end__"


def build_workflow():
    """构建工作流图"""
    workflow = StateGraph(AgentState)

    # 添加所有节点
    workflow.add_node("check_waiting", check_waiting_node)
    workflow.add_node("intent_classifier", intent_classifier_node)
    workflow.add_node("guidance_node", guidance_node)
    workflow.add_node("planning_node", planning_node)

    # 设置入口点
    workflow.set_entry_point("check_waiting")

    # check_waiting → 条件边：有 waiting_info 跳过 intent_classifier
    workflow.add_conditional_edges(
        "check_waiting",
        _should_skip_intent_classifier,
        {
            True: "planning_node",  # 跳过 intent_classifier，直接进入 planning
            False: "intent_classifier",
        }
    )

    # intent_classifier → 条件边：根据意图路由
    workflow.add_conditional_edges(
        "intent_classifier",
        _route_by_intent,
        {
            "guidance_node": "guidance_node",
            "planning_node": "planning_node",
            "__end__": END,
        }
    )

    # planning_node → END（追问不循环，等待外部调用）
    workflow.add_edge("planning_node", END)

    # guidance_node → END
    workflow.add_edge("guidance_node", END)

    return workflow.compile()


# 单例 workflow
_workflow_instance = None


def get_workflow():
    global _workflow_instance
    if _workflow_instance is None:
        _workflow_instance = build_workflow()
    return _workflow_instance


def run_workflow(
    user_input: str,
    profile: dict | None = None,
    waiting_info: dict | None = None,
    pending_intent: str | None = None,
    pending_entities: dict | None = None
) -> dict:
    """
    运行工作流

    Args:
        user_input: 用户输入
        profile: 用户画像（可选）
        waiting_info: 追问状态（可选，用于多轮对话）
        pending_intent: 上一轮意图（可选）
        pending_entities: 上一轮实体（可选）
    """
    workflow = get_workflow()

    initial_state: AgentState = {
        "user_input": user_input,
        "profile": profile or {},
        "intents": [],
        "primary_intent": None,
        "entities": {},
        "waiting_info": waiting_info,
        "pending_intent": pending_intent,
        "pending_entities": pending_entities,
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

    print("=== 单轮测试：动作指导 ===")
    result = run_workflow("深蹲怎么做，发力点在哪？")
    print(f"status: {result.get('status')}")
    print(f"source: {result.get('metadata', {}).get('source')}")
    print(f"guidance (前300字): {result.get('guidance', '')[:300]}")

    print("\n=== 单轮测试：完整训练计划 ===")
    result = run_workflow("我想增肌，一周训练4天，4周")
    print(f"status: {result.get('status')}")
    print(f"plan_days: {[d.get('day') for d in result.get('plan', {}).get('plan', [])]}")

    print("\n=== 多轮测试：追问 ===")
    # 第1轮
    result = run_workflow("给我一个训练计划")
    print(f"第1轮 - status: {result.get('status')}")
    print(f"  追问: {result.get('follow_up_questions')}")
    print(f"  waiting_info: {result.get('waiting_info')}")

    waiting_info = result.get("waiting_info")
    pending_intent = result.get("pending_intent")
    pending_entities = result.get("pending_entities")

    # 第2轮（恢复状态，用户补充信息）
    result = run_workflow(
        "增肌，每周4天",
        waiting_info=waiting_info,
        pending_intent=pending_intent,
        pending_entities=pending_entities
    )
    print(f"\n第2轮 - status: {result.get('status')}")
    if result.get("plan"):
        print(f"  plan_days: {[d.get('day') for d in result.get('plan', {}).get('plan', [])]}")
    else:
        print(f"  追问: {result.get('follow_up_questions')}")
