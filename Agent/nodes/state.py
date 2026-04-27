"""
LangGraph State 定义

数据流说明：
1. 来源：
- run_workflow 组装 initial_state 注入大部分字段
- 各节点执行后增量更新对应字段
2. 去向：
- 字段在节点之间沿 state 传递
- workflow 结束后由 run_workflow 读取关键字段写回短期记忆并返回给调用方
"""
from typing import Any, TypedDict


class AgentState(TypedDict):
    """
    Agent 状态容器（节点间共享）

    参数/字段来源:
    - user_input/profile/waiting_info/pending_*: 来自 run_workflow 入参或 session memory 回填
    - intents/primary_intent/entities: 来自 intent_classifier_node
    - guidance/plan/status/follow_up_questions/metadata: 来自 guidance_node 或 planning_node
    - memory_summary/recent_turns: 来自短期记忆读取

    输出/流向:
    - 被 LangGraph 各节点读取并更新
    - 最终 result 返回给 run_workflow，再写回 session memory + 返回上层调用方
    """
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
    long_memory: str | None         # 长期记忆 markdown（持久化存储）

    # 输出
    retrieved_content: str            # 检索到的内容
    guidance: str                     # 动作指导回答
    plan: dict[str, Any]             # 训练计划
    analysis_result: str              # 饮食营养分析结果
    general_response: str             # 一般对话回复
    follow_up_questions: list[str]  # 追问问题
    status: str                      # 当前状态
    messages: list[dict[str, str]]   # 对话历史
    metadata: dict[str, Any]         # 元信息（来源、分数等）
