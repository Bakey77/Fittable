from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Protocol
from backend.services.llm import get_llm
from tools.search_with_tavily import search_with_tavily

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from .intent_classifier import classify_intent, parse_json_from_text
except ImportError:
    from Agent.intent_classifier import classify_intent, parse_json_from_text

# from Agent.retriever import get_fitness_guide_retriever
# _fitness_guide_retriever = get_fitness_guide_retriever(retrieval_model = "vector")

from Agent.retriever import get_fitness_guide_retriever                                                                                                                                                    
_fitness_guide_retriever = get_fitness_guide_retriever(retrieval_mode="vector")


GOAL_KEYWORDS = {
    "fat_loss": ["减脂", "减肥", "lose", "fat"],
    "muscle_gain": ["增肌", "变壮", "muscle", "gain"],
    "beginner": ["新手", "刚开始", "beginner", "入门"],
}

VALID_FREQUENCY = set(range(1, 8))


class FitnessGuideRetriever(Protocol):
    def retrieve(self, query: str, top_k: int = 3) -> list[Any]:
        ...


_fitness_guide_retriever: FitnessGuideRetriever | None = None
_check_retrieval_fn: Callable[[str, str], bool] | None = None


def set_fitness_guide_retriever(retriever: FitnessGuideRetriever) -> None:
    global _fitness_guide_retriever
    _fitness_guide_retriever = retriever


def set_retrieval_sufficiency_checker(checker: Callable[[str, str], bool]) -> None:
    """
    注入检索充分性判断函数:
    checker(retrieved_content, query) -> bool
    """
    global _check_retrieval_fn
    _check_retrieval_fn = checker


def _is_retrieval_sufficient(retrieved_content: str, query: str) -> bool:
    if _check_retrieval_fn is not None:
        try:
            return bool(_check_retrieval_fn(retrieved_content, query))
        except Exception:
            return False

    # 默认兜底规则：有足够长度文本即认为可用
    _ = query
    return bool(retrieved_content and len(retrieved_content.strip()) >= 40)


def normalize_goal(raw_goal: str | None) -> str | None:
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


def check_plan_info(entities: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    goal = normalize_goal(entities.get("goal"))
    if not goal:
        goal = normalize_goal(profile.get("goal"))

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


def _call_llm(prompt: str) -> str:
    llm = get_llm()
    response = llm.invoke([{"role": "user", "content": prompt}])
    return response.content if hasattr(response, "content") else str(response)


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
    if not isinstance(parsed, dict):
        return {}
    return parsed


def plan_pipeline(user_input: str, profile: dict[str, Any], entities: dict[str, Any]) -> dict[str, Any]:
    merged_entities = dict(entities or {})
    if not merged_entities.get("goal") or not merged_entities.get("frequency"):
        extracted = _extract_plan_entities_with_llm(user_input)
        merged_entities = {**extracted, **merged_entities}

    info = check_plan_info(merged_entities, profile)
    if info["missing"]:
        question_map = {
            "goal": "你的训练目标是？请选择：增肌 / 减脂 / 新手入门",
            "frequency": "你每周打算训练几天？（1~7天）",
        }
        questions = [question_map[field] for field in info["missing"]]
        return {
            "status": "need_info",
            "missing": info["missing"],
            "questions": questions,
        }

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
        return {"status": "error", "message": "plan generation failed"}

    return {"status": "success", "plan": plan_json}


def tavily_search_exercise(query: str) -> str | None:
    try:
        text = search_with_tavily(query)
        if not text:
            return None
        return str(text).strip() or None
    except Exception:
        return None


def format_guidance_with_llm(query: str, raw_text: str) -> str:
    prompt = f"""
你是健身教练，请基于检索到的资料回答用户问题。

用户问题：
{query}

原始资料：
{raw_text}

输出要求：
1. 分三段：标准动作 / 常见错误 / 注意事项
2. 每段 2-4 条，简洁可执行
3. 只基于提供的资料，不要编造
4. 不要输出 JSON
"""
    return _call_llm(prompt).strip()


def retrieve_from_fitness_guide(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    if _fitness_guide_retriever is None:
        return []

    try:
        # 优先对齐 lrw.py：混合检索接口 retrieve(query, top_k)
        if hasattr(_fitness_guide_retriever, "retrieve"):
            raw_results = _fitness_guide_retriever.retrieve(query, top_k=top_k)
            normalized: list[dict[str, Any]] = []
            for r in raw_results or []:
                if isinstance(r, dict):
                    text = r.get("text") or r.get("content")
                    if text:
                        normalized.append(
                            {
                                "id": r.get("id"),
                                "text": str(text),
                                "score": r.get("score"),
                            }
                        )
                else:
                    text = getattr(r, "text", None) or getattr(r, "content", None)
                    if text:
                        normalized.append(
                            {
                                "id": getattr(r, "id", None),
                                "text": str(text),
                                "score": getattr(r, "score", None),
                            }
                        )
            return normalized

        # 兼容旧接口：embed_query + search_top1
        if hasattr(_fitness_guide_retriever, "embed_query") and hasattr(_fitness_guide_retriever, "search_top1"):
            vector = _fitness_guide_retriever.embed_query(query)
            hit = _fitness_guide_retriever.search_top1(vector)
            if isinstance(hit, dict) and hit.get("text"):
                return [{"id": hit.get("id"), "text": str(hit["text"]), "score": hit.get("score")}]
            return []
    except Exception:
        return []
    return []


def handle_guidance(user_input: str, entities: dict[str, Any] | None = None) -> dict[str, Any]:
    _ = entities  # 保留参数占位，避免破坏外部调用

    retrieved_results = retrieve_from_fitness_guide(user_input, top_k=3)
    retrieved_content = "\n".join([r["text"] for r in retrieved_results if r.get("text")]).strip()
    source = "fitness_guide"

    if not _is_retrieval_sufficient(retrieved_content, query=user_input):
        search_text = tavily_search_exercise(user_input)
        if search_text:
            retrieved_content = (
                f"{retrieved_content}\n\n--- 网络搜索结果 ---\n{search_text}".strip()
                if retrieved_content
                else search_text
            )
            source = "fitness_guide+tavily" if retrieved_results else "tavily"

    if not retrieved_content:
        return {
            "status": "not_found",
            "message": "未在 fitness_guide 或 Tavily 中检索到相关训练指导，请补充更具体的问题。",
        }

    answer = format_guidance_with_llm(user_input, retrieved_content)
    if retrieved_results:
        top_hit = retrieved_results[0]
        return {
            "status": "success",
            "source": source,
            "hit_id": top_hit.get("id"),
            "score": top_hit.get("score"),
            "guidance": answer,
        }

    return {"status": "success", "source": source, "guidance": answer}


def understand(user_input: str) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, Any]]:
    classified = classify_intent(user_input)
    intents_scored = classified.get("intents") or []
    selected: list[str] = []
    entities_by_intent: dict[str, dict[str, Any]] = {}

    for item in intents_scored:
        if not isinstance(item, dict):
            continue
        intent_type = item.get("type")
        if intent_type in {"training_plan", "training_guidance"} and intent_type not in selected:
            selected.append(intent_type)
            entities_by_intent[intent_type] = item.get("entities") or {}

    primary = classified.get("primary_intent")
    if isinstance(primary, dict):
        intent_type = primary.get("type")
        if intent_type in {"training_plan", "training_guidance"} and intent_type not in selected:
            selected.append(intent_type)
            entities_by_intent[intent_type] = primary.get("entities") or {}

    return selected, entities_by_intent, classified


def build_response(result: dict[str, Any], ask: list[str], meta: dict[str, Any]) -> dict[str, Any]:
    response: dict[str, Any] = {"result": result}
    if ask:
        response["follow_up"] = ask
    response["meta"] = meta
    return response


def workout_agent(user_input: str, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = profile or {}
    intents, entities_by_intent, classify_meta = understand(user_input)

    result: dict[str, Any] = {}
    ask: list[str] = []
    statuses: dict[str, str] = {}

    if "training_plan" in intents:
        plan_entities = entities_by_intent.get("training_plan", {})
        plan_result = plan_pipeline(user_input, profile, plan_entities)
        statuses["training_plan"] = plan_result.get("status", "error")
        if plan_result["status"] == "success":
            result["plan"] = plan_result["plan"]
        elif plan_result["status"] == "need_info":
            ask.extend(plan_result.get("questions", []))
        else:
            result["plan_error"] = plan_result.get("message", "plan generation failed")

    if "training_guidance" in intents:
        guidance_entities = entities_by_intent.get("training_guidance", {})
        guidance_result = handle_guidance(user_input, entities=guidance_entities)
        statuses["training_guidance"] = guidance_result.get("status", "error")
        if guidance_result["status"] == "success":
            result["guidance"] = guidance_result
        elif guidance_result["status"] == "need_info":
            ask.append(guidance_result["message"])
        else:
            result["guidance_error"] = guidance_result.get("message", "guidance failed")

    if not result and not ask:
        ask.append("你更想要哪类帮助：训练计划还是动作指导？")
        statuses["fallback"] = "need_info"

    ask = list(dict.fromkeys(ask))
    meta = {
        "intents": intents,
        "classify": classify_meta,
        "statuses": statuses,
    }
    return build_response(result=result, ask=ask, meta=meta)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Workout Agent")
    parser.add_argument("message", help="User input")
    parser.add_argument("--profile", default="{}", help='JSON string, e.g. {"goal":"增肌","frequency":4}')
    args = parser.parse_args()

    try:
        profile_data = json.loads(args.profile)
        if not isinstance(profile_data, dict):
            profile_data = {}
    except json.JSONDecodeError:
        profile_data = {}

    output = workout_agent(args.message, profile_data)
    print(json.dumps(output, ensure_ascii=False, indent=2))
