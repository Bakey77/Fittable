"""一般对话节点 - 处理 general 意图"""
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.services.llm import get_longcat_llm


def general_conversation_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    参数:
    - state: 当前工作流状态
      来源: intent_classifier 路由到 general_conversation_node 后的 state
      关键读取字段:
      1) user_input
      2) recent_turns: 完整短期记忆（10轮，不裁剪）
      3) long_memory: 长期记忆 markdown
      4) pending_intent / pending_entities: 当前进行中的意图状态

    输出:
    - general_response: 一般对话回复文本
    - status: success
    - follow_up_questions: 追问提示（引导用户回到健身话题）
    """
    user_input = state.get("user_input", "")
    recent_turns = state.get("recent_turns") or []
    long_memory = state.get("long_memory")
    pending_intent = state.get("pending_intent")
    pending_entities = state.get("pending_entities")

    # 组装完整上下文（不裁剪）
    context_parts = []
    if long_memory:
        context_parts.append(f"【长期记忆 - 用户持久档案】\n{long_memory}")
    if recent_turns:
        context_parts.append("【短期记忆 - 最近对话】\n" + "\n".join(
            f"- {'用户' if t['role'] == 'user' else '助手'}：{t['text']}"
            for t in recent_turns
        ))
    if pending_intent or pending_entities:
        context_parts.append(f"【进行中的任务】pending_intent={pending_intent}, pending_entities={pending_entities}")

    # 构建完整 prompt
    context_block = "\n\n".join(context_parts)
    prompt = f"""你是健身助手小Fit，专注于健身、训练计划、饮食分析等健康话题。

{context_block}

用户新消息：{user_input}

回复要求：
1. 优先根据【长期记忆】和【短期记忆】中的信息回答问题。如果用户询问"我叫什么""我的身高"等个人信息，必须在记忆中查找并准确回答。
2. 如果记忆中确实没有相关信息，诚实告知用户。
3. 回复控制在 100 字以内，只输出自然语言。
"""

    # 调用 LLM 生成回复
    llm = get_longcat_llm()
    response = llm.invoke([{"role": "user", "content": prompt}])
    answer = response.content if hasattr(response, "content") else str(response)

    # 生成追问提示
    follow_up = "你还有什么健身相关的问题吗？比如：\n- 想要一个训练计划\n- 动作指导\n- 饮食分析"
    if recent_turns:
        follow_up = "关于之前的训练计划或对话内容，你还有什么想了解的吗？\n- 需要调整训练安排吗\n- 有其他健身问题吗"

    return {
        "general_response": answer.strip(),
        "status": "success",
        "follow_up_questions": [follow_up],
    }
