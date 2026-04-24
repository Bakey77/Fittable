"""Package entrypoint for workflow smoke tests.

Run from project root:
    python -m Agent "深蹲怎么做"
    python -m Agent "给我一个训练计划" --session my_session
"""
import sys
import json
from .workflow import run_workflow


def main() -> None:
    args = sys.argv[1:]
    session_id = "default"

    # 解析参数
    user_input = None
    for i, arg in enumerate(args):
        if arg == "--session" and i + 1 < len(args):
            session_id = args[i + 1]
        elif not arg.startswith("--"):
            user_input = arg

    if not user_input:
        user_input = input("请输入问题: ").strip()
        if not user_input:
            print("未输入问题，退出。")
            return

    result = run_workflow(user_input, session_id=session_id)

    print(f"session_id: {session_id}")
    print(f"status: {result.get('status')}")
    print(f"intent: {result.get('primary_intent')}")
    print(f"follow_up: {result.get('follow_up_questions')}")
    print(f"waiting_info: {result.get('waiting_info')}")

    if result.get("guidance"):
        print(f"guidance (前300字): {result.get('guidance', '')[:300]}")
    if result.get("plan"):
        plan = result.get("plan", {})
        print(f"plan: goal={plan.get('goal')}, days={len(plan.get('plan', []))}")


if __name__ == "__main__":
    main()
