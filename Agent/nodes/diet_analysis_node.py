"""饮食分析节点"""
import os
import sys
import json
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.search_with_tavily import search_with_tavily


# =============================================================================
# 食物数据加载器
# =============================================================================
class FoodDataSearcher:
    """食物营养数据检索器"""

    _instance: "FoodDataSearcher | None" = None
    _food_data: list[dict[str, Any]] | None = None

    @classmethod
    def get_instance(cls) -> "FoodDataSearcher":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _load_food_data(self) -> list[dict[str, Any]]:
        """懒加载食物数据"""
        if self._food_data is None:
            food_data_path = ROOT_DIR / "uploads" / "food_data.json"
            if not food_data_path.exists():
                return []
            with open(food_data_path, "r", encoding="utf-8") as f:
                self._food_data = json.load(f)
        return self._food_data or []

    def _format_food_item(self, item: dict[str, Any]) -> str:
        """格式化单个食物项

        Args:
            item: 食物数据字典

        Returns:
            格式化字符串，如：
            "白米饭：主食，每100g有 130kcal热量，2.5g蛋白质，0.4g脂肪，29.0g碳水。来源merged_dataset"
        """
        food_name = item.get("food_name", "")
        category = item.get("category", "")
        serving_size = item.get("serving_size", "100g")
        kcal = item.get("kcal_per_serving", 0)
        protein = item.get("protein_per_100g", 0)
        fat = item.get("fat_per_100g", 0)
        carbs = item.get("carbs_per_100g", 0)
        source = item.get("source", "")

        return (
            f"{food_name}：{category}，每{serving_size}有 "
            f"{kcal}kcal热量，{protein}g蛋白质，{fat}g脂肪，{carbs}g碳水。"
            f"来源{source}"
        )

    def _fuzzy_match(self, query: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """模糊匹配：query 匹配 food_name 或 aliases 中的任意一项

        Args:
            query: 用户查询（已预处理）
            items: 食物数据列表

        Returns:
            匹配的食物项列表
        """
        query_lower = query.lower()
        results = []

        for item in items:
            # 匹配 food_name
            food_name = item.get("food_name", "").lower()
            if query_lower in food_name or food_name in query_lower:
                results.append(item)
                continue

            # 匹配 aliases
            aliases = item.get("aliases", [])
            for alias in aliases:
                alias_lower = alias.lower()
                if query_lower in alias_lower or alias_lower in query_lower:
                    results.append(item)
                    break

        return results

    def search(self, query: str, exact_first: bool = True) -> list[str]:
        """搜索食物并返回格式化结果

        Args:
            query: 用户查询（如"米饭"、"鸡胸肉热量"）
            exact_first: True=先精确再模糊，False=仅模糊

        Returns:
            格式化后的食物营养信息列表
        """
        items = self._load_food_data()
        if not items:
            return []

        # 预处理 query：提取食物名称（去掉"多少卡"、"热量"等后缀）
        clean_query = query.replace("多少卡", "").replace("热量", "").replace("蛋白质", "").replace("脂肪", "").replace("碳水", "").strip()

        results = []

        # 1. 精确匹配：query 完全匹配 food_name
        exact_matches = [
            item for item in items
            if clean_query == item.get("food_name", "").lower() or
               clean_query in [alias.lower() for alias in item.get("aliases", [])]
        ]
        if exact_matches:
            results.extend([self._format_food_item(item) for item in exact_matches])

        # 2. 模糊匹配：query 作为子串匹配
        if not results or exact_first is False:
            fuzzy_matches = self._fuzzy_match(clean_query, items)
            # 排除已精确匹配的项目
            seen_ids = {item.get("food_name") for item in exact_matches}
            for item in fuzzy_matches:
                if item.get("food_name") not in seen_ids:
                    results.append(self._format_food_item(item))
                    seen_ids.add(item.get("food_name"))

        return results


# =============================================================================
# Tavily 兜底搜索（饮食分析专用）
# =============================================================================
def _tavily_search_diet(
    query: str,
    long_memory: str | None = None,
    memory_summary: str | None = None,
) -> str | None:
    """使用 Tavily 搜索食物营养信息

    Args:
        query: 用户查询
        long_memory: 长期记忆 markdown
        memory_summary: 短期记忆历史摘要

    Returns:
        格式化后的营养信息字符串，失败返回 None
    """
    try:
        text = search_with_tavily(query)
        if not text:
            return None

        # 构建提示让 LLM 提取营养信息并格式化为统一格式
        from backend.services.llm import get_llm

        mem_parts = []
        if long_memory:
            mem_parts.append(f"用户长期记忆：\n{long_memory}")
        if memory_summary:
            mem_parts.append(f"对话历史摘要：\n{memory_summary}")
        mem_section = "\n".join(mem_parts)
        mem_block = f"\n{mem_section}\n" if mem_section else "\n"

        prompt = f"""你是营养分析助手。请结合以下用户记忆上下文，从搜索结果中提取食物营养信息。{mem_block}
用户查询：{query}

请从以下搜索结果中提取食物营养信息，并按以下统一格式返回（只返回格式化的结果，不要其他内容）：
{{食物名称}}：{{类别}}，每{{份量}}有 {{kcal}}kcal热量，{{蛋白质}}g蛋白质，{{脂肪}}g脂肪，{{碳水}}g碳水。来源{{来源}}

格式要求：
1. 如果找到多种食物，每条一行
2. 份量格式统一为"100g"或"每100g"
3. kcal、蛋白质、脂肪、碳水用数字表示
4. 如果搜索结果中没有营养信息，返回"未找到相关营养数据"
5. 不能编造没有的食物营养信息

搜索结果：
{text}
"""
        llm = get_llm()
        response = llm.invoke([{"role": "user", "content": prompt}])
        result = response.content if hasattr(response, "content") else str(response)
        return result.strip() if result else None
    except Exception:
        return None


# =============================================================================
# 饮食分析节点
# =============================================================================
def diet_analysis_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    参数:
    - state: 当前工作流状态
      关键读取字段:
      1) user_input: 用户输入文本
      2) long_memory: 长期记忆 markdown
      3) memory_summary: 短期记忆历史摘要

    输出:
    - analysis_result: 饮食营养分析结果
    - status: success | not_found
    - metadata: 检索来源信息（source/hit_ids）

    流向:
    - 输出回到 workflow 主流程并到 END
    """
    user_input = state["user_input"]
    long_memory = state.get("long_memory")
    memory_summary = state.get("memory_summary")

    # 初始化食物检索器
    searcher = FoodDataSearcher.get_instance()

    # 1. 先在 food_data.json 中检索
    food_results = searcher.search(user_input)

    # 2. 格式化输出
    if food_results:
        analysis_result = "\n".join(food_results)
        return {
            "analysis_result": analysis_result,
            "status": "success",
            "metadata": {
                "source": "food_data",
                "count": len(food_results),
            },
        }

    # 3. food_data 未命中，fallback 到 tavily（注入记忆上下文）
    tavily_result = _tavily_search_diet(user_input, long_memory, memory_summary)
    if tavily_result and tavily_result != "未找到相关营养数据":
        return {
            "analysis_result": tavily_result,
            "status": "success",
            "metadata": {
                "source": "tavily",
            },
        }

    # 4. 两者都未命中
    return {
        "analysis_result": "",
        "status": "not_found",
        "metadata": {
            "source": "none",
        },
        "follow_up_questions": [
            "未在食物数据库或网络搜索中找到相关营养信息，请尝试更具体的食物名称。"
        ],
    }
