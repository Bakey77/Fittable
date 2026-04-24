"""Workout Agent 节点模块"""
from .state import AgentState

__all__ = [
    "AgentState",
    "intent_classifier_node",
    "check_waiting_node",
    "guidance_node",
    "planning_node",
]


def __getattr__(name: str):
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
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
