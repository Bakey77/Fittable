"""
意图分类器模块 v2
基于 codexx.md 和 意图处理流程可执行方案.md 实现
支持多意图独立评分、排序和筛选
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json
import re
from pathlib import Path
from typing import Any

from backend.services.llm import get_longcat_llm


# =============================================================================
# 意图配置：允许的意图类型
# =============================================================================
ALLOWED_INTENTS = frozenset([
    "diet_analysis",     # 食物营养分析
    "meal_planning",     # 食谱/餐食规划
    "training_guidance", # 单个动作指导
    "training_plan",     # 训练计划安排
    "general",           # 一般对话（LLM fallback）
])


# =============================================================================
# 关键词配置：按意图类型维护 strong/medium/weak/negative
# =============================================================================
INTENT_KEYWORDS = {
    "diet_analysis": {
        "strong": [
            "多少", "几克", "几卡", "多少卡", "含量",
            "热量多少", "蛋白质多少", "脂肪多少", "碳水多少"
        ],
        "medium": [
            "热量", "蛋白质", "脂肪", "碳水", "卡路里",
            "营养成分", "营养"
        ],
        "weak": [
            "分析", "计算", "估算", "帮我算"
        ],
        "negative": [
            "推荐", "吃什么", "怎么搭配"
        ]
    },
    "meal_planning": {
        "strong": [
            "推荐", "吃什么", "怎么吃", "怎么搭配",
            "食谱", "菜单", "一日三餐", "早餐吃什么"
        ],
        "medium": [
            "减脂餐", "增肌餐", "饮食方案",
            "高蛋白", "低脂", "低碳"
        ],
        "weak": [
            "安排饮食", "帮我搭配", "建议吃"
        ],
        "negative": [
            "多少", "几克", "热量多少"
        ]
    },
    "training_guidance": {
        "strong": [
            "怎么做", "如何做", "发力", "姿势",
            "动作要领", "注意什么", "怎么练这个动作"
        ],
        "medium": [
            "深蹲", "卧推", "硬拉", "引体向上",
            "动作", "训练技巧"
        ],
        "weak": [
            "感觉不对", "哪里错了", "有问题吗"
        ],
        "negative": [
            "一周", "计划", "安排训练"
        ]
    },
    "training_plan": {
        "strong": [
            "计划", "安排", "一周", "4周", "周期",
            "训练表", "训练安排", "每天练什么",
            "减脂","增肌","新手入门","塑形"
            
        ],
        "medium": [
            "增肌计划", "减脂计划", "新手计划",
            "训练方案"
        ],
        "weak": [
            "怎么练", "帮我设计训练"
        ],
        "negative": [
            "怎么做动作", "发力", "姿势"
        ]
    },
    "general": {
        "strong": [],
        "medium": [],
        "weak": [],
        "negative": []
    }
}


# =============================================================================
# Entity 期望字段配置
# =============================================================================
ENTITY_SCHEMA = {
    "training_plan": {
        "goal": 0.5,        # 核心字段
        "duration": 0.3,    # 带时间单位
        "frequency": 0.3    # 每次/每周
    },
    "diet_analysis": {
        "food": 0.4,        # 核心字段
        "metric": 0.4,      # 核心字段
        "weight": 0.2       # 带单位
    },
    "meal_planning": {
        "goal": 0.5,        # 核心字段
        "duration": 0.3,   # 带时间单位
        "diet_type": 0.2    # 饮食类型
    },
    "training_guidance": {
        "action": 0.6,     # 核心字段
        "problem": 0.3,    # 问题描述
        "focus": 0.1       # 关注点
    },
    "general": {}          # 无 entity 要求
}


# =============================================================================
# 置信度权重配置
# =============================================================================
CONFIDENCE_WEIGHTS = {
    "llm": 0.5,
    "keyword": 0.3,
    "entity": 0.2
}

SECONDARY_THRESHOLD = 0.5  # 次意图阈值


# =============================================================================
# 意图词 / 目标词分离配置
# 意图词：表达用户要什么类型的回答（answer type），优先级最高
# 目标词：表达用户的健身目标（fitness goal），次优先级
# =============================================================================
# 意图词列表：这些词命中表示用户要某个类型的回答，而非仅仅表达目标
INTENT_KEYWORDS_ONLY = {
    # training_guidance：请求动作指导/技术细节
    "training_guidance": [
        "怎么做", "如何做", "发力", "姿势", "动作要领",
        "注意什么", "怎么练", "动作指导", "动作技巧",
        "怎么发力", "怎么蹲", "怎么推", "怎么拉",
        "关节", "肌肉", "核心", "拉伸", "热身"
    ],
    # meal_planning：请求食谱/餐食安排
    "meal_planning": [
        "吃", "食谱", "菜单", "早餐", "午餐", "晚餐",
        "三餐", "加餐", "饮食", "做饭", "烹饪",
        "推荐吃", "吃什么", "怎么吃", "怎么搭配"
    ],
    # training_plan：请求训练安排/计划表（不含目标词）
    "training_plan": [
        "计划", "安排", "训练表", "每周练", "每天练",
        "一周", "4周", "周期", "日程", "课表"
    ],
    # diet_analysis：请求营养数据/计算
    "diet_analysis": [
        "多少", "几克", "几卡", "热量", "蛋白质",
        "脂肪", "碳水", "卡路里", "营养成分", "含量"
    ],
}

GOAL_KEYWORDS = {
    "training_plan": ["增肌", "减脂", "新手入门", "塑形"],
    "meal_planning": ["减脂餐", "增肌餐", "高蛋白", "低脂", "低碳"],
}


# =============================================================================
# 工具函数：计算关键词得分
# =============================================================================
def compute_keyword_score(text: str, intent_type: str) -> float:
    """
    两阶段得分：
    1. 意图词命中（INTENT_KEYWORDS_ONLY）→ 直接返回 1.0，意图词优先级最高
    2. 目标词命中（GOAL_KEYWORDS）→ 返回 0.6
    3. 其余走原 strong/medium/weak 链路

    意图词命中压制目标词命中，体现"用户要什么类型的回答"优先于"用户的健身目标"
    """
    # 阶段1：意图词命中 → 已禁用，改为融入阶段3的 strong 词中
    # if intent_type in INTENT_KEYWORDS_ONLY:
    #     if any(kw in text for kw in INTENT_KEYWORDS_ONLY[intent_type]):
    #         return 1.0

    # 阶段2：目标词命中（goal 次优先级）
    if intent_type in GOAL_KEYWORDS:
        if any(kw in text for kw in GOAL_KEYWORDS[intent_type]):
            return 0.6

    # 阶段3：原有 strong/medium/weak 链路（兜底）
    config = INTENT_KEYWORDS.get(intent_type, {})
    score = 0.0

    def match(words: list[str]) -> bool:
        return any(w in text for w in words)

    if match(config.get("strong", [])):
        score += 0.6
    if match(config.get("medium", [])):
        score += 0.3
    if match(config.get("weak", [])):
        score += 0.1
    if match(config.get("negative", [])):
        score -= 0.4

    return max(0.0, min(score, 1.0))


# =============================================================================
# 工具函数：计算 entity 完整性得分
# =============================================================================
def compute_entity_score(intent_type: str, entities: dict[str, Any]) -> float:
    """
    根据实体结构完整性计算得分

    Args:
        intent_type: 意图类型
        entities: LLM 提取的实体字典

    Returns:
        0-1 之间的得分
    """
    schema = ENTITY_SCHEMA.get(intent_type, {})
    if not schema:
        return 0.0

    score = 0.0
    for field, weight in schema.items():
        if field not in entities:
            continue

        value = entities[field]
        # 时间类字段检查单位
        if field in ("duration", "frequency"):
            if any(u in str(value) for u in ["天", "周", "月", "每"]):
                score += weight
        # 重量类字段检查单位
        elif field == "weight":
            if any(u in str(value) for u in ["g", "克"]):
                score += weight
        else:
            # 普通字段直接给分
            score += weight

    return round(min(score, 1.0), 2)


# =============================================================================
# 工具函数：计算最终置信度
# =============================================================================
def compute_final_confidence(
    llm_confidence: float,
    keyword_score: float,
    entity_score: float
) -> float:
    """
    按权重合并三项得分

    Args:
        llm_confidence: LLM 返回的置信度
        keyword_score: 关键词匹配得分
        entity_score: 实体完整性得分

    Returns:
        0-1 之间的最终得分
    """
    final = (
        CONFIDENCE_WEIGHTS["llm"] * llm_confidence +
        CONFIDENCE_WEIGHTS["keyword"] * keyword_score +
        CONFIDENCE_WEIGHTS["entity"] * entity_score
    )
    return round(min(final, 1.0), 3)


# =============================================================================
# 工具函数：解析 JSON
# =============================================================================
def parse_json_from_text(text: str) -> dict[str, Any]:
    """
    从 LLM 返回的文本中提取 JSON
    支持三种方式：代码块、直接 JSON、正则提取
    """
    cleaned = text.strip()

    # 方式1：Markdown 代码块
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()

    # 方式2：直接解析
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 方式3：正则提取 {...}
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return {}


# =============================================================================
# 意图分类器
# =============================================================================
class IntentClassifier:
    def __init__(self, prompt_path: str | None = None, llm: Any | None = None):
        """
        初始化分类器

        Args:
            prompt_path: 可选，自定义 prompt 文件路径
            llm: 可选，指定 LLM 实例
        """
        root = Path(__file__).resolve().parents[1]
        prompt_file = Path(prompt_path) if prompt_path else root / "意图分类 Prompt v2.md"
        self.prompt_template = prompt_file.read_text(encoding="utf-8")
        self.llm = llm or get_longcat_llm()

    def classify(self, user_input: str) -> dict[str, Any]:
        """
        对用户输入进行意图分类

        Args:
            user_input: 用户原始输入文本

        Returns:
            {
                "intents": [...],           # 所有候选意图（含得分）
                "primary_intent": {...},     # 主意图
                "secondary_intent": {...} | None,  # 次意图（可选）
            }
        """
        # 1. 调用 LLM 获取候选意图列表
        prompt = self.prompt_template.replace("{user_input}", user_input.strip())
        response = self.llm.invoke([{"role": "user", "content": prompt}])
        raw = response.content if hasattr(response, "content") else str(response)
        parsed = parse_json_from_text(raw)

        # 2. 提取 llm_intents（支持两种格式回退）
        llm_intents = parsed.get("intents", [])
        if not isinstance(llm_intents, list) or not llm_intents:
            # Fallback: 尝试从 single intent 格式回退
            primary = parsed.get("primary_intent")
            if primary and primary in ALLOWED_INTENTS:
                llm_intents = [{
                    "type": primary,
                    "llm_confidence": parsed.get("llm_confidence", 0.5),
                    "entities": parsed.get("entities", {})
                }]
            else:
                llm_intents = []

        # 3. 对每个意图独立计算各项得分
        scored_intents = []
        for intent in llm_intents:
            intent_type = intent.get("type")
            if intent_type not in ALLOWED_INTENTS:
                continue

            llm_conf = intent.get("llm_confidence", 0.5)
            entities = intent.get("entities", {})

            kw_score = compute_keyword_score(user_input, intent_type)
            ent_score = compute_entity_score(intent_type, entities)
            final_score = compute_final_confidence(llm_conf, kw_score, ent_score)

            scored_intents.append({
                "type": intent_type,
                "confidence": final_score,
                "llm_confidence": llm_conf,
                "keyword_score": kw_score,
                "entity_score": ent_score,
                "entities": entities
            })

        # 4. 无有效意图时回退到 general
        if not scored_intents:
            scored_intents.append({
                "type": "general",
                "confidence": 0.0,
                "llm_confidence": 0.0,
                "keyword_score": 0.0,
                "entity_score": 0.0,
                "entities": {}
            })

        # 5. 按 final_confidence 降序排序
        scored_intents.sort(key=lambda x: x["confidence"], reverse=True)

        # 6. 构建最终输出（intents 只返回 top2）
        top2_intents = scored_intents[:2]
        result = {
            "intents": top2_intents,
            "primary_intent": scored_intents[0] if scored_intents else None
        }

        # 7. 次意图：取第2个且 confidence >= 0.5
        if len(scored_intents) >= 2:
            secondary = scored_intents[1]
            if secondary["confidence"] >= SECONDARY_THRESHOLD:
                result["secondary_intent"] = secondary
            else:
                result["secondary_intent"] = None
        else:
            result["secondary_intent"] = None

        # 8. 同分并列处理：若 top2 同分，都按主意图处理
        if (len(scored_intents) >= 2 and
            scored_intents[0]["confidence"] == scored_intents[1]["confidence"]):
            # 并列意图都标记为 primary，不输出 secondary
            result["primary_intents"] = [
                it for it in scored_intents
                if it["confidence"] == scored_intents[0]["confidence"]
            ]
            result["secondary_intent"] = None

        return result


# =============================================================================
# 单例与便捷函数
# =============================================================================
_classifier: IntentClassifier | None = None


def classify_intent(user_input: str) -> dict[str, Any]:
    """
    便捷函数：对用户输入进行意图分类
    """
    global _classifier
    if _classifier is None:
        _classifier = IntentClassifier()
    return _classifier.classify(user_input)


# =============================================================================
# 命令行入口
# =============================================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Intent classifier for Fit-Agent")
    parser.add_argument("message", help="User input to classify")
    args = parser.parse_args()

    result = classify_intent(args.message)
    print(json.dumps(result, ensure_ascii=False, indent=2))
