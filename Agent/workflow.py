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
import logging
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

logger = logging.getLogger(__name__)

# 短期记忆模块
from .memory import (
    get_session_memory,
    append_turn,
    update_working_memory,
    trim_and_summarize,
    update_metadata,
    get_recent_turns,
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
    print(f"[ROUTE] pending_intent={pending_intent!r}, primary_intent={state.get('primary_intent')!r}")
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


def _entry_router(state: AgentState) -> str:
    """入口路由：pending_intent 存在时跳过 intent_classifier，直接到目标节点。

    这避免了两个问题：
    1. 每次多轮追问都浪费一次 LLM 调用做意图分类
    2. 单字/短输入（如"2天"）被分类器误判为 general，覆盖 pending_intent 路由
    """
    pending_intent = state.get("pending_intent")
    chosen = "intent_classifier"
    if pending_intent:
        chosen = pending_intent
    print(f"[ENTRY-ROUTER] pending_intent={pending_intent!r} → routing to: {chosen}")
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
    return "intent_classifier"


# =============================================================================
# 多意图执行计划构建器
# =============================================================================
SECONDARY_CONFIDENCE_THRESHOLD = 0.5


def _build_execution_plan(state: AgentState) -> list[dict]:
    """
    根据意图分类结果构建有序执行计划。

    参数:
    - state: 含 primary_intent、intents 的 workflow state

    输出:
    - 有序执行计划数组:
      [
        {"intent": "training_plan", "role": "primary"},
        {"intent": "training_guidance", "role": "secondary"}
      ]
    规则:
    - secondary 只能来自 intents top2 中的第2个
    - secondary 置信度需 >= 0.5
    - secondary 与 primary 相同则去重
    - primary 为 general 时默认不执行 secondary
    """
    primary_intent = state.get("primary_intent")
    intents: list[dict] = state.get("intents", [])

    plan: list[dict] = []
    if not primary_intent:
        return plan

    plan.append({"intent": primary_intent, "role": "primary"})

    # 仅当 primary 不是 general 时才考虑 secondary
    if primary_intent == "general":
        return plan

    # 从 intents 中找第2个意图作为 secondary
    if len(intents) >= 2:
        candidate = intents[1]
        sec_type = candidate.get("type")
        sec_conf = candidate.get("confidence", 0)
        if (
            sec_type
            and sec_type != primary_intent
            and sec_conf >= SECONDARY_CONFIDENCE_THRESHOLD
        ):
            plan.append({"intent": sec_type, "role": "secondary"})

    return plan


# =============================================================================
# 节点输出标准化 Adapter
# =============================================================================
def _normalize_node_output(raw: dict, intent: str) -> dict:
    """
    将各节点的异构输出统一为标准格式，减少聚合器 if-else 拼接。

    输入: 节点原始返回 dict + 对应的 intent 类型
    输出: {
        "intent": str,
        "status": str,
        "payload": str,               # 主要文本输出
        "follow_up_questions": list,
        "waiting_info": dict | None,
        "pending_intent": str | None,
        "pending_entities": dict | None,
        "metadata": dict,
    }
    """
    normalized: dict = {
        "intent": intent,
        "status": raw.get("status", "success"),
        "payload": "",
        "follow_up_questions": raw.get("follow_up_questions") or [],
        "waiting_info": raw.get("waiting_info"),
        "pending_intent": raw.get("pending_intent"),
        "pending_entities": raw.get("pending_entities"),
        "metadata": raw.get("metadata") or {},
    }

    # 根据意图类型提取 payload
    if intent == "training_guidance":
        normalized["payload"] = raw.get("guidance", "")
    elif intent == "training_plan":
        normalized["payload"] = raw.get("plan", {})
    elif intent == "diet_analysis":
        normalized["payload"] = raw.get("analysis_result", "")
    elif intent == "meal_planning":
        normalized["payload"] = raw.get("analysis_result", "")
    elif intent == "general":
        normalized["payload"] = raw.get("general_response", "")

    return normalized


# intent → node function 映射（用于 secondary 直接调用）
_INTENT_NODE_MAP = {
    "training_guidance": guidance_node,
    "training_plan": planning_node,
    "diet_analysis": diet_analysis_node,
    "meal_planning": meal_planning_node,
    "general": general_conversation_node,
}


# =============================================================================
# 多意图响应聚合器
# =============================================================================
def _merge_multi_intent_outputs(
    primary_result: dict,
    secondary_result: dict | None,
    execution_plan: list[dict],
) -> dict:
    """
    合并主次意图输出，保持向后兼容。

    参数:
    - primary_result: 主意图节点的原始返回 dict（非标准化）
    - secondary_result: 次意图标准化输出 或 None
    - execution_plan: _build_execution_plan 的输出

    返回:
    - 合并后的 result dict，顶层字段保持主意图内容，
      新增 primary_output / secondary_output / multi_intent
    """
    if not secondary_result:
        # 无次意图：只加 multi_intent 标记，其余原样返回
        primary_result["primary_output"] = dict(primary_result)
        primary_result["secondary_output"] = None
        primary_result["multi_intent"] = {
            "enabled": False,
            "executed": [p["intent"] for p in execution_plan],
            "supplement_note": "",
        }
        return primary_result

    # 有次意图：构建多意图响应
    executed_intents = [p["intent"] for p in execution_plan]

    # 判断是否有 fallback（主意图失败）
    fallback_used = primary_result.get("status") not in ("success", "need_info") and bool(secondary_result.get("payload"))
    if fallback_used:
        if "metadata" not in primary_result:
            primary_result["metadata"] = {}
        primary_result["metadata"]["fallback_used"] = True

    # 次意图补充说明
    sec_intent = secondary_result["intent"]
    supplement_note = f"已补充执行次意图「{sec_intent}」，详见 secondary_output"

    # 如果次意图有 follow_up_questions，追加到主意图追问尾部
    sec_follow_ups = secondary_result.get("follow_up_questions") or []
    if sec_follow_ups:
        existing_follow_ups = primary_result.get("follow_up_questions") or []
        primary_result["follow_up_questions"] = list(existing_follow_ups) + [
            f"[{sec_intent}] {q}" for q in sec_follow_ups
        ]

    primary_result["primary_output"] = dict(primary_result)
    primary_result["secondary_output"] = dict(secondary_result)
    primary_result["multi_intent"] = {
        "enabled": True,
        "executed": executed_intents,
        "supplement_note": supplement_note,
    }

    return primary_result


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

    # 条件入口：有 pending_intent 时跳过意图分类器，直达目标节点
    workflow.set_conditional_entry_point(
        _entry_router,
        {
            "planning_node": "planning_node",
            "guidance_node": "guidance_node",
            "diet_analysis_node": "diet_analysis_node",
            "meal_planning_node": "meal_planning_node",
            "general_conversation_node": "general_conversation_node",
            "intent_classifier": "intent_classifier",
        }
    )

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
    print(f"[MEMORY-READ] session={session_id} pending_intent={working_memory.get('pending_intent')!r} pending_entities={working_memory.get('pending_entities')!r}")

    if waiting_info is None:
        waiting_info = working_memory.get("waiting_info")
    if pending_intent is None:
        pending_intent = working_memory.get("pending_intent")
    if pending_entities is None:
        pending_entities = working_memory.get("pending_entities")

    # 2) 读取上下文短期记忆
    #    数据来源: session_memory(recent_turns)
    #    数据去向: initial_state.recent_turns
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

    # 3) 执行工作流（主意图）
    #    输入: initial_state
    #    输出: result
    result = workflow.invoke(initial_state)

    # -----------------------------------------------------------------
    # 3.5) 多意图并行执行：主意图完成后，条件执行次意图
    # -----------------------------------------------------------------
    execution_plan = _build_execution_plan(result)
    secondary_result = None
    secondary_node_raw = None

    if len(execution_plan) >= 2:
        sec_intent = execution_plan[1]["intent"]
        sec_node_fn = _INTENT_NODE_MAP.get(sec_intent)

        if sec_node_fn:
            primary_status = result.get("status", "")

            # need_info: 跳过次意图，避免上下文分裂
            if primary_status == "need_info":
                print(f"[MULTI-INTENT] 主意图 {result.get('primary_intent')} 需要追问，跳过次意图 {sec_intent}")

            elif primary_status == "success":
                # 构建次意图的上下文 state
                sec_state = dict(initial_state)
                sec_state["primary_intent"] = sec_intent
                # 次意图不继承 pending 状态，防止污染主流程
                sec_state["pending_intent"] = None
                sec_state["pending_entities"] = None
                sec_state["waiting_info"] = None
                try:
                    secondary_node_raw = sec_node_fn(sec_state)
                    secondary_result = _normalize_node_output(secondary_node_raw, sec_intent)
                    # 清除次意图返回的 pending/waiting，不污染主流程状态机
                    secondary_result["pending_intent"] = None
                    secondary_result["pending_entities"] = None
                    secondary_result["waiting_info"] = None
                    print(f"[MULTI-INTENT] 次意图 {sec_intent} 执行成功")
                except Exception as e:
                    print(f"[MULTI-INTENT] 次意图 {sec_intent} 执行失败: {e}")
                    secondary_result = {
                        "intent": sec_intent,
                        "status": "error",
                        "payload": "",
                        "follow_up_questions": [],
                        "waiting_info": None,
                        "pending_intent": None,
                        "pending_entities": None,
                        "metadata": {"error": str(e)},
                    }

            else:
                # primary status 为 error 或其他：降级尝试次意图
                print(f"[MULTI-INTENT] 主意图 {result.get('primary_intent')} status={primary_status}，降级尝试次意图 {sec_intent}")
                sec_state = dict(initial_state)
                sec_state["primary_intent"] = sec_intent
                sec_state["pending_intent"] = None
                sec_state["pending_entities"] = None
                sec_state["waiting_info"] = None
                try:
                    secondary_node_raw = sec_node_fn(sec_state)
                    secondary_result = _normalize_node_output(secondary_node_raw, sec_intent)
                    secondary_result["pending_intent"] = None
                    secondary_result["pending_entities"] = None
                    secondary_result["waiting_info"] = None
                    # fallback 标记在 _merge_multi_intent_outputs 中设置
                except Exception as e:
                    print(f"[MULTI-INTENT] 次意图降级也失败: {e}")
                    secondary_result = None

    # 合并主次意图输出（始终执行，单意图也需设置 multi_intent 标记）
    result = _merge_multi_intent_outputs(result, secondary_result, execution_plan)

    # -----------------------------------------------------------------
    # 4) 结果写回层：写回短期记忆
    #    数据来源: user_input + result（仅主意图的 pending/waiting 字段）
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

    # 次意图摘要（可选，简短）
    if result.get("multi_intent", {}).get("enabled") and result.get("secondary_output"):
        sec = result["secondary_output"]
        sec_intent = sec.get("intent", "")
        sec_payload = sec.get("payload", "")
        if isinstance(sec_payload, str) and sec_payload:
            sec_summary = sec_payload[:80] + "..." if len(sec_payload) > 80 else sec_payload
            append_turn(session_id, "assistant", f"[secondary:{sec_intent}] {sec_summary}")
            _increment_total_turns(session_id)

    # 更新 working_memory（字段级 KEEP/CLEAR/SET，并发安全）
    # KEEP: 节点未返回该字段 → Lua 内保留 Redis 当前值
    # CLEAR: 节点显式返回 None → 清空
    # SET: 节点返回非 None 值 → 写入新值
    def _op(key: str) -> tuple[str, object]:
        if key not in result:
            return "KEEP", None
        val = result[key]
        return ("CLEAR", None) if val is None else ("SET", val)

    wi_op, wi_val = _op("waiting_info")
    pi_op, pi_val = _op("pending_intent")
    pe_op, pe_val = _op("pending_entities")

    print(f"[MEMORY-WRITE] session={session_id} pi_op={pi_op} pi_val={pi_val!r} pe_op={pe_op} pe_val_keys={list(pe_val.keys()) if isinstance(pe_val, dict) else pe_val!r}")

    update_working_memory(
        session_id,
        waiting_info_op=wi_op,
        waiting_info=wi_val,
        pending_intent_op=pi_op,
        pending_intent=pi_val,
        pending_entities_op=pe_op,
        pending_entities=pe_val,
    )

    # -----------------------------------------------------------------
    # 5) 记忆维护层：裁剪长度
    #    数据来源: session_memory.recent_turns
    #    数据去向: session_memory.recent_turns
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
        print(f"[LONG_MEMORY] triggered session={session_id} success={lm_result.get('success')} warning={lm_result.get('warning')!r}")
        if not lm_result.get("success"):
            logger.error(f"[LONG_MEMORY] Update FAILED for session={session_id}: warning={lm_result.get('warning')}")
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
    递增 session memory 中的 total_turns 计数器（原子操作）。
    该计数器用于长期记忆触发判断，不受 trim_and_summarize 影响。
    """
    update_metadata(session_id, "increment_total_turns")


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
