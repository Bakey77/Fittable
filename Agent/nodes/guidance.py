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
    """
    参数:
    - prompt: 提示词文本
      来源: _format_guidance_with_llm 组装

    输出:
    - LLM 生成文本

    流向:
    - 返回给 _format_guidance_with_llm 作为动作指导答案
    """
    llm = get_llm()
    response = llm.invoke([{"role": "user", "content": prompt}])
    return response.content if hasattr(response, "content") else str(response)


def _is_retrieval_sufficient(retrieved_content: str, min_length: int = 40) -> bool:
    """
    参数:
    - retrieved_content: 当前检索拼接文本
      来源: guidance_node 内部的 fitness_guide 检索结果
    - min_length: 最小长度阈值
      来源: guidance_node 默认配置

    输出:
    - bool: True 表示无需 Tavily 兜底，False 表示需要补检索

    流向:
    - guidance_node 内部分支判断
    """
    return bool(retrieved_content and len(retrieved_content.strip()) >= min_length)


def _format_guidance_with_llm(query: str, raw_text: str, long_memory: str | None = None) -> str:
    """
    参数:
    - query: 用户原始问题
      来源: guidance_node 的 state["user_input"]
    - raw_text: 最终检索文本（知识库 ± Tavily）
      来源: guidance_node 检索与兜底拼接结果
    - long_memory: 长期记忆 markdown（可选）
      来源: state["long_memory"]

    输出:
    - guidance 文本答案

    流向:
    - 返回给 guidance_node，写入节点输出 guidance 字段
    """
    mem_section = f"\n用户长期记忆：\n{long_memory}\n" if long_memory else ""
    prompt = f"""你是健身教练，请基于检索到的资料回答用户问题。{mem_section}
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
    """
    参数:
    - query: 用户问题
      来源: guidance_node 的 user_input

    输出:
    - str | None: Tavily 搜索文本（失败或空结果返回 None）

    流向:
    - guidance_node 在本地检索不足时调用，用于补充 retrieved_content
    """
    try:
        text = search_with_tavily(query)
        return str(text).strip() if text else None
    except Exception:
        return None


def guidance_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    参数:
    - state: 当前工作流状态
      来源: intent_classifier 路由到 guidance_node 后的 state
      关键读取字段:
      1) user_input

    输出:
    - guidance: 指导文本（成功时）
    - status: success | not_found
    - metadata: 检索来源和命中信息（source/hit_id/score）
    - follow_up_questions: 未命中时的追问提示

    流向:
    - 输出回到 workflow 主流程并到 END
    - workflow.run_workflow 会把 guidance/status/metadata 写入 result
    - run_workflow 再把摘要写回 session memory（assistant turn）
    """
    user_input = state["user_input"]
    long_memory = state.get("long_memory")

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
    answer = _format_guidance_with_llm(user_input, retrieved_content, long_memory)

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
