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
import re
from pathlib import Path
import uuid
import time as time_module

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
    from Agent.nodes.memory_explain_node import memory_explain_node
else:
    from .nodes.state import AgentState
    from .nodes.intent_classifier import intent_classifier_node
    from .nodes.guidance import guidance_node
    from .nodes.planning import planning_node
    from .nodes.diet_analysis_node import diet_analysis_node
    from .nodes.meal_planning_node import meal_planning_node
    from .nodes.general_conversation_node import general_conversation_node
    from .nodes.memory_explain_node import memory_explain_node

from Agent.memory.long_memory import should_update_long_memory
from Agent.worker import enqueue_long_memory_update

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
from .memory.long_memory import load_long_memory, render_markdown


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
    路由决策（post-classifier 安全网）：
    - 如果 pending_intent 存在且应继续追问，直接路由到对应节点
    - 否则，走 _route_by_intent 的普通路由

    注意：_determine_route 已在入口处做了 pending lock，这里是二次安全网。
    """
    pending_intent = state.get("pending_intent")
    print(f"[ROUTE] pending_intent={pending_intent!r}, primary_intent={state.get('primary_intent')!r}")
    if pending_intent and _should_continue_pending_slot(state):
        return _pending_intent_to_node(pending_intent)

    # 无 pending_intent，走普通意图分类路由
    return _route_by_intent(state)


def _pending_intent_to_node(pending_intent: str) -> str:
    """将 pending_intent 映射为目标节点名。"""
    mapping = {
        "training_plan": "planning_node",
        "training_guidance": "guidance_node",
        "diet_analysis": "diet_analysis_node",
        "meal_planning": "meal_planning_node",
        "general": "general_conversation_node",
    }
    return mapping.get(pending_intent, "intent_classifier")


def _determine_route(state: AgentState) -> str:
    """
    入口路由决策（严格优先级顺序）：

    1. Pending State Lock（追问态锁定 — 绝对优先）
       → 只要 pending_intent 存在且 _should_continue_pending_slot，必须锁定到追问节点
       → 跳过所有 regex / evidence / followup / LLM 分类
    2. Strong Regex Match（强触发正则）
       → 命中 ROUTE_PATTERNS 中的正则 → 直接路由
    3. Evidence Inquiry（依据追问）
       → 命中依据追问语义 → memory_explain_node
    4. Follow-up Confirmation（跟进确认）
       → 命中确认语义 + previous_intent 存在 → 继承 previous_intent
    5. LLM Classification（最终兜底）
       → 前述规则均未命中 → intent_classifier
    """
    from .routing import (
        _regex_route,
        _is_evidence_inquiry,
        _is_followup_confirm,
        _get_previous_intent,
        log_route_decision,
    )

    pending_intent = state.get("pending_intent")
    user_input = (state.get("user_input") or "").strip()
    session_id = state.get("session_id", "unknown")

    # ---- Priority 1: Pending State Lock (ABSOLUTE) ----
    # 处于追问态时，绝对锁定当前意图，不允许被任何 regex/confirm/LLM 覆盖
    if pending_intent and _should_continue_pending_slot(state):
        node = _pending_intent_to_node(pending_intent)
        log_route_decision(session_id, user_input, pending_intent, "pending_lock", 1.0)
        print(f"[ROUTE] pending_lock → {node}")
        return node

    # ---- Priority 2: Strong Regex Match ----
    regex_intent, matched_pat = _regex_route(user_input)
    if regex_intent:
        from .routing import intent_to_node
        node = intent_to_node(regex_intent)
        log_route_decision(session_id, user_input, regex_intent, f"regex_hit:{matched_pat}", 0.95)
        print(f"[ROUTE] regex_hit intent={regex_intent} pat={matched_pat!r} → {node}")
        return node

    # ---- Priority 3: Evidence Inquiry ----
    if _is_evidence_inquiry(user_input):
        log_route_decision(session_id, user_input, "memory_explain", "evidence_inquiry", 0.90)
        print(f"[ROUTE] evidence_inquiry → memory_explain_node")
        return "memory_explain_node"

    # ---- Priority 4: Follow-up Confirmation ----
    previous_intent = _get_previous_intent(session_id)
    if previous_intent and _is_followup_confirm(user_input):
        from .routing import intent_to_node
        node = intent_to_node(previous_intent)
        log_route_decision(session_id, user_input, previous_intent, f"followup_inherit:prev={previous_intent}", 0.85)
        print(f"[ROUTE] followup_inherit prev={previous_intent} → {node}")
        return node

    # ---- Priority 5: LLM Classification (LAST RESORT) ----
    log_route_decision(session_id, user_input, "intent_classifier", "llm", 0.0)
    print(f"[ROUTE] llm_classify → intent_classifier")
    return "intent_classifier"


def _should_continue_pending_slot(state: AgentState) -> bool:
    """
    仅当用户输入看起来是在补充缺失字段时，才继续 pending slot-filling。
    避免“我有什么训练限制”这类新问题被误当成上一轮追问的续答。
    """
    pending_intent = state.get("pending_intent")
    if not pending_intent:
        return False

    # 当前仅对 training_plan 的补槽做严格限制；其他 pending 保持原行为。
    if pending_intent != "training_plan":
        return True

    waiting_info = state.get("waiting_info") or {}
    missing_fields = waiting_info.get("missing") or []
    if not missing_fields:
        return True

    user_input = (state.get("user_input") or "").strip()
    if not user_input:
        return False

    # 明显是在发起新的解释/回顾问题，而不是补充 goal/frequency。
    diversion_patterns = [
        r"我有什么",
        r"有什么训练限制",
        r"按我的.*要注意什么",
        r"需要注意什么",
        r"器械条件",
        r"训练限制",
        r"为什么",
        r"怎么回事",
    ]
    if any(re.search(pattern, user_input) for pattern in diversion_patterns):
        return False

    if "frequency" in missing_fields:
        if re.search(r"(?:每周|一周)\s*(?:\d|[一二两三四五六七])\s*(?:练|天)", user_input):
            return True
    if "goal" in missing_fields:
        if re.search(r"(增肌|减脂|新手入门|新手)", user_input):
            return True

    # 其余情况保守处理：重新走分类，而不是强制续接旧追问。
    return False


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
    workflow.add_node("memory_explain_node", memory_explain_node)

    # 条件入口：5 级严格优先级路由
    workflow.set_conditional_entry_point(
        _determine_route,
        {
            "planning_node": "planning_node",
            "guidance_node": "guidance_node",
            "diet_analysis_node": "diet_analysis_node",
            "meal_planning_node": "meal_planning_node",
            "general_conversation_node": "general_conversation_node",
            "memory_explain_node": "memory_explain_node",
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

    # memory_explain_node → END
    workflow.add_edge("memory_explain_node", END)

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
    trace_id = str(uuid.uuid4())[:8]
    t_start = time_module.monotonic()
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
        "trace_id": trace_id,
        "retrieved_content": "",
        "relevant_events": [],  # 历史摘要检索结果
        "guidance": "",
        "plan": {},
        "follow_up_questions": [],
        "status": "",
        "messages": [],
        "metadata": {},
        "route_reason": "",
        "route_confidence": 0.0,
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

    # 3.6) 即时同步：将高价值长期事实直接写入 SQLite，
    #       避免等待批次 LLM 更新期间长期记忆继续显示旧值。
    _sync_high_value_memory_if_changed(user_input, session_id)

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

    # 写回本轮 executed_intent 为 next round 的 previous_intent
    executed_intent = result.get("primary_intent")
    if executed_intent:
        from .routing import _write_previous_intent
        _write_previous_intent(session_id, executed_intent)
        print(f"[LAST-INTENT-WRITE] session={session_id} last_intent={executed_intent}")

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



    if should_update_long_memory(session_id, recent_turns):
        snapshot = list(recent_turns[-10:])
        enqueue_long_memory_update(session_id, snapshot)

    elapsed = (time_module.monotonic() - t_start) * 1000
    logger.info(f"[trace={trace_id}] workflow done: {elapsed: .0f}ms,intent = {result.get('primary_intent')}")

    return result


def _sync_high_value_memory_if_changed(user_input: str, session_id: str) -> None:
    """
    检测用户输入中的高价值长期事实变更，并立即写入 SQLite。
    只处理规则可稳定识别的字段，批次 LLM 更新继续作为补全器保留。
    """
    import re
    try:
        from Agent.db import upsert_attribute, upsert_profile

        profile_updates: dict[str, str] = {}
        attribute_updates: list[tuple[str, str, str]] = []

        # 名字变更：只匹配简短中文名字，避免把问句疑问词写入档案。
        invalid_name_tokens = {"什么", "啥", "谁", "名字", "姓名"}
        is_name_question = bool(re.search(r"(什么|啥|谁|哪位|\?|？)", user_input)) and bool(
            re.search(r"(我叫|叫什么|名字|姓名)", user_input)
        )
        name_patterns = [
            r"我叫\s*([\u4e00-\u9fff]{1,3})(?:[，。,\.!！\s呢吗啊吧呀]|$)",
            r"我(?:的)?名字(?:现在|已经)?(?:叫|是|改成?[为了]?)\s*[：:]?\s*([\u4e00-\u9fff]{1,3})(?:[，。,\.!！\s呢吗啊吧呀]|$)",
            r"(?:叫我|called?)\s+([\u4e00-\u9fff]{1,3})(?:[，。,\.!！\s呢吗啊吧呀]|$)",
        ]
        if not is_name_question:
            for pat in name_patterns:
                m = re.search(pat, user_input)
                if m:
                    name = m.group(1).strip().rstrip("。，！,. ")
                    if name and len(name) <= 10 and name not in invalid_name_tokens:
                        profile_updates["name"] = name
                        break

        # 年龄变更："今年X岁" / "我X岁"
        age_m = re.search(r"(?:今年|我)\s*(\d{1,3})\s*岁", user_input)
        if age_m:
            profile_updates["age"] = f"{age_m.group(1)}岁"

        # 身高变更
        ht_m = re.search(r"(?:身高)\s*(\d{2,3})\s*(?:cm|厘米)?", user_input)
        if ht_m:
            profile_updates["height"] = f"{ht_m.group(1)}cm"

        # 体重变更
        wt_m = re.search(r"(?:体重)\s*(\d{2,3})\s*(?:kg|公斤)?", user_input)
        if wt_m:
            profile_updates["weight"] = f"{wt_m.group(1)}kg"

        # 目标变更：只在用户明确表达长期目标时更新。
        if re.search(r"(不想增肌了|现在要减脂|我要减脂|想减脂|目标是减脂)", user_input):
            profile_updates["goal"] = "减脂"
        elif re.search(r"(不想减脂了|现在要增肌|我要增肌|想增肌|目标是增肌)", user_input):
            profile_updates["goal"] = "增肌"
        elif re.search(r"(新手入门|刚开始健身|我是新手)", user_input):
            profile_updates["goal"] = "新手入门"

        # 性别变更
        gender_m = re.search(r"(?:我是|我)[\s]*(男生|男的|男性|女生|女的|女性)", user_input)
        if gender_m:
            g = gender_m.group(1)
            profile_updates["gender"] = "男" if g in ("男生", "男的", "男性") else "女"

        # 训练频率：覆盖当前 active_plan_facts.frequency
        freq = None
        freq_match = re.search(r"(?:每周|一周)\s*(\d)\s*(?:练|天)", user_input)
        if freq_match:
            freq = freq_match.group(1)
        else:
            cn_match = re.search(r"(?:每周|一周)\s*([一二两三四五六七])\s*(?:练|天)", user_input)
            if cn_match:
                cn_map = {"一": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7"}
                freq = cn_map.get(cn_match.group(1))
        if freq:
            attribute_updates.append(("active_plan_facts", "frequency", freq))

        # 约束：使用稳定 key，避免把原句写成属性名。
        constraint_patterns = [
            (r"(腿伤|腿受伤|腿部受伤)", "lower_body_injury", "是"),
            (r"(膝盖疼|膝盖痛|膝伤)", "knee_pain", "是"),
            (r"(不能做下肢负重|暂停下肢负重|避免下肢负重)", "avoid_lower_body_loading", "是"),
            (r"(不能练腿|别安排腿|避免练腿|不要练腿)", "avoid_leg_training", "是"),
            (r"(不能练胸|别安排胸|避免练胸|不要练胸)", "avoid_chest_training", "是"),
            (r"(不能练肩|别安排肩|避免练肩|不要练肩)", "avoid_shoulder_training", "是"),
            (r"(不能练背|别安排背|避免练背|不要练背)", "avoid_back_training", "是"),
            (r"(不能练手臂|别安排手臂|避免练手臂|不要练手臂)", "avoid_arm_training", "是"),
            (r"(不能熬夜训练|晚上太晚不能训练|不适合夜训)", "avoid_late_night_training", "是"),
            (r"(早上不能训练|不适合晨练)", "avoid_early_morning_training", "是"),
        ]
        for pattern, key, value in constraint_patterns:
            if re.search(pattern, user_input):
                attribute_updates.append(("constraints", key, value))

        time_limit_match = re.search(r"(?:每次|单次)训练(?:时间)?(?:最多|只能|控制在)?\s*(\d{1,3})\s*分钟", user_input)
        if time_limit_match:
            attribute_updates.append(("constraints", "session_time_limit_minutes", time_limit_match.group(1)))

        # 器械可用性：正负向都用稳定 key 存，便于计划生成时直接消费。
        equipment_patterns = [
            (r"(没有器械|徒手训练|只能徒手)", "constraints", "no_equipment_only", "是"),
            (r"(没健身房|没有健身房|不能去健身房)", "constraints", "gym_access", "否"),
            (r"(有健身房|可以去健身房|在健身房练)", "constraints", "gym_access", "是"),
            (r"(没哑铃|没有哑铃|不能用哑铃)", "constraints", "available_dumbbells", "否"),
            (r"(有哑铃|可以用哑铃)", "constraints", "available_dumbbells", "是"),
            (r"(没杠铃|没有杠铃|不能用杠铃)", "constraints", "available_barbell", "否"),
            (r"(有杠铃|可以用杠铃)", "constraints", "available_barbell", "是"),
            (r"(没弹力带|没有弹力带|不能用弹力带)", "constraints", "available_resistance_bands", "否"),
            (r"(有弹力带|可以用弹力带)", "constraints", "available_resistance_bands", "是"),
            (r"(没跑步机|没有跑步机|不能用跑步机)", "constraints", "available_treadmill", "否"),
            (r"(有跑步机|可以用跑步机)", "constraints", "available_treadmill", "是"),
        ]
        for pattern, category, key, value in equipment_patterns:
            if re.search(pattern, user_input):
                attribute_updates.append((category, key, value))

        # 作息约束：尽量只抓明确表达，避免误判。
        schedule_patterns = [
            (r"(只能早上训练|只能晨练|只能早晨训练)", "constraints", "preferred_training_time", "morning_only"),
            (r"(只能晚上训练|只能夜训|只能晚饭后训练)", "constraints", "preferred_training_time", "evening_only"),
            (r"(午休训练|中午训练)", "constraints", "preferred_training_time", "midday_only"),
            (r"(作息不规律|经常熬夜)", "constraints", "irregular_schedule", "是"),
            (r"(周末才能训练|只有周末能练)", "constraints", "weekend_only_training", "是"),
            (r"(工作日才能训练|只有工作日能练)", "constraints", "weekday_only_training", "是"),
        ]
        for pattern, category, key, value in schedule_patterns:
            if re.search(pattern, user_input):
                attribute_updates.append((category, key, value))

        # 偏好：优先用 LLM 检测，覆盖正则无法处理的表达（过敏、忌口、口语化等）
        food_prefs = _detect_food_preferences(user_input)
        for fp in food_prefs:
            attribute_updates.append(("preferences", fp["food"], fp["attitude"]))

        if profile_updates:
            upsert_profile(session_id, profile_updates)
            print(f"[HIGH-VALUE-SYNC] profile={profile_updates!r}")

        seen_attr_keys: set[tuple[str, str]] = set()
        for category, key, value in attribute_updates:
            dedupe_key = (category, key)
            if dedupe_key in seen_attr_keys:
                continue
            upsert_attribute(session_id, category, key, value)
            seen_attr_keys.add(dedupe_key)
            print(f"[HIGH-VALUE-SYNC] {category}.{key}={value!r}")

    except Exception as e:
        print(f"[HIGH-VALUE-SYNC] failed: {e}")


# ---------------------------------------------------------------------------
# 食物名称去重：将语义相同的食物合并
# ---------------------------------------------------------------------------

# 食物同义词组：每组第一个为规范名，后续为同义词
_FOOD_SYNONYM_CANONICAL_MAP: dict[str, str] = {}
for _canonical, *_synonyms in [
    ["米饭", "白米饭", "白饭", "大米饭"],
    ["鸡肉", "鸡胸肉", "鸡腿肉", "鸡翅", "鸡"],
    ["猪肉", "猪瘦肉", "瘦肉", "五花肉", "猪"],
    ["牛肉", "牛腩", "肥牛", "牛排", "牛"],
    ["羊肉", "羊排", "羊腿", "羊"],
    ["鸡蛋", "蛋", "鸡蛋白", "蛋清"],
    ["牛奶", "奶", "牛乳"],
    ["面条", "面", "拉面", "挂面", "白面"],
    ["面包", "吐司", "全麦面包"],
    ["鱼", "鱼肉", "鱼类"],
]:
    for _name in [_canonical] + _synonyms:
        _FOOD_SYNONYM_CANONICAL_MAP[_name] = _canonical


def _deduplicate_food_prefs(prefs: list[dict]) -> list[dict]:
    """
    对检测到的食物偏好做去重：
    1. 同义词组合并（如 米饭/白米饭 → 米饭）
    2. 相同 food 合并（保留先出现的 attitude）
    """
    if not prefs:
        return prefs

    merged: dict[str, str] = {}
    for item in prefs:
        food = str(item.get("food", "")).strip()
        attitude = str(item.get("attitude", "")).strip()
        if not food or not attitude:
            continue
        # 映射到规范名
        canonical = _FOOD_SYNONYM_CANONICAL_MAP.get(food, food)
        if canonical not in merged:
            merged[canonical] = attitude

    result = [{"food": k, "attitude": v} for k, v in merged.items()]
    if len(result) != len(prefs):
        print(f"[FOOD-PREF-DEDUP] {len(prefs)} → {len(result)}: {prefs} → {result}")
    return result


def _detect_food_preferences(user_input: str) -> list[dict]:
    """
    使用 LLM 检测用户输入中的食物偏好。

    覆盖：喜欢/不喜欢/过敏/忌口/习惯等表达，
    正则无法穷举的口语化偏好（如"海鲜过敏""最近戒糖""鸡胸肉不错"）。

    返回: [{"food": "鸡胸肉", "attitude": "喜欢"}, ...]
    非偏好表达返回空列表。
    """
    try:
        import json
        from backend.services.llm import get_longcat_llm

        prompt = f"""判断以下用户输入是否表达了食物偏好（喜欢/不喜欢/过敏/忌口/习惯/不爱吃等）。
不是偏好就输出空数组 []。

去重规则：语义相同的食物必须合并为一条，只保留最通用的名称。
- "喜欢米饭和白米饭" → [{{"food": "米饭", "attitude": "喜欢"}}]（白米饭=米饭，合并为米饭）
- "喜欢吃鸡胸肉和鸡肉" → [{{"food": "鸡肉", "attitude": "喜欢"}}]（鸡胸肉是鸡肉的子类，合并为鸡肉）
- "喜欢牛肉、牛腩、肥牛" → [{{"food": "牛肉", "attitude": "喜欢"}}]

偏好示例：
- "我喜欢吃鸡胸肉" → [{{"food": "鸡胸肉", "attitude": "喜欢"}}]
- "鱼我不要" → [{{"food": "鱼", "attitude": "不喜欢"}}]
- "海鲜过敏" → [{{"food": "海鲜", "attitude": "过敏"}}]
- "最近在戒糖" → [{{"food": "糖", "attitude": "忌口"}}]
- "早餐一般吃燕麦" → [{{"food": "燕麦", "attitude": "习惯"}}]
- "牛肉面yyds" → [{{"food": "牛肉面", "attitude": "喜欢"}}]
- "米饭不太想吃" → [{{"food": "米饭", "attitude": "不喜欢"}}]
- "深蹲怎么做" → []
- "今天天气怎么样" → []

用户输入：{user_input}

只输出 JSON 数组，不要其他内容。"""

        llm = get_longcat_llm()
        response = llm.invoke([{"role": "user", "content": prompt}])
        raw = response.content.strip()

        # 清理可能的 markdown 代码块标记
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3]
        raw = raw.strip()

        result = json.loads(raw)
        if isinstance(result, list):
            print(f"[FOOD-PREF-DETECT] detected: {result}")
            return _deduplicate_food_prefs(result)
    except Exception as e:
        print(f"[FOOD-PREF-DETECT] failed: {e}")
    return []


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
