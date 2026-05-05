"""
多意图并行输出 - 单元测试 + 集成测试
"""
import sys
import json
import copy

sys.path.insert(0, ".")

from Agent.workflow import (
    _build_execution_plan,
    _normalize_node_output,
    _merge_multi_intent_outputs,
)


PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        extra = f" | {detail}" if detail else ""
        print(f"  ❌ {name}{extra}")


# =============================================================================
# 测试 _build_execution_plan
# =============================================================================
def test_build_execution_plan():
    print("\n--- _build_execution_plan ---")

    # 单意图（无 secondary）
    state = {
        "primary_intent": "training_guidance",
        "intents": [{"type": "training_guidance", "confidence": 0.9}],
    }
    plan = _build_execution_plan(state)
    check("单意图仅返回 primary", len(plan) == 1 and plan[0]["role"] == "primary")

    # 有 secondary，置信度足够
    state = {
        "primary_intent": "training_plan",
        "intents": [
            {"type": "training_plan", "confidence": 0.85},
            {"type": "training_guidance", "confidence": 0.72},
        ],
    }
    plan = _build_execution_plan(state)
    check("双意图（置信度够）返回2项",
          len(plan) == 2
          and plan[0] == {"intent": "training_plan", "role": "primary"}
          and plan[1] == {"intent": "training_guidance", "role": "secondary"})

    # 有 secondary，但置信度不足
    state = {
        "primary_intent": "training_plan",
        "intents": [
            {"type": "training_plan", "confidence": 0.85},
            {"type": "training_guidance", "confidence": 0.3},
        ],
    }
    plan = _build_execution_plan(state)
    check("secondary置信度<0.5被过滤", len(plan) == 1)

    # secondary 与 primary 相同 → 去重
    state = {
        "primary_intent": "training_plan",
        "intents": [
            {"type": "training_plan", "confidence": 0.85},
            {"type": "training_plan", "confidence": 0.70},
        ],
    }
    plan = _build_execution_plan(state)
    check("secondary与primary相同则去重", len(plan) == 1)

    # primary 为 general → 不执行 secondary
    state = {
        "primary_intent": "general",
        "intents": [
            {"type": "general", "confidence": 0.5},
            {"type": "training_guidance", "confidence": 0.8},
        ],
    }
    plan = _build_execution_plan(state)
    check("primary为general时跳过secondary", len(plan) == 1)

    # 无 primary_intent
    state = {"primary_intent": None, "intents": []}
    plan = _build_execution_plan(state)
    check("无primary_intent返回空数组", plan == [])


# =============================================================================
# 测试 _normalize_node_output
# =============================================================================
def test_normalize_node_output():
    print("\n--- _normalize_node_output ---")

    # guidance
    raw = {"guidance": "深蹲要...", "status": "success", "metadata": {"source": "fitness"}}
    n = _normalize_node_output(raw, "training_guidance")
    check("guidance→payload", n["payload"] == "深蹲要..." and n["intent"] == "training_guidance")

    # planning
    raw = {"plan": {"goal": "增肌"}, "status": "need_info", "follow_up_questions": ["几天？"], "waiting_info": {"missing": ["frequency"]}, "pending_intent": "training_plan", "pending_entities": {"goal": "增肌"}}
    n = _normalize_node_output(raw, "training_plan")
    check("planning→payload为dict", n["payload"] == {"goal": "增肌"})
    check("planning→保留pending/waiting", n["pending_intent"] == "training_plan" and n["waiting_info"] == {"missing": ["frequency"]})

    # diet_analysis
    raw = {"analysis_result": "鸡胸肉：...", "status": "success"}
    n = _normalize_node_output(raw, "diet_analysis")
    check("diet→payload", n["payload"] == "鸡胸肉：...")

    # meal_planning
    raw = {"analysis_result": "早餐：...", "status": "success"}
    n = _normalize_node_output(raw, "meal_planning")
    check("meal→payload", n["payload"] == "早餐：...")

    # general
    raw = {"general_response": "你好！", "status": "success"}
    n = _normalize_node_output(raw, "general")
    check("general→payload", n["payload"] == "你好！")

    # 空 follow_up_questions 默认为 []
    raw = {"guidance": "test", "status": "success"}
    n = _normalize_node_output(raw, "training_guidance")
    check("空follow_up默认为[]", n["follow_up_questions"] == [])


# =============================================================================
# 测试 _merge_multi_intent_outputs
# =============================================================================
def test_merge_multi_intent_outputs():
    print("\n--- _merge_multi_intent_outputs ---")

    # 场景5：无次意图 → 行为不变
    primary = {"guidance": "深蹲标准动作：...", "status": "success", "primary_intent": "training_guidance"}
    plan = [{"intent": "training_guidance", "role": "primary"}]
    merged = _merge_multi_intent_outputs(copy.deepcopy(primary), None, plan)
    check("无次意图→guidance顶层保留", merged.get("guidance") == "深蹲标准动作：...")
    check("无次意图→primary_output=primary", merged.get("primary_output") is not None)
    check("无次意图→secondary_output=None", merged.get("secondary_output") is None)
    check("无次意图→multi_intent.enabled=False", merged["multi_intent"]["enabled"] == False)

    # 场景2：主=training_plan(success) + 次=training_guidance(success)
    # → 顶层仍是plan，guidance只在secondary_output
    primary = {"plan": {"goal": "muscle_gain", "plan": []}, "status": "success", "primary_intent": "training_plan"}
    secondary = _normalize_node_output({"guidance": "波比跳要领：...", "status": "success"}, "training_guidance")
    plan = [
        {"intent": "training_plan", "role": "primary"},
        {"intent": "training_guidance", "role": "secondary"},
    ]
    merged = _merge_multi_intent_outputs(copy.deepcopy(primary), secondary, plan)
    check("主plan→顶层plan保留", merged.get("plan") == {"goal": "muscle_gain", "plan": []})
    check("主plan→顶层guidance为空", merged.get("guidance", "") == "")
    check("主plan→secondary_output非空", merged.get("secondary_output") is not None)
    check("主plan→secondary_output含guidance", merged["secondary_output"]["payload"] == "波比跳要领：...")
    check("主plan→multi_intent.enabled=True", merged["multi_intent"]["enabled"] == True)

    # 场景3：主=training_guidance(success) + 次=meal_planning(success)
    primary = {"guidance": "硬拉标准动作：...", "status": "success", "primary_intent": "training_guidance"}
    secondary = _normalize_node_output({"analysis_result": "减脂餐：早餐燕麦...", "status": "success"}, "meal_planning")
    plan = [
        {"intent": "training_guidance", "role": "primary"},
        {"intent": "meal_planning", "role": "secondary"},
    ]
    merged = _merge_multi_intent_outputs(copy.deepcopy(primary), secondary, plan)
    check("主guidance→顶层guidance保留", merged.get("guidance") == "硬拉标准动作：...")
    check("主guidance→顶层analysis_result为空", merged.get("analysis_result", "") == "")
    check("主guidance→secondary_output.meal存在", merged["secondary_output"]["payload"] == "减脂餐：早餐燕麦...")

    # 场景4：主失败 + 次成功 → status仍体现主失败，metadata标记fallback
    primary = {"guidance": "", "status": "not_found", "primary_intent": "training_guidance", "metadata": {}}
    secondary = _normalize_node_output({"analysis_result": "鸡蛋：每100g...", "status": "success"}, "diet_analysis")
    plan = [
        {"intent": "training_guidance", "role": "primary"},
        {"intent": "diet_analysis", "role": "secondary"},
    ]
    merged = _merge_multi_intent_outputs(copy.deepcopy(primary), secondary, plan)
    check("主失败→顶层status仍为not_found", merged.get("status") == "not_found")
    check("主失败→metadata.fallback_used=True", merged.get("metadata", {}).get("fallback_used") == True)
    check("主失败→secondary_output有效", merged.get("secondary_output") is not None)

    # 次意图的 follow_up_questions 追加到主意图尾部
    primary = {"guidance": "深蹲动作...", "status": "success", "primary_intent": "training_guidance", "follow_up_questions": ["还有什么问题？"]}
    secondary = _normalize_node_output({"analysis_result": "菜单：...", "status": "success", "follow_up_questions": ["需要调整食谱吗？"]}, "meal_planning")
    plan = [
        {"intent": "training_guidance", "role": "primary"},
        {"intent": "meal_planning", "role": "secondary"},
    ]
    merged = _merge_multi_intent_outputs(copy.deepcopy(primary), secondary, plan)
    merged_follow_ups = merged.get("follow_up_questions") or []
    check("次意图追问追加到主意图追问",
          len(merged_follow_ups) == 2
          and "meal_planning" in merged_follow_ups[1])


# =============================================================================
# 集成测试（场景1：need_info 跳过次意图）
# =============================================================================
def test_scenario_need_info_skips_secondary():
    """
    场景1: 主=training_plan(need_info), 次=training_guidance
    预期: 不执行次意图，只返回主追问
    """
    print("\n--- 集成：need_info跳过次意图 ---")

    # 模拟 intent 分类结果
    initial_state = {
        "user_input": "给我一个训练计划",
        "primary_intent": "training_plan",
        "intents": [
            {"type": "training_plan", "confidence": 0.85},
            {"type": "training_guidance", "confidence": 0.70},
        ],
        "pending_intent": None,
        "pending_entities": None,
        "waiting_info": None,
        "profile": {},
        "entities": {},
        "long_memory": None,
        "recent_turns": [],
        "guidance": "",
        "plan": {},
        "analysis_result": "",
        "general_response": "",
        "follow_up_questions": [],
        "status": "",
        "messages": [],
        "metadata": {},
        "session_id": "test",
    }

    plan = _build_execution_plan(initial_state)
    check("场景1→plan含secondary", len(plan) == 2)

    # 模拟主意图返回 need_info
    mock_primary_result = {
        "primary_intent": "training_plan",
        "status": "need_info",
        "plan": {},
        "follow_up_questions": ["请告诉我你的训练目标（增肌/减脂）？"],
        "waiting_info": {"missing": ["goal"]},
        "pending_intent": "training_plan",
        "pending_entities": {"goal": None},
    }

    # 验证：need_info 时不应执行 secondary
    from Agent.workflow import SECONDARY_CONFIDENCE_THRESHOLD
    should_run_secondary = (
        mock_primary_result.get("status") != "need_info"
        and len(plan) >= 2
    )
    check("场景1→need_info跳过secondary", should_run_secondary == False)


# =============================================================================
# 主入口
# =============================================================================
if __name__ == "__main__":
    test_build_execution_plan()
    test_normalize_node_output()
    test_merge_multi_intent_outputs()
    test_scenario_need_info_skips_secondary()

    total = PASS + FAIL
    print(f"\n{'='*50}")
    print(f"结果: {PASS}/{total} 通过" + (f", {FAIL} 失败" if FAIL else " ✅ 全部通过"))
    print(f"{'='*50}")
