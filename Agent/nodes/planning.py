"""训练计划节点 - 带内部子图的实现"""
import re
import sys
from pathlib import Path
from typing import Any, Literal

from langgraph.graph import StateGraph, END

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.services.llm import get_llm
from ..intent_classifier import parse_json_from_text


GOAL_KEYWORDS = {
    "fat_loss": ["减脂", "减肥", "lose", "fat"],
    "muscle_gain": ["增肌", "变壮", "muscle", "gain"],
    "beginner": ["新手", "刚开始", "beginner", "入门"],
}
VALID_FREQUENCY = set(range(1, 8))
CN_NUM_MAP = {
    "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7,
}


def _call_llm(prompt: str) -> str:
    llm = get_llm()
    response = llm.invoke([{"role": "user", "content": prompt}])
    return response.content if hasattr(response, "content") else str(response)


def _normalize_goal(raw_goal: str | None) -> str | None:
    if not raw_goal:
        return None
    goal = str(raw_goal).lower()
    for normalized, words in GOAL_KEYWORDS.items():
        if any(w in goal for w in words):
            return normalized
    return None


def _extract_positive_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        m = re.search(r"\d+", value)
        if m:
            num = int(m.group(0))
            return num if num > 0 else None
        for ch, num in CN_NUM_MAP.items():
            if ch in value:
                return num
    return None


def _extract_plan_entities_with_llm(user_input: str, long_memory: str | None = None) -> dict[str, Any]:
    mem_section = f"\n用户长期记忆：\n{long_memory}\n" if long_memory else ""
    prompt = f"""你是信息抽取器。请从用户输入中提取训练计划相关实体。{mem_section}
用户输入：{user_input}

只输出 JSON：
{{
  "goal": "增肌/减脂/新手 或 null",
  "frequency": "每周训练天数数字 或 null"
}}
"""
    raw = _call_llm(prompt)
    parsed = parse_json_from_text(raw)
    return parsed if isinstance(parsed, dict) else {}


def _is_non_empty_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


# =============================================================================
# Planning Subgraph State
# =============================================================================
class PlanningSubgraphState(dict):
    """子图内部状态"""
    user_input: str
    pending_entities: dict[str, Any]   # 累积的实体（从历史轮次合并）
    extracted_entities: dict[str, Any] # 本轮新提取的实体
    profile: dict[str, Any]
    plan: dict[str, Any]
    status: str
    follow_up_questions: list[str]
    waiting_info: dict | None
    _route: str  # internal routing signal returned by check node


# =============================================================================
# Planning Subgraph Nodes
# =============================================================================
def _subgraph_extract(state: PlanningSubgraphState) -> dict:
    """从用户输入提取实体"""
    user_input = state["user_input"]
    long_memory = state.get("long_memory")
    extracted = _extract_plan_entities_with_llm(user_input, long_memory)
    return {"extracted_entities": extracted}


def _subgraph_merge(state: PlanningSubgraphState) -> dict:
    """合并 pending_entities + 本轮提取的实体"""
    pending = state.get("pending_entities") or {}
    extracted = state.get("extracted_entities") or {}
    merged = dict(pending)
    for k, v in extracted.items():
        if _is_non_empty_value(v):
            merged[k] = v
    return {"pending_entities": merged}


def _subgraph_check(state: PlanningSubgraphState) -> dict:
    """检查是否所有必填字段都已完备，设置 _route 路由信号"""
    pending = state.get("pending_entities") or {}
    goal = _normalize_goal(pending.get("goal"))
    frequency = _extract_positive_int(pending.get("frequency"))
    if goal and frequency in VALID_FREQUENCY:
        return {"_route": "generate_plan"}
    return {"_route": "ask_user"}


def _subgraph_ask_user(state: PlanningSubgraphState) -> dict:
    """生成追问，一次性要求补全所有缺失字段

    注意：这里只是返回追问，本轮 workflow call 结束后用户在前端输入新内容，
    前端触发第二次 workflow call，带着新 user_input 和 session memory 中
    已累积的 pending_entities 进入 planning_node 子图继续处理。
    """
    pending = state.get("pending_entities") or {}
    goal = _normalize_goal(pending.get("goal"))
    frequency = _extract_positive_int(pending.get("frequency"))

    missing = []
    if not goal:
        missing.append("goal")
    if not frequency:
        missing.append("frequency")

    question_map = {
        "goal": "你的训练目标是？请选择：增肌 / 减脂 / 新手入门",
        "frequency": "你每周打算训练几天？（1~7天）",
    }
    questions = [question_map[field] for field in missing]

    return {
        "follow_up_questions": questions,
        "waiting_info": {"missing": missing},
        "status": "need_info",
        "pending_entities": pending,
        "_route": "ask_user",
    }


def _subgraph_generate_plan(state: PlanningSubgraphState) -> dict:
    """生成训练计划"""
    pending = state.get("pending_entities") or {}
    profile = state.get("profile", {})
    goal = _normalize_goal(pending.get("goal"))
    frequency = _extract_positive_int(pending.get("frequency"))
    long_memory = state.get("long_memory")

    mem_section = f"\n用户长期记忆：\n{long_memory}\n" if long_memory else ""
    prompt = f"""你是一个专业健身教练。请基于以下信息生成训练计划。{mem_section}
- 目标: {goal}
- 每周训练: {frequency} 天

输出要求：
1. 只输出 JSON，不要解释
2. 顶层字段：goal, frequency, plan
3. plan 是数组，每个元素包含：
   - day: Day1/Day2...（保持英文格式如Day1、Day2等）
   - focus: 必须是这些中文之一：推、拉、腿、全身、核心、有氧（绝不允许英文）
   - exercises: 数组，每个元素包含 name, sets, reps
4. 重要：exercises 中的 name 字段必须全部是中文动作名
5. 常见中文动作名示例：杠铃深蹲、杠铃卧推、高位下拉、罗马尼亚硬拉、平板支撑、硬拉、肩上推举、上斜哑铃卧推、腿弯举、悬垂举腿、哑铃划船、绳索夹胸、二头弯举、三头下压、腿屈伸、提踵、箭步蹲、前深蹲、杠铃划船、引体向上、俯卧撑、卷腹等
6. 绝对禁止任何英文动作名（如BenchPress、Deadlift等）
"""
    raw_output = _call_llm(prompt)
    plan_json = parse_json_from_text(raw_output)

    if not isinstance(plan_json, dict) or "plan" not in plan_json:
        return {
            "plan": {},
            "status": "error",
            "follow_up_questions": ["生成计划失败，请重试"],
            "pending_intent": None,  # 失败也清空，避免无限循环
        }

    return {
        "plan": plan_json,
        "status": "success",
        "follow_up_questions": [],
        "waiting_info": None,
        "pending_intent": None,  # 计划生成成功，清空 pending_intent
        "pending_entities": pending,  # 保留，供后续追问使用
    }


# =============================================================================
# Build Planning Subgraph
# =============================================================================
_subgraph_instance = None


def _build_planning_subgraph():
    """构建 planning 子图"""
    g = StateGraph(PlanningSubgraphState)

    g.add_node("extract", _subgraph_extract)
    g.add_node("merge", _subgraph_merge)
    g.add_node("check", _subgraph_check)
    g.add_node("ask_user", _subgraph_ask_user)
    g.add_node("generate_plan", _subgraph_generate_plan)

    g.set_entry_point("extract")
    g.add_edge("extract", "merge")
    g.add_edge("merge", "check")

    def _router(state: PlanningSubgraphState) -> str:
        """条件边路由函数"""
        pending = state.get("pending_entities") or {}
        goal = _normalize_goal(pending.get("goal"))
        frequency = _extract_positive_int(pending.get("frequency"))
        if goal and frequency in VALID_FREQUENCY:
            return "generate_plan"
        return "ask_user"

    g.add_conditional_edges(
        "check",
        _router,
        {
            "generate_plan": "generate_plan",
            "ask_user": "ask_user",
        }
    )

    g.add_edge("generate_plan", END)
    g.add_edge("ask_user", END)

    return g.compile()


def _get_planning_subgraph():
    global _subgraph_instance
    if _subgraph_instance is None:
        _subgraph_instance = _build_planning_subgraph()
    return _subgraph_instance


# =============================================================================
# Planning Node (主节点 - 调用子图)
# =============================================================================
def planning_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    参数:
    - state: 主工作流状态
      关键读取字段:
      1) user_input: 用户输入
      2) pending_entities: 从 session memory 恢复的历史实体
      3) profile: 用户画像
      4) waiting_info: 当前等待状态（如果有）

    输出:
    - plan / status / follow_up_questions / waiting_info / pending_entities
    """
    user_input = state["user_input"]
    profile = state.get("profile", {})
    # 从 session memory 恢复的 pending_entities（在上轮写回的）
    pending_entities = state.get("pending_entities") or {}
    waiting_info = state.get("waiting_info")

    subgraph_state: PlanningSubgraphState = {
        "user_input": user_input,
        "pending_entities": dict(pending_entities),
        "extracted_entities": {},
        "profile": profile,
        "plan": {},
        "status": "",
        "follow_up_questions": [],
        "waiting_info": None,
        "long_memory": state.get("long_memory"),
    }

    subgraph = _get_planning_subgraph()
    result = subgraph.invoke(subgraph_state)

    # 子图返回的 pending_entities（可能是累积后的）写回 session memory
    subgraph_pending = result.get("pending_entities") or pending_entities
    status = result.get("status", "")

    # need_info 场景必须保留 pending_intent，确保下一轮继续走 planning，而不是重新意图分类
    if status == "need_info":
        next_pending_intent = "training_plan"
    else:
        # success/error 场景由子图决定（通常为 None，表示清空）
        next_pending_intent = result.get("pending_intent")

    return {
        "plan": result.get("plan", {}),
        "status": status,
        "follow_up_questions": result.get("follow_up_questions", []),
        "waiting_info": result.get("waiting_info"),
        "pending_intent": next_pending_intent,
        "pending_entities": subgraph_pending,
    }
