"""训练计划节点"""
import re
import sys
from pathlib import Path
from typing import Any

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
    return None


def _extract_plan_entities_with_llm(user_input: str) -> dict[str, Any]:
    prompt = f"""
你是信息抽取器。请从用户输入中提取训练计划相关实体。
用户输入：{user_input}

只输出 JSON：
{{
  "goal": "增肌/减脂/新手 或 null",
  "frequency": "每周训练天数数字 或 null",
  "duration": "训练周期数字（周）或 null"
}}
"""
    raw = _call_llm(prompt)
    parsed = parse_json_from_text(raw)
    return parsed if isinstance(parsed, dict) else {}


def _check_plan_info(entities: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    goal = _normalize_goal(entities.get("goal"))
    if not goal:
        goal = _normalize_goal(profile.get("goal"))

    frequency = _extract_positive_int(entities.get("frequency"))
    if frequency not in VALID_FREQUENCY:
        frequency = _extract_positive_int(profile.get("frequency"))
        if frequency not in VALID_FREQUENCY:
            frequency = None

    duration = _extract_positive_int(entities.get("duration"))
    if duration is None:
        duration = _extract_positive_int(profile.get("duration"))

    missing = []
    if not goal:
        missing.append("goal")
    if not frequency:
        missing.append("frequency")

    return {
        "missing": missing,
        "goal": goal,
        "frequency": frequency,
        "duration": duration,
    }


def planning_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    节点3: 训练计划

    输入: user_input, profile, entities, pending_intent, pending_entities, waiting_info
    输出: plan, status, follow_up_questions, waiting_info, pending_intent, pending_entities
    """
    user_input = state["user_input"]
    profile = state.get("profile", {})
    entities = state.get("entities", {})
    pending_intent = state.get("pending_intent")
    pending_entities = state.get("pending_entities")
    waiting_info = state.get("waiting_info")

    # 首次进入（不是从 waiting 恢复）
    if not waiting_info:
        # 合并实体，缺失时用 LLM 补充
        merged_entities = dict(entities or {})
        if not merged_entities.get("goal") or not merged_entities.get("frequency"):
            extracted = _extract_plan_entities_with_llm(user_input)
            merged_entities = {**extracted, **merged_entities}
    else:
        # 从 waiting 恢复：pending_entities 中的缺失字段已经是 None
        merged_entities = dict(pending_entities or {})

    # 检查必需信息
    info = _check_plan_info(merged_entities, profile)

    if info["missing"]:
        question_map = {
            "goal": "你的训练目标是？请选择：增肌 / 减脂 / 新手入门",
            "frequency": "你每周打算训练几天？（1~7天）",
        }
        questions = [question_map[field] for field in info["missing"]]
        return {
            "plan": {},
            "status": "need_info",
            "follow_up_questions": questions,
            "waiting_info": {"missing": info["missing"]},
            "pending_intent": "training_plan",
            "pending_entities": merged_entities,
        }

    # 生成训练计划
    prompt = f"""
你是一个专业健身教练。请基于以下信息生成训练计划。
- 目标: {info["goal"]}
- 每周训练: {info["frequency"]} 天
- 训练周期: {info["duration"] if info["duration"] else "4"} 周

输出要求：
1. 只输出 JSON，不要解释
2. 顶层字段：goal, frequency, duration, plan
3. plan 是数组，每个元素包含：
   - day: Day1/Day2...
   - focus: push/pull/legs/full_body/core/cardio
   - exercises: 数组，每个元素包含 name, sets, reps
"""
    raw_output = _call_llm(prompt)
    plan_json = parse_json_from_text(raw_output)

    if not isinstance(plan_json, dict) or "plan" not in plan_json or not isinstance(plan_json.get("plan"), list):
        return {
            "plan": {},
            "status": "error",
            "follow_up_questions": [],
            "waiting_info": None,
            "pending_intent": None,
            "pending_entities": None,
        }

    return {
        "plan": plan_json,
        "status": "success",
        "follow_up_questions": [],
        "waiting_info": None,
        "pending_intent": None,
        "pending_entities": None,
    }
