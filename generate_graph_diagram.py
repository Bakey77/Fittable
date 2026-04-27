#!/usr/bin/env python3
"""使用 LangGraph 的 get_graph() 生成工作流程图

生成后的 Mermaid 图可用于 README 或 Mermaid Live Editor 渲染。

Usage:
    python generate_graph_diagram.py                    # 输出 Mermaid 语法
    python generate_graph_diagram.py -f png -o graph.png  # 输出 PNG
    python generate_graph_diagram.py -f svg -o graph.svg  # 输出 SVG
"""

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv
load_dotenv()

from Agent.workflow import get_workflow


def main():
    parser = argparse.ArgumentParser(
        description="生成 Fit-Agent 工作流图",
        epilog="""
工作流节点说明：
  intent_classifier      - LLM 意图分类 + 实体提取
  guidance_node          - 单个动作指导（发力点/姿势）
  planning_node          - 训练计划生成（goal + frequency）
  diet_analysis_node     - 食物营养分析
  meal_planning_node     - 餐食/食谱规划
  general_conversation_node - 通用对话（LLM fallback）

条件边逻辑：
  _route_pending_or_intent:
    - pending_intent 存在 → 直接路由到对应节点（多轮追问恢复）
    - 否则 → 基于 primary_intent 路由

  _route_by_intent:
    - training_guidance → guidance_node
    - training_plan → planning_node
    - diet_analysis → diet_analysis_node
    - meal_planning → meal_planning_node
    - general → general_conversation_node
    - 其他 → __end__
        """
    )
    parser.add_argument(
        "--format", "-f",
        choices=["mermaid", "graphviz", "png", "svg"],
        default="mermaid",
        help="输出格式: mermaid (默认), png, svg"
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="输出文件路径"
    )
    args = parser.parse_args()

    app = get_workflow()
    graph = app.get_graph()

    if args.format == "mermaid":
        mermaid_code = graph.draw_mermaid()

        if args.output:
            args.output.write_text(mermaid_code, encoding="utf-8")
            print(f"已生成 Mermaid 图: {args.output}")
        else:
            print(mermaid_code)

    elif args.format == "graphviz":
        try:
            dot_code = graph.draw_graphviz()
            if args.output:
                args.output.write_text(dot_code, encoding="utf-8")
                print(f"已生成 Graphviz 图: {args.output}")
            else:
                print(dot_code)
        except Exception as e:
            print(f"生成 Graphviz 失败: {e}")

    elif args.format == "png":
        try:
            png_data = graph.draw_mermaid_png()
            if args.output:
                args.output.write_bytes(png_data)
                print(f"已生成 PNG 图: {args.output}")
            else:
                print("PNG data length:", len(png_data), "bytes")
        except Exception as e:
            print(f"生成 PNG 失败: {e}")
            print("尝试使用 --format=mermaid 然后用 Mermaid Live Editor 转换")

    elif args.format == "svg":
        try:
            svg_data = graph.draw_mermaid_svg()
            if args.output:
                args.output.write_text(svg_data, encoding="utf-8")
                print(f"已生成 SVG 图: {args.output}")
            else:
                print(svg_data)
        except Exception as e:
            print(f"生成 SVG 失败: {e}")


if __name__ == "__main__":
    main()