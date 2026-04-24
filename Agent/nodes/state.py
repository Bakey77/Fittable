"""LangGraph State 定义"""
from typing import Any, TypedDict


class AgentState(TypedDict):
    """Agent 状态"""
    user_input: str                   # 用户输入
    profile: dict[str, Any]          # 用户画像（goal, frequency, duration）
    intents: list[dict[str, Any]]    # 分类结果列表
    primary_intent: str | None       # 主意图
    entities: dict[str, Any]         # 实体提取结果

    # 多轮状态
    waiting_info: dict | None         # {"missing": ["goal", "frequency"]}
    pending_intent: str | None        # 上一轮识别的意图
    pending_entities: dict | None     # 上一轮已提取的实体

    # 短期记忆（可选字段，不影响现有节点逻辑）
    session_id: str | None           # 当前 session ID
    memory_summary: str | None       # 历史摘要（裁剪后）
    recent_turns: list[dict[str, str]] | None  # 最近对话轮次

    # 输出
    retrieved_content: str            # 检索到的内容
    guidance: str                     # 动作指导回答
    plan: dict[str, Any]             # 训练计划
    follow_up_questions: list[str]  # 追问问题
    status: str                      # 当前状态
    messages: list[dict[str, str]]   # 对话历史
    metadata: dict[str, Any]         # 元信息（来源、分数等）
