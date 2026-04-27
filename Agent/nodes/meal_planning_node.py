"""餐食规划节点"""
from typing import Any

from backend.services.llm import get_llm


def meal_planning_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    参数:
    - state: 当前工作流状态
      关键读取字段:
      1) user_input: 用户输入文本
      2) long_memory: 长期记忆 markdown
      3) memory_summary: 短期记忆历史摘要

    输出:
    - analysis_result: 餐食规划结果
    - status: success

    流向:
    - 输出回到 workflow 主流程并到 END
    """
    user_input = state["user_input"]
    long_memory = state.get("long_memory")
    memory_summary = state.get("memory_summary")

    mem_parts = []
    if long_memory:
        mem_parts.append(f"用户长期记忆：\n{long_memory}")
    if memory_summary:
        mem_parts.append(f"对话历史摘要：\n{memory_summary}")
    mem_section = "\n".join(mem_parts)
    mem_block = f"\n{mem_section}\n" if mem_section else "\n"

    prompt = f"""你是专业营养师。请结合用户记忆上下文，制定餐食规划。{mem_block}
用户输入：{user_input}

请给出包含早中晚三餐的食谱规划，格式：
1. 早餐 / 午餐 / 晚餐：食物列表及份量
2. 总热量估算
3. 简短营养说明
只输出自然语言，不要 JSON。
"""
    llm = get_llm()
    response = llm.invoke([{"role": "user", "content": prompt}])
    analysis_result = response.content if hasattr(response, "content") else str(response)

    return {
        "analysis_result": analysis_result.strip(),
        "status": "success",
        "metadata": {
            "source": "meal_planning_node",
        },
    }
