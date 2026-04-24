"""动作指导节点"""
import os
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.services.llm import get_llm
from tools.search_with_tavily import search_with_tavily
from ..retriever import get_fitness_guide_retriever


def _call_llm(prompt: str) -> str:
    llm = get_llm()
    response = llm.invoke([{"role": "user", "content": prompt}])
    return response.content if hasattr(response, "content") else str(response)


def _is_retrieval_sufficient(retrieved_content: str, min_length: int = 40) -> bool:
    return bool(retrieved_content and len(retrieved_content.strip()) >= min_length)


def _format_guidance_with_llm(query: str, raw_text: str) -> str:
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


def _tavily_search(query: str) -> str | None:
    try:
        text = search_with_tavily(query)
        return str(text).strip() if text else None
    except Exception:
        return None


def guidance_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    节点2: 动作指导

    输入: user_input
    输出: guidance, status, metadata, follow_up_questions
    """
    user_input = state["user_input"]

    # 初始化检索器（单例）
    retriever = get_fitness_guide_retriever(retrieval_mode="vector")

    # 执行检索
    retrieved_results = retriever.retrieve(user_input, top_k=3)
    retrieved_content = "\n".join([r["text"] for r in retrieved_results if r.get("text")]).strip()

    source = "fitness_guide"
    hit_id = None
    score = None

    # 检索不充分时使用 Tavily 兜底
    if not _is_retrieval_sufficient(retrieved_content):
        search_text = _tavily_search(user_input)
        if search_text:
            retrieved_content = (
                f"{retrieved_content}\n\n--- 网络搜索结果 ---\n{search_text}".strip()
                if retrieved_content
                else search_text
            )
            source = "fitness_guide+tavily" if retrieved_results else "tavily"

    if not retrieved_content:
        return {
            "guidance": "",
            "status": "not_found",
            "metadata": {
                "source": source,
                "hit_id": hit_id,
                "score": score,
            },
            "follow_up_questions": [
                "未在 fitness_guide 或 Tavily 中检索到相关训练指导，请补充更具体的问题。"
            ],
        }

    # 生成回答
    answer = _format_guidance_with_llm(user_input, retrieved_content)

    if retrieved_results:
        hit_id = retrieved_results[0].get("id")
        score = retrieved_results[0].get("score")

    return {
        "guidance": answer,
        "status": "success",
        "metadata": {
            "source": source,
            "hit_id": hit_id,
            "score": score,
        },
        "follow_up_questions": [],
    }
