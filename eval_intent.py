"""
意图分类泛化边界评估脚本
"""
import sys
import json
import time
from collections import defaultdict

sys.path.insert(0, ".")

from Agent.intent_classifier import classify_intent

# 测试样例
TEST_CASES = {
    "training_guidance": [
        "深蹲怎么做才不伤膝盖？",
        "我想减脂，波比跳动作要领是什么？",
        "硬拉发力总是腰酸，哪里错了？",
        "给我讲讲卧推姿势，顺便安排一周训练",
        "引体向上练背感觉不到，怎么调整？",
    ],
    "training_plan": [
        "我每周三天，帮我安排增肌训练表",
        "新手入门，4周怎么练？",
        "想减脂但也想知道深蹲怎么做",
        "给个每天练什么的计划，不用讲动作细节",
        "我时间很少，一周两练能怎么安排",
    ],
    "diet_analysis": [
        "100g鸡胸肉蛋白质和热量多少？",
        "一个鸡蛋大概几克蛋白质？",
        "我今天吃了米饭和牛肉，帮我估算营养",
        "减脂吃什么好，顺便说下热量多少",
        "香蕉碳水含量高吗？",
    ],
    "meal_planning": [
        "帮我做一份减脂一日三餐菜单",
        "早餐吃什么比较高蛋白？",
        "我不想算热量，直接推荐怎么吃",
        "增肌餐怎么搭配，鸡胸肉要吃多少克",
        "给我一个低碳晚餐食谱",
    ],
    "general": [
        "你好呀",
        "我叫什么名字你记得吗？",
        "今天天气怎么样",
        "讲个笑话",
        "谢谢你，晚安",
    ],
}

ALL_CATEGORIES = list(TEST_CASES.keys())


def evaluate():
    results = {cat: [] for cat in ALL_CATEGORIES}
    confusion = defaultdict(lambda: defaultdict(int))
    correct = defaultdict(int)
    total = defaultdict(int)

    for true_label, samples in TEST_CASES.items():
        print(f"\n{'='*70}")
        print(f"📂 类别: {true_label}")
        print(f"{'='*70}")

        for i, text in enumerate(samples):
            try:
                result = classify_intent(text)
            except Exception as e:
                print(f"  [{i+1}] ❌ 异常: {text[:40]}... → {e}")
                continue

            primary = result.get("primary_intent", {})
            pred_label = primary.get("type", "unknown") if primary else "unknown"
            confidence = primary.get("confidence", 0) if primary else 0
            kw_score = primary.get("keyword_score", 0) if primary else 0
            entity_score = primary.get("entity_score", 0) if primary else 0
            llm_conf = primary.get("llm_confidence", 0) if primary else 0

            sec = result.get("secondary_intent")
            sec_label = sec.get("type", None) if sec else None
            sec_conf = sec.get("confidence", 0) if sec else 0

            hit = "✅" if pred_label == true_label else "❌"
            if pred_label == true_label:
                correct[true_label] += 1
            total[true_label] += 1
            confusion[true_label][pred_label] += 1

            results[true_label].append({
                "text": text,
                "predicted": pred_label,
                "true": true_label,
                "correct": pred_label == true_label,
                "confidence": confidence,
                "keyword_score": kw_score,
                "entity_score": entity_score,
                "llm_confidence": llm_conf,
                "secondary": sec_label,
                "secondary_conf": sec_conf,
            })

            sec_str = ""
            if sec_label:
                sec_str = f" | 次意图: {sec_label}({sec_conf:.2f})"
            print(f"  {hit} [{i+1}] conf={confidence:.3f} (kw={kw_score:.2f} ent={entity_score:.2f} llm={llm_conf:.2f}){sec_str}")
            print(f"       输入: {text}")

            time.sleep(0.1)  # 避免过快调用

    # 汇总报告
    print(f"\n{'='*70}")
    print("📊 评估汇总")
    print(f"{'='*70}")

    overall_correct = sum(correct.values())
    overall_total = sum(total.values())
    print(f"\n总准确率: {overall_correct}/{overall_total} = {overall_correct/overall_total*100:.1f}%")

    print(f"\n--- 各类别准确率 ---")
    for cat in ALL_CATEGORIES:
        c = correct[cat]
        t = total[cat]
        pct = c / t * 100 if t > 0 else 0
        bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
        print(f"  {cat:25s}: {c}/{t} ({pct:5.1f}%) {bar}")

    print(f"\n--- 混淆矩阵 (行=真实, 列=预测) ---")
    header = f"{'':25s}" + "".join(f"{c:>20s}" for c in ALL_CATEGORIES)
    print(header)
    for true_cat in ALL_CATEGORIES:
        row = f"{true_cat:25s}"
        for pred_cat in ALL_CATEGORIES:
            count = confusion[true_cat][pred_cat]
            row += f"{count:>20d}"
        print(row)

    # 重点分析边界混淆
    print(f"\n--- 边界混淆分析 ---")
    for true_cat in ALL_CATEGORIES:
        for pred_cat in ALL_CATEGORIES:
            if true_cat != pred_cat and confusion[true_cat][pred_cat] > 0:
                # 列出具体样本
                cases = [(r["text"], r["confidence"], r["keyword_score"])
                         for r in results[true_cat] if r["predicted"] == pred_cat]
                print(f"\n  {true_cat} → {pred_cat} ({len(cases)}例):")
                for text, conf, kw in cases:
                    print(f"    - \"{text}\" (conf={conf:.3f}, kw={kw:.2f})")

    # 精度细节：输出低置信度的正确分类和高置信度的错误分类
    print(f"\n--- 低置信度正确分类 (conf < 0.5) ---")
    low_correct = []
    for cat in ALL_CATEGORIES:
        for r in results[cat]:
            if r["correct"] and r["confidence"] < 0.5:
                low_correct.append(r)
    if low_correct:
        for r in sorted(low_correct, key=lambda x: x["confidence"]):
            print(f"  [{r['true']}] conf={r['confidence']:.3f} \"{r['text']}\"")
    else:
        print("  (无)")

    print(f"\n--- 高置信度错误分类 (conf >= 0.5) ---")
    high_wrong = []
    for cat in ALL_CATEGORIES:
        for r in results[cat]:
            if not r["correct"] and r["confidence"] >= 0.5:
                high_wrong.append(r)
    if high_wrong:
        for r in sorted(high_wrong, key=lambda x: -x["confidence"]):
            print(f"  [{r['true']}→{r['predicted']}] conf={r['confidence']:.3f} kw={r['keyword_score']:.2f} \"{r['text']}\"")
    else:
        print("  (无)")

    print(f"\n--- 次意图触发情况 ---")
    sec_triggered = 0
    for cat in ALL_CATEGORIES:
        for r in results[cat]:
            if r["secondary"]:
                sec_triggered += 1
                print(f"  主:{r['true']}→{r['predicted']} 次:{r['secondary']}({r['secondary_conf']:.2f}) \"{r['text']}\"")
    if sec_triggered == 0:
        print("  (无)")

    return results, confusion


if __name__ == "__main__":
    evaluate()
