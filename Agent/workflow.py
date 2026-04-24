"""
Workout Agent LangGraph Workflow

多轮状态机：
- 首次进入: check_waiting → intent_classifier → guidance/planning → END
- 追问后恢复: check_waiting → planning → END（外部存储状态，下轮继续）

短期记忆集成：
- run_workflow 前读取 session memory，注入 working_memory
- run_workflow 后写回 user turn + assistant output + working_memory
- 每次调用结束执行 trim_and_summarize 控制长度
"""
from typing import Literal
import sys
from pathlib import Path

try:
    from langgraph.graph import StateGraph, END
except ImportError:
    raise ImportError("langgraph is required. Install with: pip install langgraph")

if __package__ in (None, ""):
    # 兼容 `python Agent/workflow.py` 直接运行。
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))

    from Agent.nodes.state import AgentState
    from Agent.nodes.intent_classifier import intent_classifier_node
    from Agent.nodes.check_waiting import check_waiting_node
    from Agent.nodes.guidance import guidance_node
    from Agent.nodes.planning import planning_node
else:
    from .nodes.state import AgentState
    from .nodes.intent_classifier import intent_classifier_node
    from .nodes.check_waiting import check_waiting_node
    from .nodes.guidance import guidance_node
    from .nodes.planning import planning_node

# 短期记忆模块
from .memory import (
    get_session_memory,
    append_turn,
    update_working_memory,
    trim_and_summarize,
    get_recent_turns,
    get_memory_summary,
    get_working_memory,
)


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
            True: "planning_node",
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

    # planning_node → END
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
    pending_entities: dict | None = None,
    session_id: str = "default",
) -> dict:
    """
    运行工作流

    Args:
        user_input: 用户输入
        profile: 用户画像（可选）
        waiting_info: 追问状态（可选，显式传入优先于 memory）
        pending_intent: 上一轮意图（可选）
        pending_entities: 上一轮实体（可选）
        session_id: session 标识（默认 "default"）
    """
    workflow = get_workflow()

    # -----------------------------------------------------------------
    # 1. 从短期记忆读取 working_memory（若显式参数未提供则使用）
    # -----------------------------------------------------------------
    working_memory = get_working_memory(session_id)

    if waiting_info is None:
        waiting_info = working_memory.get("waiting_info")
    if pending_intent is None:
        pending_intent = working_memory.get("pending_intent")
    if pending_entities is None:
        pending_entities = working_memory.get("pending_entities")

    # 读取 memory summary 和 recent_turns（用于上下文）
    memory_summary = get_memory_summary(session_id)
    recent_turns = get_recent_turns(session_id)

    initial_state: AgentState = {
        "user_input": user_input,
        "profile": profile or {},
        "intents": [],
        "primary_intent": None,
        "entities": {},
        "waiting_info": waiting_info,
        "pending_intent": pending_intent,
        "pending_entities": pending_entities,
        "session_id": session_id,
        "memory_summary": memory_summary or None,
        "recent_turns": recent_turns or [],
        "retrieved_content": "",
        "guidance": "",
        "plan": {},
        "follow_up_questions": [],
        "status": "",
        "messages": [],
        "metadata": {},
    }

    result = workflow.invoke(initial_state)

    # -----------------------------------------------------------------
    # 2. 写回短期记忆
    # -----------------------------------------------------------------
    # 追加用户输入
    append_turn(session_id, "user", user_input)

    # 追加助手输出摘要
    if result.get("guidance"):
        output_summary = result["guidance"][:100] + "..." if len(result["guidance"]) > 100 else result["guidance"]
        append_turn(session_id, "assistant", f"[guidance] {output_summary}")
    elif result.get("plan"):
        plan_summary = f"[plan] {result['plan'].get('goal', '')} - {len(result['plan'].get('plan', []))} days"
        append_turn(session_id, "assistant", plan_summary)
    elif result.get("follow_up_questions"):
        append_turn(session_id, "assistant", f"[follow_up] {'; '.join(result['follow_up_questions'])}")

    # 更新 working_memory
    update_working_memory(
        session_id,
        waiting_info=result.get("waiting_info"),
        pending_intent=result.get("pending_intent"),
        pending_entities=result.get("pending_entities"),
    )

    # -----------------------------------------------------------------
    # 3. 裁剪长度
    # -----------------------------------------------------------------
    trim_and_summarize(session_id, max_turns=10)

    return result


if __name__ == "__main__":
    import json

    print("=== 单轮测试：动作指导 ===")
    result = run_workflow("深蹲怎么做，发力点在哪？", session_id="test_single")
    print(f"status: {result.get('status')}")
    print(f"source: {result.get('metadata', {}).get('source')}")
    print(f"guidance (前200字): {result.get('guidance', '')[:200]}")

    print("\n=== 单轮测试：完整训练计划 ===")
    result = run_workflow("我想增肌，一周训练4天，4周", session_id="test_single2")
    print(f"status: {result.get('status')}")
    print(f"plan_days: {[d.get('day') for d in result.get('plan', {}).get('plan', [])]}")

    print("\n=== 多轮测试：同一 session 追问恢复 ===")
    # 第1轮
    result = run_workflow("给我一个训练计划", session_id="test_multi")
    print(f"第1轮 - status: {result.get('status')}")
    print(f"  追问: {result.get('follow_up_questions')}")
    print(f"  waiting_info: {result.get('waiting_info')}")

    # 第2轮（不传 waiting_info，从 session memory 恢复）
    result = run_workflow("增肌，每周4天", session_id="test_multi")
    print(f"\n第2轮 - status: {result.get('status')}")
    if result.get("plan"):
        print(f"  plan_days: {[d.get('day') for d in result.get('plan', {}).get('plan', [])]}")
    else:
        print(f"  追问: {result.get('follow_up_questions')}")

    print("\n=== 不同 session 隔离测试 ===")
    # session A
    result_a = run_workflow("给我一个训练计划", session_id="session_a")
    print(f"session_a 第1轮 - waiting_info: {result_a.get('waiting_info')}")
    # session B（不应受 session A 影响）
    result_b = run_workflow("深蹲怎么做", session_id="session_b")
    print(f"session_b 第1轮 - status: {result_b.get('status')}, guidance 非空: {bool(result_b.get('guidance'))}")
