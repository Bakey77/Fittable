"""
训练指导 RAG 检索评测脚本

对比六种检索方案在 fitness_guide collection 上的表现：
  1. Vector Only              — 纯向量检索
  2. Hybrid                   — 向量检索 + BM25（RRF 融合），无 Rewrite，无 Rerank
  3. Hybrid+Rewrite           — 向量检索 + BM25 + Query Rewrite（RRF 融合）
  4. Hybrid+Rerank            — 向量检索 + BM25 + Rerank（使用 LLM 重新排序）
  5. Hybrid+Rewrite+Rerank    — 向量检索 + BM25 + Query Rewrite + Rerank

默认运行四种（vector / hybrid / hybrid_rewrite / hybrid_rerank）；hybrid_rewrite_rerank 为可选，需显式传 `--methods hybrid_rewrite_rerank` 或 `all`。

评测指标：Precision@K, Recall@K, F1@K, HitRate@K, MRR@K, nDCG@K
默认 K 值：3, 5

用法：
    python scripts/eval_workout_rag.py
    python scripts/eval_workout_rag.py --gold scripts/eval_workout_gold.json --output scripts/eval_results
    python scripts/eval_workout_rag.py --fusion-k 10 --k 3 5 10
    python scripts/eval_workout_rag.py --methods vector hybrid hybrid_rerank
    python scripts/eval_workout_rag.py --methods all  # 包含 hybrid_rewrite_rerank
"""
import os
import sys
import json
import csv
import re
import time
import argparse
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from qdrant_client import QdrantClient

from config import Config
from tools.retriever1 import (
    VectorRetriever,
    BM25Retriever,
    QueryRewriter,
    LLMReranker,
    reciprocal_rank_fusion,
)


# ─────────────────────────────────────────────
# Retriever 封装
# ─────────────────────────────────────────────

class VectorOnlyRetriever:
    """只做向量检索，不走 BM25，不走 Query 改写"""

    def __init__(self, collection_name: str, qdrant_client: QdrantClient, embed_model):
        self.collection_name = collection_name
        self.client = qdrant_client
        self.embed_model = embed_model
        self._vector_retriever = VectorRetriever(collection_name, qdrant_client, embed_model)

    def retrieve(self, query: str, top_k: int = 20) -> list:
        return self._vector_retriever.retrieve(query, top_k)


class HybridNoRewriteRetriever:
    """Hybrid Retriever，强制关闭 Query 改写"""

    def __init__(
        self,
        collection_name: str,
        qdrant_client: QdrantClient,
        embed_model,
        vector_top_k: int = 20,
        bm25_top_k: int = 20,
        fusion_top_k: int = 5,
    ):
        self.collection_name = collection_name
        self.vector_retriever = VectorRetriever(collection_name, qdrant_client, embed_model)
        self.bm25_retriever = BM25Retriever(collection_name, qdrant_client)
        self.vector_top_k = vector_top_k
        self.bm25_top_k = bm25_top_k
        self.fusion_top_k = fusion_top_k

    def retrieve(self, query: str) -> list:
        vector_results = self.vector_retriever.retrieve(query, self.vector_top_k)
        bm25_results = self.bm25_retriever.retrieve(query, self.bm25_top_k)
        fused = reciprocal_rank_fusion([vector_results, bm25_results])
        return fused[: self.fusion_top_k]


class HybridWithRewriteRetriever:
    """Hybrid Retriever + Query Rewrite（Query Rewrite 由 LLM 完成）"""

    def __init__(
        self,
        collection_name: str,
        qdrant_client: QdrantClient,
        embed_model,
        vector_top_k: int = 20,
        bm25_top_k: int = 20,
        fusion_top_k: int = 5,
    ):
        self.collection_name = collection_name
        self.vector_retriever = VectorRetriever(collection_name, qdrant_client, embed_model)
        self.bm25_retriever = BM25Retriever(collection_name, qdrant_client)
        self.query_rewriter = QueryRewriter()
        self.vector_top_k = vector_top_k
        self.bm25_top_k = bm25_top_k
        self.fusion_top_k = fusion_top_k

    def retrieve(self, query: str) -> tuple[list, str]:
        """返回 (chunks, rewritten_query)"""
        rewritten = self.query_rewriter.rewrite(query)
        vector_results = self.vector_retriever.retrieve(rewritten, self.vector_top_k)
        bm25_results = self.bm25_retriever.retrieve(rewritten, self.bm25_top_k)
        fused = reciprocal_rank_fusion([vector_results, bm25_results])
        return fused[: self.fusion_top_k], rewritten


class HybridWithRerankRetriever:
    """
    Hybrid Retriever + LLM Rerank（Query Rewrite 关闭，专注验证 Rerank 效果）
    """

    def __init__(
        self,
        collection_name: str,
        qdrant_client: QdrantClient,
        embed_model,
        vector_top_k: int = 20,
        bm25_top_k: int = 20,
        fusion_top_k: int = 5,
        rerank_top_n: int = 20,
    ):
        self.collection_name = collection_name
        self.vector_retriever = VectorRetriever(collection_name, qdrant_client, embed_model)
        self.bm25_retriever = BM25Retriever(collection_name, qdrant_client)
        self.reranker = LLMReranker(rerank_top_n=rerank_top_n)
        self.vector_top_k = vector_top_k
        self.bm25_top_k = bm25_top_k
        self.fusion_top_k = fusion_top_k

    def retrieve(self, query: str) -> list:
        """
        Hybrid 召回 top_n → LLM Rerank → 返回 top_k
        """
        vector_results = self.vector_retriever.retrieve(query, self.vector_top_k)
        bm25_results = self.bm25_retriever.retrieve(query, self.bm25_top_k)
        fused = reciprocal_rank_fusion([vector_results, bm25_results])
        candidates = fused[: self.reranker.rerank_top_n]
        reranked = self.reranker.rerank(query, candidates, self.fusion_top_k)
        return reranked


class HybridWithRewriteAndRerankRetriever:
    """
    Hybrid Retriever + Query Rewrite + LLM Rerank
    先 Rewrite 改写 Query，再 Hybrid 召回，最后 Rerank 重排
    """

    def __init__(
        self,
        collection_name: str,
        qdrant_client: QdrantClient,
        embed_model,
        vector_top_k: int = 20,
        bm25_top_k: int = 20,
        fusion_top_k: int = 5,
        rerank_top_n: int = 20,
    ):
        self.collection_name = collection_name
        self.vector_retriever = VectorRetriever(collection_name, qdrant_client, embed_model)
        self.bm25_retriever = BM25Retriever(collection_name, qdrant_client)
        self.query_rewriter = QueryRewriter()
        self.reranker = LLMReranker(rerank_top_n=rerank_top_n)
        self.vector_top_k = vector_top_k
        self.bm25_top_k = bm25_top_k
        self.fusion_top_k = fusion_top_k

    def retrieve(self, query: str) -> tuple[list, str]:
        """返回 (chunks, rewritten_query)"""
        rewritten = self.query_rewriter.rewrite(query)
        vector_results = self.vector_retriever.retrieve(rewritten, self.vector_top_k)
        bm25_results = self.bm25_retriever.retrieve(rewritten, self.bm25_top_k)
        fused = reciprocal_rank_fusion([vector_results, bm25_results])
        candidates = fused[: self.reranker.rerank_top_n]
        reranked = self.reranker.rerank(query, candidates, self.fusion_top_k)
        return reranked, rewritten


# ─────────────────────────────────────────────
# 评测指标
# ─────────────────────────────────────────────

@dataclass
class EvalResult:
    query_id: str
    query: str
    retrieved_titles: list[str]
    gold_titles: list[str]
    precision_at: dict[int, float] = field(default_factory=dict)
    recall_at: dict[int, float] = field(default_factory=dict)
    f1_at: dict[int, float] = field(default_factory=dict)
    hit_at: dict[int, bool] = field(default_factory=dict)
    mrr: float = 0.0
    ndcg_at: dict[int, float] = field(default_factory=dict)


def _normalize_title(title: str) -> str:
    return re.sub(r'[（(][^)）]*[)）]$', '', title).strip()


def _dcg(gains: list[float], k: int) -> float:
    gains = gains[:k]
    if not gains:
        return 0.0
    return sum(g / np.log2(i + 2) for i, g in enumerate(gains))


def _ndcg(retrieved_titles: list[str], gold_titles: list[str], k: int) -> float:
    gold_norm = {_normalize_title(t).lower() for t in gold_titles}
    gains = [1.0 if _normalize_title(t).lower() in gold_norm else 0.0 for t in retrieved_titles[:k]]
    if not gains or not any(gains):
        return 0.0
    ideal = sorted(gains, reverse=True)
    dcg_val = _dcg(gains, k)
    idcg_val = _dcg(ideal, k)
    return dcg_val / idcg_val if idcg_val > 0 else 0.0


def _mrr(retrieved_titles: list[str], gold_titles: list[str]) -> float:
    gold_norm = {_normalize_title(t).lower() for t in gold_titles}
    for i, t in enumerate(retrieved_titles):
        if _normalize_title(t).lower() in gold_norm:
            return 1.0 / (i + 1)
    return 0.0


def _precision_at(retrieved_titles: list[str], gold_titles: list[str], k: int) -> float:
    gold_norm = {_normalize_title(t).lower() for t in gold_titles}
    hits = sum(1 for t in retrieved_titles[:k] if _normalize_title(t).lower() in gold_norm)
    return hits / k


def _recall_at(retrieved_titles: list[str], gold_titles: list[str], k: int) -> float:
    gold_norm = {_normalize_title(t).lower() for t in gold_titles}
    hits = sum(1 for t in retrieved_titles[:k] if _normalize_title(t).lower() in gold_norm)
    return hits / len(gold_titles) if gold_titles else 0.0


def _f1_at(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _extract_titles(chunks: list, top_k: int) -> list[str]:
    titles = []
    for chunk in chunks[:top_k]:
        title = chunk.metadata.get("title") if chunk.metadata else None
        if title:
            titles.append(_normalize_title(title))
        else:
            lines = chunk.text.strip().split("\n")
            first = lines[0].strip().lstrip("# ").strip() if lines else ""
            titles.append(_normalize_title(first) if first else chunk.id)
    return titles


def evaluate_query(
    query_id: str,
    query: str,
    chunks: list,
    gold_titles: list[str],
    k_values: list[int],
) -> EvalResult:
    retrieved_titles = _extract_titles(chunks, max(k_values))
    result = EvalResult(
        query_id=query_id,
        query=query,
        retrieved_titles=retrieved_titles,
        gold_titles=gold_titles,
    )
    for k in k_values:
        p = _precision_at(retrieved_titles, gold_titles, k)
        r = _recall_at(retrieved_titles, gold_titles, k)
        f = _f1_at(p, r)
        result.precision_at[k] = p
        result.recall_at[k] = r
        result.f1_at[k] = f
        result.hit_at[k] = p > 0
        result.ndcg_at[k] = _ndcg(retrieved_titles, gold_titles, k)
    result.mrr = _mrr(retrieved_titles, gold_titles)
    return result


def aggregate_metrics(results: list[EvalResult], k_values: list[int]) -> dict:
    agg = {}
    for k in k_values:
        p_list = [r.precision_at[k] for r in results]
        r_list = [r.recall_at[k] for r in results]
        f_list = [r.f1_at[k] for r in results]
        h_list = [1 if r.hit_at[k] else 0 for r in results]
        ndcg_list = [r.ndcg_at[k] for r in results]
        agg[k] = {
            "Precision@K": np.mean(p_list),
            "Recall@K": np.mean(r_list),
            "F1@K": np.mean(f_list),
            "HitRate@K": np.mean(h_list),
            "nDCG@K": np.mean(ndcg_list),
        }
    agg["MRR"] = np.mean([r.mrr for r in results])
    return agg


# ─────────────────────────────────────────────
# 报告生成
# ─────────────────────────────────────────────

def format_table(rows: list[list], headers: list[str], col_widths: list[int]) -> str:
    sep = "|" + "|".join("-" * (w + 2) for w in col_widths) + "|"
    header_line = "|" + "|".join(f" {h} " for h in headers) + "|"
    lines = [header_line, sep]
    for row in rows:
        line = "|" + "|".join(f" {str(row[i]).ljust(col_widths[i])} " for i in range(len(headers))) + "|"
        lines.append(line)
    return "\n".join(lines)


def _n_cell(agg: dict, methods: list[str], k_or_mrr: int | str, metric_key: str) -> str:
    """Generate N-column cell for given methods."""
    vals = []
    for m in methods:
        if k_or_mrr == "MRR":
            v = agg[m]["MRR"]
        else:
            v = agg[m][k_or_mrr][metric_key]
        vals.append(f"{v:.4f}")
    return " / ".join(vals)


def build_markdown_report(
    results_by_method: dict[str, list[EvalResult]],
    agg_by_method: dict,
    k_values: list[int],
    elapsed_by_method: dict[str, float],
    rewrite_log: dict[str, tuple[str, str]] = None,
) -> str:
    methods = list(results_by_method.keys())
    n = len(methods)
    md = []
    rewrite_log = rewrite_log or {}

    method_labels = {
        "Vector Only": "纯向量检索",
        "Hybrid（无Rewrite无Rerank）": "Vector+BM25，无Rewrite，无Rerank",
        "Hybrid+Rewrite": "Vector+BM25+QueryRewrite",
        "Hybrid+Rerank": "Vector+BM25+LLM Rerank",
        "Hybrid+Rewrite+Rerank": "Vector+BM25+QueryRewrite+LLM Rerank",
    }
    label_row = " | ".join(methods)

    md.append("# 训练指导 RAG 检索评测报告\n")
    md.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    md.append(f"**评测范围**: fitness_guide collection · 仅训练指导 / 动作建议相关 query")
    md.append(f"**Query 数量**: {len(next(iter(results_by_method.values())))}")
    md.append(f"**K 值**: {k_values}")
    md.append(f"**四种方案**: `{label_row}`\n")

    # ── 1. 总体指标对比 ──
    md.append("## 1. 总体指标对比\n")

    col_w = 16 if n <= 3 else 14
    per_k_w = max(8, 24 // n)
    full_headers = ["Metric"] + [f"K={k}" for k in k_values] + ["MRR"]
    width_cols = [col_w] + [per_k_w] * len(k_values) + [per_k_w]

    table_rows = []
    for metric_key in ["Precision@K", "Recall@K", "F1@K", "HitRate@K", "nDCG@K"]:
        row = [f"**{metric_key}**"]
        for k in k_values:
            row.append(_n_cell(agg_by_method, methods, k, metric_key))
        row.append(_n_cell(agg_by_method, methods, "MRR", metric_key))
        table_rows.append(row)

    mrr_row = ["**MRR**"]
    for _ in k_values:
        mrr_row.append("")
    mrr_row.append(_n_cell(agg_by_method, methods, "MRR", "MRR"))
    table_rows.append(mrr_row)

    md.append(format_table(table_rows, full_headers, width_cols))
    md.append(f"\n> 格式为 `{' / '.join(methods)}`\n")

    # ── 2. 每条 Query 明细 ──
    md.append("## 2. 每条 Query 详细结果\n")
    q_headers = ["Query ID", "Query (truncated)", "Method"] + \
                [f"P@{k}" for k in k_values] + [f"R@{k}" for k in k_values] + \
                [f"nDCG@{k}" for k in k_values] + ["MRR"]
    q_widths = [8, 28, max(12, 8 + (n - 3) * 3)] + [7] * len(k_values) * 3 + [7]

    first_results = next(iter(results_by_method.values()))
    for i in range(len(first_results)):
        rows = []
        for method in methods:
            r = results_by_method[method][i]
            row = [
                r.query_id,
                r.query[:26] + ("…" if len(r.query) > 26 else ""),
                method,
            ] + [f"{r.precision_at[k]:.3f}" for k in k_values] + \
               [f"{r.recall_at[k]:.3f}" for k in k_values] + \
               [f"{r.ndcg_at[k]:.3f}" for k in k_values] + \
               [f"{r.mrr:.3f}"]
            rows.append(row)
        md.append(format_table(rows, q_headers, q_widths))
        md.append("")

    # ── 3. Query Rewrite 改写结果 ──
    md.append("## 3. Query Rewrite 改写结果\n")
    if rewrite_log:
        rw_headers = ["Query ID", "原始 Query", "改写后 Query"]
        rw_widths = [8, 35, 50]
        rw_rows = []
        for qid, (orig, rewritten) in rewrite_log.items():
            rw_rows.append([
                qid,
                orig[:33] + ("…" if len(orig) > 33 else orig),
                rewritten[:48] + ("…" if len(rewritten) > 48 else rewritten),
            ])
        md.append(format_table(rw_rows, rw_widths, rw_widths))
        md.append("")
    else:
        md.append("*未启用 Query Rewrite*\n")

    # ── 4. 检索耗时 ──
    md.append("## 4. 检索耗时\n")
    for method, elapsed in elapsed_by_method.items():
        md.append(f"- {method}: {elapsed:.2f}s")
    md.append("")

    # ── 5. 结论 ──
    md.append("## 5. 结论\n")
    best_mrr = max(methods, key=lambda m: agg_by_method[m]["MRR"])
    worst_mrr = min(methods, key=lambda m: agg_by_method[m]["MRR"])
    md.append(f"- **MRR 最高**: {best_mrr}（MRR = {agg_by_method[best_mrr]['MRR']:.4f}）")
    md.append(f"- **MRR 最低**: {worst_mrr}（MRR = {agg_by_method[worst_mrr]['MRR']:.4f}）")

    sep_bar = " / ".join(["——"] * n)
    for k in k_values:
        vals = {m: agg_by_method[m][k]["Recall@K"] for m in methods}
        best_k = max(vals, key=vals.get)
        val_str = " / ".join(f"{vals[m]:.4f}" for m in methods)
        md.append(f"- K={k} Recall: {val_str} → **{best_k}** 最高")

    for k in k_values:
        vals = {m: agg_by_method[m][k]["nDCG@K"] for m in methods}
        best_k = max(vals, key=vals.get)
        val_str = " / ".join(f"{vals[m]:.4f}" for m in methods)
        md.append(f"- K={k} nDCG:  {val_str} → **{best_k}** 最高")

    return "\n".join(md)


def build_csv(
    results_by_method: dict[str, list[EvalResult]],
    k_values: list[int],
) -> str:
    import io
    methods = list(results_by_method.keys())
    rows = []
    header = ["query_id", "query", "method"] + \
             [f"precision@{k}" for k in k_values] + \
             [f"recall@{k}" for k in k_values] + \
             [f"f1@{k}" for k in k_values] + \
             [f"hitrate@{k}" for k in k_values] + \
             [f"ndcg@{k}" for k in k_values] + ["mrr"]
    rows.append(header)

    first_results = next(iter(results_by_method.values()))
    for i in range(len(first_results)):
        for method in methods:
            r = results_by_method[method][i]
            row = [
                r.query_id,
                r.query,
                method,
            ] + [f"{r.precision_at[k]:.4f}" for k in k_values] + \
               [f"{r.recall_at[k]:.4f}" for k in k_values] + \
               [f"{r.f1_at[k]:.4f}" for k in k_values] + \
               [f"{'1' if r.hit_at[k] else '0'}" for k in k_values] + \
               [f"{r.ndcg_at[k]:.4f}" for k in k_values] + \
               [f"{r.mrr:.4f}"]
            rows.append(row)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows(rows)
    return output.getvalue()


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def load_gold(gold_path: str) -> list[dict]:
    with open(gold_path, encoding="utf-8") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="训练指导 RAG 检索评测（四种方案对比）")
    parser.add_argument("--gold", default="scripts/eval_workout_gold.json")
    parser.add_argument("--output", default="scripts/eval_results")
    parser.add_argument("--k", nargs="+", type=int, default=[3, 5])
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--fusion-k", type=int, default=5)
    parser.add_argument("--rerank-top-n", type=int, default=20,
                        help="Hybrid 召回候选数量，送入 Rerank（仅 Hybrid+Rerank）")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--methods",
        nargs="+",
        default=["vector", "hybrid", "hybrid_rewrite", "hybrid_rerank"],
        choices=["vector", "hybrid", "hybrid_rewrite", "hybrid_rerank", "hybrid_rewrite_rerank", "all"],
        help="指定运行哪些方法（默认四种；hybrid_rewrite_rerank 为可选，需显式指定）",
    )
    args = parser.parse_args()

    if "all" in args.methods:
        active_methods = ["vector", "hybrid", "hybrid_rewrite", "hybrid_rerank", "hybrid_rewrite_rerank"]
    else:
        active_methods = args.methods

    method_display_names = {
        "vector": "Vector Only",
        "hybrid": "Hybrid（无Rewrite无Rerank）",
        "hybrid_rewrite": "Hybrid+Rewrite",
        "hybrid_rerank": "Hybrid+Rerank",
        "hybrid_rewrite_rerank": "Hybrid+Rewrite+Rerank",
    }

    gold_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), args.gold)
    gold_data = load_gold(gold_path)
    print(f"[Eval] Loaded {len(gold_data)} queries from {gold_path}")
    print(f"[Eval] Active methods: {[method_display_names[m] for m in active_methods]}")

    qdrant_client = QdrantClient(host=Config.QDRANT_HOST, port=Config.QDRANT_PORT)
    from backend.services.llm import get_embedding_model
    embed_model = get_embedding_model()
    collection = "fitness_guide"

    retrievers = {}
    if "vector" in active_methods:
        retrievers["vector"] = VectorOnlyRetriever(collection, qdrant_client, embed_model)
    if "hybrid" in active_methods:
        retrievers["hybrid"] = HybridNoRewriteRetriever(
            collection, qdrant_client, embed_model,
            vector_top_k=args.top_k, bm25_top_k=args.top_k, fusion_top_k=args.fusion_k,
        )
    if "hybrid_rewrite" in active_methods:
        retrievers["hybrid_rewrite"] = HybridWithRewriteRetriever(
            collection, qdrant_client, embed_model,
            vector_top_k=args.top_k, bm25_top_k=args.top_k, fusion_top_k=args.fusion_k,
        )
    if "hybrid_rerank" in active_methods:
        retrievers["hybrid_rerank"] = HybridWithRerankRetriever(
            collection, qdrant_client, embed_model,
            vector_top_k=args.top_k, bm25_top_k=args.top_k,
            fusion_top_k=args.fusion_k, rerank_top_n=args.rerank_top_n,
        )
    if "hybrid_rewrite_rerank" in active_methods:
        retrievers["hybrid_rewrite_rerank"] = HybridWithRewriteAndRerankRetriever(
            collection, qdrant_client, embed_model,
            vector_top_k=args.top_k, bm25_top_k=args.top_k,
            fusion_top_k=args.fusion_k, rerank_top_n=args.rerank_top_n,
        )

    # 结果 & 计时
    results_by_method: dict[str, list[EvalResult]] = {m: [] for m in active_methods}
    elapsed_by_method: dict[str, float] = {}
    rewrite_log: dict[str, tuple[str, str]] = {}

    for item in gold_data:
        gold_titles = [ri["title"] for ri in item["relevant_items"]]
        qid = item["id"]
        query = item["query"]

        # Vector Only
        if "vector" in active_methods:
            t0 = time.time()
            chunks = retrievers["vector"].retrieve(query, top_k=args.fusion_k)
            elapsed_by_method["vector"] = elapsed_by_method.get("vector", 0) + (time.time() - t0)
            result = evaluate_query(qid, query, chunks, gold_titles, args.k)
            results_by_method["vector"].append(result)
            if args.debug:
                print(f"\n[DEBUG] [{qid}] Vector Only | gold={gold_titles} | ret={result.retrieved_titles}")

        # Hybrid
        if "hybrid" in active_methods:
            t0 = time.time()
            chunks = retrievers["hybrid"].retrieve(query)
            elapsed_by_method["hybrid"] = elapsed_by_method.get("hybrid", 0) + (time.time() - t0)
            result = evaluate_query(qid, query, chunks, gold_titles, args.k)
            results_by_method["hybrid"].append(result)
            if args.debug:
                print(f"\n[DEBUG] [{qid}] Hybrid | gold={gold_titles} | ret={result.retrieved_titles}")

        # Hybrid + Rewrite
        if "hybrid_rewrite" in active_methods:
            t0 = time.time()
            chunks, rewritten = retrievers["hybrid_rewrite"].retrieve(query)
            elapsed_by_method["hybrid_rewrite"] = elapsed_by_method.get("hybrid_rewrite", 0) + (time.time() - t0)
            result = evaluate_query(qid, query, chunks, gold_titles, args.k)
            results_by_method["hybrid_rewrite"].append(result)
            rewrite_log[qid] = (query, rewritten)
            if args.debug:
                print(f"\n[DEBUG] [{qid}] Hybrid+Rewrite | orig={query} | rewritten={rewritten}")
                print(f"  | gold={gold_titles} | ret={result.retrieved_titles}")

        # Hybrid + Rewrite + Rerank
        if "hybrid_rewrite_rerank" in active_methods:
            t0 = time.time()
            chunks, rewritten = retrievers["hybrid_rewrite_rerank"].retrieve(query)
            elapsed_by_method["hybrid_rewrite_rerank"] = elapsed_by_method.get("hybrid_rewrite_rerank", 0) + (time.time() - t0)
            result = evaluate_query(qid, query, chunks, gold_titles, args.k)
            results_by_method["hybrid_rewrite_rerank"].append(result)
            rewrite_log[qid + "_rewrite_rerank"] = (query, rewritten)
            if args.debug:
                print(f"\n[DEBUG] [{qid}] Hybrid+Rewrite+Rerank | orig={query} | rewritten={rewritten}")
                print(f"  | gold={gold_titles} | ret={result.retrieved_titles}")

        # Hybrid + Rerank
        if "hybrid_rerank" in active_methods:
            t0 = time.time()
            chunks = retrievers["hybrid_rerank"].retrieve(query)
            elapsed_by_method["hybrid_rerank"] = elapsed_by_method.get("hybrid_rerank", 0) + (time.time() - t0)
            result = evaluate_query(qid, query, chunks, gold_titles, args.k)
            results_by_method["hybrid_rerank"].append(result)
            if args.debug:
                print(f"\n[DEBUG] [{qid}] Hybrid+Rerank | gold={gold_titles} | ret={result.retrieved_titles}")

    # 聚合
    agg_by_method = {m: aggregate_metrics(results_by_method[m], args.k) for m in active_methods}

    # 打印摘要（动态 N 列）
    n = len(active_methods)
    print("\n" + "=" * (30 + 26 * n))
    print("评测结果摘要")
    print("=" * (30 + 26 * n))

    header_row = "Metric".ljust(14) + "".join(
        f"  K={k} " + " / ".join(method_display_names[m] for m in active_methods) for k in args.k
    ) + "  MRR  " + " / ".join(method_display_names[m] for m in active_methods)
    print(header_row)
    print("-" * (30 + 26 * n))

    for metric_key in ["Precision@K", "Recall@K", "F1@K", "HitRate@K", "nDCG@K"]:
        row = metric_key.ljust(14)
        for k in args.k:
            cells = [f"{agg_by_method[m][k][metric_key]:.4f}" for m in active_methods]
            row += "  " + " / ".join(cells).ljust(26)
        mrr_cells = [f"{agg_by_method[m]['MRR']:.4f}" for m in active_methods]
        row += "  " + " / ".join(mrr_cells)
        print(row)

    print("-" * (30 + 26 * n))
    print("耗时:")
    for method, elapsed in elapsed_by_method.items():
        print(f"  {method_display_names[method]}: {elapsed:.2f}s")

    # 写报告
    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)

    results_display = {method_display_names[m]: results_by_method[m] for m in active_methods}
    agg_display = {method_display_names[m]: agg_by_method[m] for m in active_methods}
    elapsed_display = {method_display_names[m]: elapsed_by_method[m] for m in active_methods}

    md_content = build_markdown_report(
        results_display, agg_display, args.k,
        elapsed_display, rewrite_log,
    )
    md_path = args.output + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"\n[Eval] Markdown report → {md_path}")

    csv_content = build_csv(results_display, args.k)
    csv_path = args.output + ".csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        f.write(csv_content)
    print(f"[Eval] CSV report      → {csv_path}")


if __name__ == "__main__":
    main()