"""一般对话节点 - 处理 general 意图"""
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.services.llm import get_llm


GENERAL_CONVERSATION_PROMPT = """你是健身助手小Fit，专注于健身、训练计划、饮食分析等健康话题。

用户发送了一条非专业领域的问题："{user_input}"

请按以下要求回复：
1. 友好、简洁地回应用户
2. 如果问题与健身/健康完全无关，可以礼貌地说明你的专长范围
3. 适当引导用户回到健身相关话题
4. 不要输出 JSON 或结构化数据，只输出自然语言回复
5. 回复控制在 100 字以内

回复："""


def general_conversation_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    参数:
    - state: 当前工作流状态
      来源: intent_classifier 路由到 general_conversation_node 后的 state
      关键读取字段:
      1) user_input
      2) memory_summary（若有）
      3) recent_turns（若有）

    输出:
    - general_response: 一般对话回复文本
    - status: success
    - follow_up_questions: 追问提示（引导用户回到健身话题）

    流向:
    - 输出回到 workflow 主流程并到 END
    - workflow.run_workflow 会把 general_response/status 写入 result
    """
    user_input = state.get("user_input", "")
    memory_summary = state.get("memory_summary")
    recent_turns = state.get("recent_turns", [])
    long_memory = state.get("long_memory")

    # 组装上下文（如果有历史对话）
    context_parts = []
    if long_memory:
        context_parts.append(f"用户长期记忆：\n{long_memory}")
    if memory_summary:
        context_parts.append(f"对话历史摘要：{memory_summary}")
    if recent_turns:
        # 取最近2轮作为上下文
        recent = recent_turns[-4:] if len(recent_turns) >= 4 else recent_turns
        context_parts.append("最近对话：\n" + "\n".join(
            f"- {'用户' if t['role'] == 'user' else '助手'}：{t['text']}"
            for t in recent
        ))

    # 构建完整 prompt
    if context_parts:
        prompt = f"""你是健身助手小Fit，专注于健身、训练计划、饮食分析等健康话题。

以下是对话上下文：
{chr(10).join(context_parts)}

用户新消息：{user_input}

请根据上下文，友好地回应用户。如果用户问题与健身/健康无关，可以礼貌地引导用户回到健身话题。回复控制在 100 字以内，只输出自然语言。
"""
    else:
        prompt = GENERAL_CONVERSATION_PROMPT.format(user_input=user_input)

    # 调用 LLM 生成回复
    llm = get_llm()
    response = llm.invoke([{"role": "user", "content": prompt}])
    answer = response.content if hasattr(response, "content") else str(response)

    # 生成追问提示，引导用户回到健身话题
    follow_up = "你还有什么健身相关的问题吗？比如：\n- 想要一个训练计划\n- 动作指导\n- 饮食分析"
    if memory_summary or recent_turns:
        follow_up = "关于之前的训练计划或对话内容，你还有什么想了解的吗？\n- 需要调整训练安排吗\n- 有其他健身问题吗"

    return {
        "general_response": answer.strip(),
        "status": "success",
        "follow_up_questions": [follow_up],
    }
