from .intent_classifier import IntentClassifier, classify_intent

__all__ = ["IntentClassifier", "classify_intent", "workout_agent"]


def workout_agent(*args, **kwargs):
    """
    Lazy import to avoid importing heavy runtime dependencies during package init.
    """
    from .workout_agent import workout_agent as _workout_agent

    return _workout_agent(*args, **kwargs)
