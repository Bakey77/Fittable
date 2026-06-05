"""餐食规划节点"""
import logging
from typing import Any

from backend.services.llm import get_longcat_llm

logger = logging.getLogger(__name__)


def meal_planning_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    参数:
    - state: 当前工作流状态
      关键读取字段:
      1) user_input: 用户输入文本
      2) long_memory: 长期记忆 markdown
    输出:
    - analysis_result: 餐食规划结果
    - status: success

    流向:
    - 输出回到 workflow 主流程并到 END
    """
    trace_id = state.get("trace_id", "unknown")
    user_input = state["user_input"]
    logger.info(f"[trace={trace_id}] meal_planning_node start")
    long_memory = state.get("long_memory")
    recent_turns = state.get("recent_turns") or []
    session_id = state.get("session_id", "")

    from tools.retriever1 import get_formatted_historical_events

    mem_parts = []
    if long_memory:
        mem_parts.append(f"【长期记忆 - 用户档案】\n{long_memory}")
    if session_id:
        hist = get_formatted_historical_events(user_input, session_id, recent_turns)
        if hist:
            mem_parts.append(hist)
    if recent_turns:
        mem_parts.append("【短期记忆 - 最近对话】\n" + "\n".join(
            f"- {'用户' if t['role'] == 'user' else '助手'}：{t['text']}"
            for t in recent_turns
        ))
    mem_section = "\n\n".join(mem_parts)
    mem_block = f"\n{mem_section}\n" if mem_section else "\n"

    prompt = f"""你是专业营养师。请根据用户记忆上下文和当前输入，制定餐食规划。{mem_block}
用户输入：{user_input}

请给出包含早中晚三餐的食谱规划，格式：
1. 早餐 / 午餐 / 晚餐：食物列表及份量
2. 总热量估算
3. 简短营养说明
只输出自然语言，不要 JSON。
"""
    llm = get_longcat_llm()
    response = llm.invoke([{"role": "user", "content": prompt}])
    analysis_result = response.content if hasattr(response, "content") else str(response)

    result = {
        "analysis_result": analysis_result.strip(),
        "status": "success",
        "metadata": {
            "source": "meal_planning_node",
        },
    }
    logger.info(f"[trace={trace_id}] meal_planning_node done: status={result['status']}")
    return result
