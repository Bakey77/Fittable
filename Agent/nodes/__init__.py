"""Workout Agent 节点模块"""
from .state import AgentState

__all__ = [
    "AgentState",
    "intent_classifier_node",
    "check_waiting_node",
    "guidance_node",
    "planning_node",
    "diet_analysis_node",
    "meal_planning_node",
]


def __getattr__(name: str):
    """
    参数:
    - name: 访问的导出符号名
      来源: 外部 `from Agent.nodes import ...` 或属性访问

    输出:
    - 对应节点函数对象或 AgentState 类型

    流向:
    - 用于按需加载节点模块，避免包初始化时提前触发重依赖导入
    """
    # Lazy import: avoid pulling optional runtime deps at package import time.
    if name == "intent_classifier_node":
        from .intent_classifier import intent_classifier_node

        return intent_classifier_node
    if name == "check_waiting_node":
        from .check_waiting import check_waiting_node

        return check_waiting_node
    if name == "guidance_node":
        from .guidance import guidance_node

        return guidance_node
    if name == "planning_node":
        from .planning import planning_node

        return planning_node
    if name == "diet_analysis_node":
        from .diet_analysis_node import diet_analysis_node

        return diet_analysis_node
    if name == "meal_planning_node":
        from .meal_planning_node import meal_planning_node

        return meal_planning_node
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
