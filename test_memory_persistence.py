"""
测试脚本：连续输入20轮对话，检查记忆保存能力

检查点：
1. recent_turns 是否持续增长（最多保留10轮）
2. pending_intent 是否正确路由（多轮追问场景）
3. 模型回答中是否能准确回忆用户信息
4. 长期记忆文件是否在第10轮触发写入
"""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from Agent.workflow import run_workflow
from Agent.memory.session_memory import (
    get_recent_turns,
    get_working_memory,
    clear_session_memory,
    get_session_memory,
)
from Agent.memory.long_memory import load_long_memory


def print_session_state(session_id: str, round_num: int):
    """打印当前 session 状态"""
    turns = get_recent_turns(session_id)
    working = get_working_memory(session_id)
    full = get_session_memory(session_id)
    total_turns = full.get("metadata", {}).get("total_turns", 0)
    long_mem = load_long_memory(session_id)

    print(f"\n{'='*60}")
    print(f"第 {round_num} 轮 - Session 状态")
    print(f"{'='*60}")
    print(f"recent_turns 条数: {len(turns)} / total_turns: {total_turns}")
    if turns:
        print("  最近2轮:")
        for t in turns[-2:]:
            role = "用户" if t["role"] == "user" else "助手"
            text = t["text"][:80] + ("..." if len(t["text"]) > 80 else "")
            print(f"    [{role}] {text}")
    print(f"pending_intent: {working.get('pending_intent')}")
    print(f"pending_entities: {working.get('pending_entities')}")
    print(f"waiting_info: {working.get('waiting_info')}")
    if long_mem:
        print(f"长期记忆文件存在: Agent/memory/long_term_store/{session_id}.md")
    else:
        print(f"长期记忆文件: 尚未生成")
    print(f"{'='*60}\n")


def run_test():
    session_id = "test_20rounds"

    # 清空历史
    clear_session_memory(session_id)

    print("\n" + "=" * 60)
    print("开始 20 轮对话记忆测试")
    print("=" * 60)

    # # ---------- 第1轮：建立训练目标 ----------
    # result = run_workflow("我要增肌，每周训练4天", session_id=session_id)
    # print(f"\n>>> 第1轮：建立训练目标")
    # print(f"    status: {result.get('status')}")
    # if result.get('plan'):
    #     print(f"    plan 生成成功")
    # elif result.get('follow_up_questions'):
    #     print(f"    追问: {result['follow_up_questions']}")

    # # ---------- 第2-9轮：逐步提供用户信息 ----------
    # info_rounds = [
    #     (2, "我叫小明，今年28岁", "设置名字和年龄"),
    #     (3, "我身高175cm，体重75kg", "设置身高体重"),
    #     (4, "我平时工作比较忙，经常加班", "提供生活习惯"),
    #     (5, "我住在上海浦东", "提供地区信息"),
    #     (6, "我的体脂率大概在20%左右", "提供体脂信息"),
    #     (7, "我健身已经2年了，有一定基础", "提供健身经验"),
    #     (8, "我的饮食偏川菜，比较重油重盐", "提供饮食习惯"),
    #     (9, "我希望在3个月内增肌5kg", "设置具体目标"),
    # ]

    # for round_num, user_input, description in info_rounds:
    #     print(f"\n>>> 第{round_num}轮：{description}")
    #     print(f"    用户输入: {user_input}")
    #     result = run_workflow(user_input, session_id=session_id)

    #     if result.get("general_response"):
    #         resp = result["general_response"][:80]
    #         print(f"    回复: {resp}...")
    #     elif result.get("follow_up_questions"):
    #         print(f"    追问: {result['follow_up_questions']}")
    #     elif result.get("plan"):
    #         print(f"    plan: {result.get('plan')}")
    #     else:
    #         print(f"    status: {result.get('status')}")

    # # ---------- 第10轮：验证长期记忆文件是否生成 ----------
    # print(f"\n>>> 第10轮：验证长期记忆 + 提问验证")
    # result = run_workflow("我叫什么名字？", session_id=session_id)
    # resp = result.get("general_response", "")
    # print(f"    用户输入: 我叫什么名字？")
    # print(f"    回复: {resp[:150]}")
    # if "小明" in resp:
    #     print(f"    ✓ 正确回忆名字")
    # else:
    #     print(f"    ⚠️ 未正确回忆名字")
    # print_session_state(session_id, 10)

    # ---------- 第11-14轮：继续提供信息 ----------
    info_rounds_2 = [
        (11, "我膝盖曾经受伤过，深蹲需要小心", "提供健康信息"),
        (12, "我喜欢在晚上8点训练", "提供训练时间偏好"),
        (13, "我没有健身器械，只有哑铃和俯卧撑架", "提供器械条件"),
        (14, "我每周可以训练4到5天", "确认训练频率"),
    ]

    for round_num, user_input, description in info_rounds_2:
        print(f"\n>>> 第{round_num}轮：{description}")
        print(f"    用户输入: {user_input}")
        result = run_workflow(user_input, session_id=session_id)
        if result.get("general_response"):
            print(f"    回复: {result['general_response'][:80]}...")

    # ---------- 第15-20轮：验证多信息回忆 ----------
    verification = [
        (15, "我叫什么名字？", "小明"),
        (16, "我今年多大？", "28"),
        (17, "我住在哪个城市？", "上海"),
        (18, "我膝盖有什么问题？", "膝盖"),
        (19, "我有什么器械？", "哑铃"),
        (20, "总结一下我的所有基本信息", None),
    ]

    print("\n" + "=" * 60)
    print("第15-20轮：验证记忆")
    print("=" * 60)

    for round_num, user_input, expected_keyword in verification:
        print(f"\n>>> 第{round_num}轮：{user_input}")
        if expected_keyword:
            print(f"    期望包含: {expected_keyword}")
        result = run_workflow(user_input, session_id=session_id)
        resp = result.get("general_response", "")
        print(f"    回复: {resp[:200]}")
        if expected_keyword and expected_keyword not in resp:
            print(f"    ⚠️ 期望包含 '{expected_keyword}' 但未在回复中找到")
        else:
            print(f"    ✓ 验证通过")

    # ---------- 最终状态 ----------
    print("\n" + "=" * 60)
    print("测试完成 - 最终 session 状态")
    print("=" * 60)
    turns = get_recent_turns(session_id)
    working = get_working_memory(session_id)
    full = get_session_memory(session_id)
    print(f"recent_turns 总条数: {len(turns)}")
    print(f"total_turns: {full.get('metadata', {}).get('total_turns', 0)}")
    print(f"working_memory pending_intent: {working.get('pending_intent')}")
    print(f"working_memory pending_entities: {working.get('pending_entities')}")

    long_mem = load_long_memory(session_id)
    if long_mem:
        print(f"\n长期记忆文件内容:")
        print("-" * 40)
        for k, v in long_mem.items():
            if v:
                print(f"  {k}: {str(v)[:200]}")
    else:
        print(f"\n长期记忆文件: 未生成")

    print(f"\n全部 recent_turns:")
    for i, t in enumerate(turns):
        role = "用户" if t["role"] == "user" else "助手"
        print(f"  [{i+1}][{role}] {t['text'][:100]}")


if __name__ == "__main__":
    run_test()
