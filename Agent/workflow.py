"""
Workout Agent LangGraph Workflow

主工作流（去掉 check_waiting）：
- 入口：intent_classifier
- 路由：按意图类型分发到各节点

子图（planning_node 内部循环）：
- extract → merge → check → [有缺失?] → [追问用户] → loop
- 用户回复后，本轮 workflow 重新触发 intent_classifier（带着新 user_input）
- planning_node 在同一轮次内累积 pending_entities，直到字段齐全才生成计划

短期记忆集成：
- run_workflow 前读取 session memory，注入 pending_entities
- run_workflow 后写回 user turn + assistant output + pending_entities
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
    from Agent.nodes.guidance import guidance_node
    from Agent.nodes.planning import planning_node
    from Agent.nodes.general_conversation_node import general_conversation_node
else:
    from .nodes.state import AgentState
    from .nodes.intent_classifier import intent_classifier_node
    from .nodes.guidance import guidance_node
    from .nodes.planning import planning_node
    from .nodes.diet_analysis_node import diet_analysis_node
    from .nodes.meal_planning_node import meal_planning_node
    from .nodes.general_conversation_node import general_conversation_node

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

# 长期记忆模块
from .memory.long_memory import update_if_needed, load_long_memory, render_markdown


def _route_by_intent(state: AgentState) -> Literal["guidance_node", "planning_node", "diet_analysis_node", "meal_planning_node", "general_conversation_node", "__end__"]:
    """
    参数:
    - state: 当前工作流状态
      来源: intent_classifier 节点输出后的 state

    输出:
    - "guidance_node" | "planning_node" | "diet_analysis_node" | "meal_planning_node" | "general_conversation_node" | "__end__"
      由 primary_intent 决定路由分支

    流向:
    - 作为 intent_classifier 的条件边函数，控制后续节点执行路径
    """
    primary_intent = state.get("primary_intent")

    if primary_intent == "training_guidance":
        return "guidance_node"
    elif primary_intent == "training_plan":
        return "planning_node"
    elif primary_intent == "diet_analysis":
        return "diet_analysis_node"
    elif primary_intent == "meal_planning":
        return "meal_planning_node"
    elif primary_intent == "general":
        return "general_conversation_node"
    else:
        return "__end__"


def _route_pending_or_intent(state: AgentState) -> str:
    """
    路由决策：
    - 如果 pending_intent 存在（多轮追问中），直接路由到对应节点，跳过 intent_classifier
    - 否则，走 intent_classifier → _route_by_intent 的普通路由

    输出:
    - 目标节点名
    """
    pending_intent = state.get("pending_intent")
    if pending_intent:
        if pending_intent == "training_plan":
            return "planning_node"
        elif pending_intent == "training_guidance":
            return "guidance_node"
        elif pending_intent == "diet_analysis":
            return "diet_analysis_node"
        elif pending_intent == "meal_planning":
            return "meal_planning_node"
        elif pending_intent == "general":
            return "general_conversation_node"

    # 无 pending_intent，走普通意图分类路由
    return _route_by_intent(state)


def _should_skip_intent_classifier(state: AgentState) -> bool:
    """如果 pending_intent 存在，跳过 intent_classifier，直接路由到 pending 节点"""
    return bool(state.get("pending_intent"))


def build_workflow():
    """
    参数:
    - 无
      来源: 由 get_workflow 在首次调用时触发

    输出:
    - 编译后的 LangGraph workflow 对象

    流向:
    - 缓存在模块级 _workflow_instance
    - 被 run_workflow 调用用于 invoke
    """
    workflow = StateGraph(AgentState)

    # 添加所有节点
    workflow.add_node("intent_classifier", intent_classifier_node)
    workflow.add_node("guidance_node", guidance_node)
    workflow.add_node("planning_node", planning_node)
    workflow.add_node("diet_analysis_node", diet_analysis_node)
    workflow.add_node("meal_planning_node", meal_planning_node)
    workflow.add_node("general_conversation_node", general_conversation_node)

    # 设置入口点：直接进入 intent_classifier
    workflow.set_entry_point("intent_classifier")

    # intent_classifier → 条件边：先检查 pending_intent，有则跳到对应节点
    workflow.add_conditional_edges(
        "intent_classifier",
        _route_pending_or_intent,
        {
            "guidance_node": "guidance_node",
            "planning_node": "planning_node",
            "diet_analysis_node": "diet_analysis_node",
            "meal_planning_node": "meal_planning_node",
            "general_conversation_node": "general_conversation_node",
            "__end__": END,
        }
    )

    # general_conversation_node → END
    workflow.add_edge("general_conversation_node", END)

    # planning_node → END
    workflow.add_edge("planning_node", END)

    # guidance_node → END
    workflow.add_edge("guidance_node", END)

    # diet_analysis_node → END
    workflow.add_edge("diet_analysis_node", END)

    # meal_planning_node → END
    workflow.add_edge("meal_planning_node", END)

    return workflow.compile()


# 单例 workflow
_workflow_instance = None


def get_workflow():
    """
    参数:
    - 无
      来源: run_workflow 每次调用都会请求

    输出:
    - workflow 实例（单例）

    流向:
    - 直接供 run_workflow.invoke 使用
    """
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

    参数:
    - user_input: 用户输入文本
      来源: API/CLI 上层调用方
    - profile: 用户画像（可选）
      来源: 调用方传入或用户资料系统
    - waiting_info: 等待补充信息状态（可选）
      来源: 上层显式传入；若为 None 则回退到 short-term memory
    - pending_intent: 待恢复意图（可选）
      来源: 上层显式传入；若为 None 则回退到 short-term memory
    - pending_entities: 待恢复实体（可选）
      来源: 上层显式传入；若为 None 则回退到 short-term memory
    - session_id: 会话标识
      来源: 调用方（用于隔离短期记忆）

    输出:
    - result(dict): workflow 最终状态中的核心业务输出
      典型字段: status / guidance / plan / follow_up_questions / waiting_info 等

    流向:
    - 直接返回给上层调用方（接口响应）
    - 同时 result 的关键字段被写回 session memory，影响同一 session 的下一轮
    """
    workflow = get_workflow()

    # -----------------------------------------------------------------
    # 1) 输入预处理层：从 memory 读取 working_memory（显式参数优先）
    #    数据来源: session_memory
    #    数据去向: initial_state.waiting_info/pending_*
    # -----------------------------------------------------------------
    working_memory = get_working_memory(session_id)

    if waiting_info is None:
        waiting_info = working_memory.get("waiting_info")
    if pending_intent is None:
        pending_intent = working_memory.get("pending_intent")
    if pending_entities is None:
        pending_entities = working_memory.get("pending_entities")

    # 2) 读取上下文短期记忆
    #    数据来源: session_memory(memory_summary/recent_turns)
    #    数据去向: initial_state.memory_summary/recent_turns
    memory_summary = get_memory_summary(session_id)
    recent_turns = get_recent_turns(session_id)

    # 读取长期记忆（markdown 格式，用于注入 prompt）
    long_memory_md = render_markdown(load_long_memory(session_id))

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
        "long_memory": long_memory_md,
        "retrieved_content": "",
        "guidance": "",
        "plan": {},
        "follow_up_questions": [],
        "status": "",
        "messages": [],
        "metadata": {},
    }

    # 3) 执行工作流
    #    输入: initial_state
    #    输出: result
    result = workflow.invoke(initial_state)

    # -----------------------------------------------------------------
    # 4) 结果写回层：写回短期记忆
    #    数据来源: user_input + result
    #    数据去向: session_memory(recent_turns/working_memory)
    # -----------------------------------------------------------------
    # 追加用户输入
    append_turn(session_id, "user", user_input)

    # 累计 total_turns（用于长期记忆触发判断）
    _increment_total_turns(session_id)

    # 追加助手输出摘要
    if result.get("guidance"):
        output_summary = result["guidance"][:100] + "..." if len(result["guidance"]) > 100 else result["guidance"]
        append_turn(session_id, "assistant", f"[guidance] {output_summary}")
        _increment_total_turns(session_id)
    elif result.get("plan"):
        plan_summary = f"[plan] {result['plan'].get('goal', '')} - {len(result['plan'].get('plan', []))} days"
        append_turn(session_id, "assistant", plan_summary)
        _increment_total_turns(session_id)
    elif result.get("analysis_result"):
        output_summary = result["analysis_result"][:100] + "..." if len(result["analysis_result"]) > 100 else result["analysis_result"]
        append_turn(session_id, "assistant", f"[diet_analysis] {output_summary}")
        _increment_total_turns(session_id)
    elif result.get("follow_up_questions"):
        append_turn(session_id, "assistant", f"[follow_up] {'; '.join(result['follow_up_questions'])}")
        _increment_total_turns(session_id)

    # 更新 working_memory
    # 规则：只有当节点显式返回非 None 值时才覆盖 working_memory
    # None 值表示"不更新"，保留旧值
    next_waiting_info = result.get("waiting_info")
    next_pending_intent = result.get("pending_intent")
    next_pending_entities = result.get("pending_entities")

    if next_waiting_info is None and "waiting_info" not in result:
        next_waiting_info = working_memory.get("waiting_info")
    if next_pending_intent is None and "pending_intent" not in result:
        next_pending_intent = working_memory.get("pending_intent")
    if next_pending_entities is None and "pending_entities" not in result:
        next_pending_entities = working_memory.get("pending_entities")

    update_working_memory(
        session_id,
        waiting_info=next_waiting_info,
        pending_intent=next_pending_intent,
        pending_entities=next_pending_entities,
    )

    # -----------------------------------------------------------------
    # 5) 记忆维护层：裁剪长度
    #    数据来源: session_memory.recent_turns
    #    数据去向: session_memory.memory_summary + 新 recent_turns
    # -----------------------------------------------------------------
    try:
        trim_and_summarize(session_id, max_turns=10)
    except RuntimeError as e:
        # 压缩失败，在 result 中标记，提示用户重试
        from .memory.session_memory import check_compress_status
        compress_status = check_compress_status(session_id)
        result["compress_retry_needed"] = compress_status["needs_retry"]
        if compress_status["needs_retry"]:
            result["compress_backup_info"] = compress_status["backup_info"]
            result["_compress_error"] = str(e)

    # -----------------------------------------------------------------
    # 6) 长期记忆更新层（每10轮触发一次）
    #    数据来源: session_memory.recent_turns + result.entities/intent
    #    数据去向: Agent/memory/long_term_store/{session_id}.md
    # -----------------------------------------------------------------
    recent_turns = get_recent_turns(session_id)
    latest_entities = result.get("entities") or result.get("pending_entities")
    latest_intent = result.get("primary_intent")

    lm_result = update_if_needed(session_id, recent_turns, latest_entities, latest_intent)
    if lm_result.get("triggered"):
        if lm_result.get("conflicts"):
            result["conflicts"] = lm_result["conflicts"]
            result["conflict_notice"] = lm_result.get("conflict_notice")
        if lm_result.get("warning"):
            if "metadata" not in result:
                result["metadata"] = {}
            result["metadata"]["long_memory_warning"] = lm_result["warning"]

    return result


def _increment_total_turns(session_id: str) -> None:
    """
    递增 session memory 中的 total_turns 计数器。
    该计数器用于长期记忆触发判断，不受 trim_and_summarize 影响。
    """
    from .memory import session_memory

    full = session_memory.get_session_memory(session_id)
    if "metadata" not in full:
        full["metadata"] = {}
    full["metadata"]["total_turns"] = full["metadata"].get("total_turns", 0) + 1
    session_memory._save_memory(session_id, full)


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
