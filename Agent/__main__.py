"""Package entrypoint for workflow smoke tests.

Run from project root:
    python -m Agent "深蹲怎么做"
    python -m Agent "给我一个训练计划" --session my_session
"""
import argparse
from .workflow import run_workflow


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Agent workflow from CLI.")
    parser.add_argument("user_input", nargs="?", help="用户输入问题")
    parser.add_argument("--session", dest="session_id", default="default", help="会话 ID")
    parsed = parser.parse_args()

    user_input = parsed.user_input
    session_id = parsed.session_id

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
    if result.get("analysis_result"):
        print(f"analysis_result: {result.get('analysis_result', '')}")
    if result.get("general_response"):
        print(f"general_response: {result.get('general_response', '')}")


if __name__ == "__main__":
    main()
