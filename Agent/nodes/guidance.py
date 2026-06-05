"""动作指导节点"""
import os
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.services.llm import get_longcat_llm
from tools.search_with_tavily import search_with_tavily
from ..retriever import get_fitness_guide_retriever
import logging
logger = logging.getLogger(__name__)


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
    llm = get_longcat_llm()
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


def _format_guidance_with_llm(query: str, raw_text: str, long_memory: str | None = None, recent_turns: list | None = None, session_id: str = "") -> str:
    """
    参数:
    - query: 用户原始问题
    - raw_text: 最终检索文本（知识库 ± Tavily）
    - long_memory: 长期记忆 markdown（可选）
    - recent_turns: 短期记忆（可选）
    - session_id: 会话标识（用于检索历史摘要）

    输出:
    - guidance 文本答案
    """
    from tools.retriever1 import get_formatted_historical_events

    mem_parts = []
    if long_memory:
        mem_parts.append(f"【长期记忆 - 用户档案】\n{long_memory}")
    # 历史摘要
    if session_id:
        hist = get_formatted_historical_events(query, session_id, recent_turns)
        if hist:
            mem_parts.append(hist)
    if recent_turns:
        mem_parts.append("【短期记忆 - 最近对话】\n" + "\n".join(
            f"- {'用户' if t['role'] == 'user' else '助手'}：{t['text']}"
            for t in recent_turns
        ))
    mem_section = "\n\n".join(mem_parts)
    mem_block = f"\n{mem_section}\n" if mem_section else ""
    prompt = f"""你是健身教练，请基于检索到的资料和用户记忆上下文回答用户问题。{mem_block}
用户问题：
{query}

原始资料：
{raw_text}

输出要求：
1. 第一行必须是：推荐动作：<具体动作名称>
2. 后续分三段：标准动作 / 常见错误 / 注意事项
3. 每段 2-4 条，简洁可执行
4. 只基于提供的资料和记忆，不要编造
5. 不要输出 JSON
"""
    return _call_llm(prompt).strip()


def _enforce_action_name(answer: str, user_input: str) -> tuple[str, bool]:
    """
    确保 guidance 输出第一行为 "推荐动作：<动作名称>"。

    返回 (corrected_answer, needed_correction)。
    如果 LLM 输出已经以 "推荐动作：" 开头，原样返回。
    否则尝试从 answer 或 user_input 中提取动作名并补上。
    提取失败则填入 "推荐动作：待确认动作名" 并打 warning。
    """
    import re

    answer = answer.strip()

    # Already has action name header
    if re.match(r"推荐动作[：:]\s*\S", answer):
        return (answer, False)

    # Known action names (ordered by specificity: longer first)
    _KNOWN_ACTIONS = [
        "罗马尼亚硬拉", "引体向上", "平板支撑", "仰卧起坐", "农夫行走",
        "高位下拉", "坐姿划船", "龙门架", "蝴蝶机", "侧平举", "前平举",
        "箭步蹲", "后踢腿", "臂屈伸", "腿屈伸", "腿弯举", "开合跳",
        "登山跑", "波比跳", "俯卧撑", "卧推", "硬拉", "深蹲", "划船",
        "弯举", "推举", "下拉", "飞鸟", "腿举", "提踵", "卷腹",
        "臀桥", "面拉", "哑铃", "杠铃", "绳索",
    ]

    def _extract_action(text: str) -> str | None:
        """Extract the first known action name found in text."""
        for action in _KNOWN_ACTIONS:
            if action in text:
                return action
        return None

    # Try to extract from answer's first non-empty line
    first_line = answer.split("\n")[0].strip()
    action_name = _extract_action(first_line)
    if action_name:
        corrected = f"推荐动作：{action_name}\n\n{answer}"
        return (corrected, True)

    # Fallback: extract from user_input
    action_name = _extract_action(user_input)
    if action_name:
        corrected = f"推荐动作：{action_name}\n\n{answer}"
        return (corrected, True)

    # Last resort
    logger.warning(
        f"[ACTION-NAME] Cannot extract action name. "
        f"user_input={user_input!r}, answer_preview={answer[:80]!r}"
    )
    corrected = f"推荐动作：待确认动作名\n\n{answer}"
    return (corrected, True)


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
    trace_id = state.get("trace_id","unknown")
    user_input = state["user_input"]
    logger.info(f"[trace={trace_id}] guidance_node start")
    long_memory = state.get("long_memory")
    recent_turns = state.get("recent_turns")

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
    answer = _format_guidance_with_llm(
        user_input, retrieved_content, long_memory, recent_turns,
        session_id=state.get("session_id", ""),
    )

    # 后处理：强制要求动作名
    answer, corrected = _enforce_action_name(answer, user_input)
    if corrected:
        logger.info(f"[trace={trace_id}] Action name was missing, post-processed")

    if retrieved_results:
        hit_id = retrieved_results[0].get("id")
        score = retrieved_results[0].get("score")

    result = {
        "guidance": answer,
        "status": "success",
        "metadata": {
            "source": source,
            "hit_id": hit_id,
            "score": score,
        },
        "follow_up_questions": [],
    }
    logger.info(
        f"[trace={trace_id}] guidance_node done: "
        f"status={result.get('status')}, source={result.get('metadata', {}).get('source')}"
    )
    return result   
    # return {
    #     "guidance": answer,
    #     "status": "success",
    #     "metadata": {
    #         "source": source,
    #         "hit_id": hit_id,
    #         "score": score,
    #     },
    #     "follow_up_questions": [],
    # }
