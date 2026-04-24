"""
混合检索模块：向量检索 + BM25 + RRF 融合
支持可选 Query 改写
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

import jieba
import numpy as np
from qdrant_client import QdrantClient
try:
    from rank_bm25 import BM25Okapi
except ImportError:
    BM25Okapi = None

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.services.llm import get_embedding_model, get_llm
from config import Config

# Qdrant 配置
QDRANT_HOST = Config.QDRANT_HOST or "127.0.0.1"
QDRANT_PORT = int(Config.QDRANT_PORT) if Config.QDRANT_PORT else 6333
DEFAULT_COLLECTION = "fitness_guide"

# 检索参数（可通过环境变量覆盖）
VECTOR_TOP_K = int(os.getenv("VECTOR_TOP_K", "8"))
BM25_TOP_K = int(os.getenv("BM25_TOP_K", "8"))
FUSION_TOP_K = int(os.getenv("FUSION_TOP_K", "3"))
USE_QUERY_REWRITE = os.getenv("USE_QUERY_REWRITE", "false").lower() == "true"
RRF_K = int(os.getenv("RRF_K", "60"))

REWRITE_QUERY_PROMPT = """将用户问题改写为更适合健身知识库检索的形式。

要求：
1. 补充隐含意图（如"我想瘦点"→"减脂方法"）
2. 扩展同义词（如"练胸"→"胸部训练 胸肌"）
3. 分解复杂问题
4. 使用知识库常用专业术语

原始问题：{query}

改写后的检索 query（只返回改写后的 query，不要其他内容）："""


@dataclass
class RetrievedChunk:
    id: str
    text: str
    score: float
    metadata: dict[str, Any]


def _extract_text_from_payload(payload: dict[str, Any] | None) -> Optional[str]:
    if not payload:
        return None

    node_content = payload.get("_node_content")
    if node_content:
        try:
            node_data = json.loads(node_content)
            if isinstance(node_data, dict):
                if node_data.get("text"):
                    return str(node_data["text"])
                if node_data.get("header"):
                    return str(node_data["header"])
        except (json.JSONDecodeError, TypeError):
            pass

    if payload.get("text"):
        return str(payload["text"])

    title = payload.get("title", "")
    text = payload.get("text", "")
    if title and text:
        return f"{title}\n{text}"

    return None


def _clean_metadata(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    return {
        k: v
        for k, v in payload.items()
        if k not in ("_node_content", "_node_type", "document_id", "doc_id", "ref_doc_id")
    }


class QueryRewriter:
    def __init__(self) -> None:
        self._llm = None

    @property
    def llm(self):
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    def rewrite(self, query: str) -> str:
        try:
            prompt = REWRITE_QUERY_PROMPT.format(query=query)
            response = self.llm.invoke([{"role": "user", "content": prompt}])
            rewritten = response.content.strip()
            return rewritten or query
        except Exception:
            return query


class BM25Retriever:
    def __init__(self, collection_name: str, qdrant_client: QdrantClient):
        self.collection_name = collection_name
        self.client = qdrant_client
        self.documents: list[str] = []
        self.ids: list[str] = []
        self.metadatas: list[dict[str, Any]] = []
        self.bm25: Optional[BM25Okapi] = None
        self._initialized = False

    def _initialize(self) -> None:
        if self._initialized:
            return
        if BM25Okapi is None:
            self._initialized = True
            return

        offset = None
        while True:
            results, offset = self.client.scroll(
                collection_name=self.collection_name,
                offset=offset,
                limit=100,
                with_payload=True,
                with_vectors=False,
            )
            for point in results:
                text = _extract_text_from_payload(point.payload)
                if text:
                    self.documents.append(text)
                    self.ids.append(str(point.id))
                    self.metadatas.append(_clean_metadata(point.payload))
            if offset is None:
                break

        if self.documents:
            tokenized_corpus = [list(jieba.cut(doc.lower())) for doc in self.documents]
            self.bm25 = BM25Okapi(tokenized_corpus)

        self._initialized = True

    def retrieve(self, query: str, top_k: int = 10) -> list[RetrievedChunk]:
        self._initialize()
        if BM25Okapi is None:
            return []
        if not self.documents or not self.bm25:
            return []

        query_tokens = list(jieba.cut(query.lower()))
        scores = self.bm25.get_scores(query_tokens)
        top_indices = np.argsort(scores)[::-1][:top_k]

        results: list[RetrievedChunk] = []
        for idx in top_indices:
            if scores[idx] <= 0:
                continue
            results.append(
                RetrievedChunk(
                    id=self.ids[idx],
                    text=self.documents[idx],
                    score=float(scores[idx]),
                    metadata=self.metadatas[idx],
                )
            )
        return results


class VectorRetriever:
    def __init__(self, collection_name: str, qdrant_client: QdrantClient, embed_model: Any):
        self.collection_name = collection_name
        self.client = qdrant_client
        self.embed_model = embed_model

    def retrieve(self, query: str, top_k: int = 10) -> list[RetrievedChunk]:
        query_embedding = self.embed_model.get_text_embedding(query)

        # 优先使用新接口 query_points，兼容旧接口 search
        if hasattr(self.client, "query_points"):
            points = self.client.query_points(
                collection_name=self.collection_name,
                query=query_embedding,
                limit=top_k,
                with_payload=True,
            ).points
        else:
            points = self.client.search(
                collection_name=self.collection_name,
                query_vector=query_embedding,
                limit=top_k,
                with_payload=True,
            )

        retrieved: list[RetrievedChunk] = []
        for p in points:
            payload = getattr(p, "payload", None)
            text = _extract_text_from_payload(payload)
            if not text:
                continue
            retrieved.append(
                RetrievedChunk(
                    id=str(getattr(p, "id", "")),
                    text=text,
                    score=float(getattr(p, "score", 0.0) or 0.0),
                    metadata=_clean_metadata(payload),
                )
            )
        return retrieved


def reciprocal_rank_fusion(results_list: list[list[RetrievedChunk]], k: int = RRF_K) -> list[RetrievedChunk]:
    rrf_scores: dict[str, tuple[float, RetrievedChunk]] = {}
    for results in results_list:
        for rank, result in enumerate(results):
            score = 1.0 / (k + rank + 1)
            if result.id in rrf_scores:
                rrf_scores[result.id] = (rrf_scores[result.id][0] + score, result)
            else:
                rrf_scores[result.id] = (score, result)
    fused = sorted(rrf_scores.values(), key=lambda x: x[0], reverse=True)
    return [x[1] for x in fused]


class HybridRetriever:
    def __init__(
        self,
        collection_name: str,
        qdrant_client: QdrantClient,
        embed_model: Any,
        vector_top_k: int | None = None,
        bm25_top_k: int | None = None,
        fusion_top_k: int | None = None,
        use_query_rewrite: bool | None = None,
    ):
        self.collection_name = collection_name
        self.vector_top_k = vector_top_k if vector_top_k is not None else VECTOR_TOP_K
        self.bm25_top_k = bm25_top_k if bm25_top_k is not None else BM25_TOP_K
        self.fusion_top_k = fusion_top_k if fusion_top_k is not None else FUSION_TOP_K
        self.use_query_rewrite = use_query_rewrite if use_query_rewrite is not None else USE_QUERY_REWRITE

        self.vector_retriever = VectorRetriever(collection_name, qdrant_client, embed_model)
        self.bm25_retriever = BM25Retriever(collection_name, qdrant_client)
        self.query_rewriter = QueryRewriter() if self.use_query_rewrite else None

    def retrieve(self, query: str) -> list[RetrievedChunk]:
        if self.use_query_rewrite and self.query_rewriter:
            query = self.query_rewriter.rewrite(query)

        vector_results = self.vector_retriever.retrieve(query, self.vector_top_k)
        bm25_results = self.bm25_retriever.retrieve(query, self.bm25_top_k)
        if bm25_results:
            fused = reciprocal_rank_fusion([vector_results, bm25_results], k=RRF_K)
        else:
            fused = vector_results
        return fused[: self.fusion_top_k]

    def retrieve_with_scores(self, query: str) -> dict[str, Any]:
        original_query = query
        if self.use_query_rewrite and self.query_rewriter:
            query = self.query_rewriter.rewrite(query)

        vector_results = self.vector_retriever.retrieve(query, self.vector_top_k)
        bm25_results = self.bm25_retriever.retrieve(query, self.bm25_top_k)
        fused = reciprocal_rank_fusion([vector_results, bm25_results], k=RRF_K)

        return {
            "original_query": original_query,
            "rewritten_query": query,
            "vector_results": [
                {"id": r.id, "text": r.text[:100] + "...", "score": r.score, "metadata": r.metadata}
                for r in vector_results
            ],
            "bm25_results": [
                {"id": r.id, "text": r.text[:100] + "...", "score": r.score, "metadata": r.metadata}
                for r in bm25_results
            ],
            "fused_results": [
                {"id": r.id, "text": r.text[:100] + "...", "score": r.score, "metadata": r.metadata}
                for r in fused[: self.fusion_top_k]
            ],
        }


class FitnessGuideRetrieverImpl:
    """
    对外统一检索器，兼容 workout_agent 接口:
    retrieve(query, top_k=3) -> list[{"id","text","score","metadata"}]
    """

    def __init__(self, retrieval_mode: Literal["vector", "bm25", "hybrid"] = "hybrid"):
        self.retrieval_mode = retrieval_mode
        self.client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        self.embed_model = get_embedding_model()
        self.collection_name = DEFAULT_COLLECTION
        self._hybrid = HybridRetriever(self.collection_name, self.client, self.embed_model)
        self._vector = VectorRetriever(self.collection_name, self.client, self.embed_model)
        self._bm25 = BM25Retriever(self.collection_name, self.client)

    def retrieve(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        if self.retrieval_mode == "vector":
            chunks = self._vector.retrieve(query, top_k=top_k)
        elif self.retrieval_mode == "bm25":
            chunks = self._bm25.retrieve(query, top_k=top_k)
        else:
            self._hybrid.fusion_top_k = top_k
            chunks = self._hybrid.retrieve(query)

        return [
            {
                "id": c.id,
                "text": c.text,
                "score": c.score,
                "metadata": c.metadata,
            }
            for c in chunks
        ]

    def retrieve_with_scores(self, query: str) -> dict[str, Any]:
        if self.retrieval_mode != "hybrid":
            return {"message": "retrieve_with_scores is only available in hybrid mode"}
        return self._hybrid.retrieve_with_scores(query)


_retriever_instance: FitnessGuideRetrieverImpl | None = None


def get_fitness_guide_retriever(
    retrieval_mode: Literal["vector", "bm25", "hybrid"] = "hybrid",
    retrieval_model: Literal["vector", "bm25", "hybrid"] | None = None,
) -> FitnessGuideRetrieverImpl:
    # 兼容旧调用参数名 retrieval_model
    if retrieval_model is not None:
        retrieval_mode = retrieval_model
    global _retriever_instance
    if _retriever_instance is None:
        _retriever_instance = FitnessGuideRetrieverImpl(retrieval_mode=retrieval_mode)
    return _retriever_instance


def init_retriever(
    retrieval_mode: Literal["vector", "bm25", "hybrid"] = "hybrid",
) -> bool:
    global _retriever_instance
    try:
        _retriever_instance = FitnessGuideRetrieverImpl(retrieval_mode=retrieval_mode)
        # 轻量连通性检查
        _retriever_instance.client.get_collections()
        return True
    except Exception:
        return False


def get_hybrid_retriever(collection_name: str) -> HybridRetriever:
    """
    兼容旧代码（例如 lrw.py）的工厂函数
    """
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    embed_model = get_embedding_model()
    return HybridRetriever(collection_name, client, embed_model)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fitness Guide Retriever")
    parser.add_argument("--mode", choices=["vector", "bm25", "hybrid"], default="hybrid")
    parser.add_argument("--query", default="深蹲怎么做")
    parser.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()

    ok = init_retriever(args.mode)
    if not ok:
        print("Failed to initialize retriever. Check Qdrant and embedding config.")
        raise SystemExit(1)

    retriever = get_fitness_guide_retriever(args.mode)
    results = retriever.retrieve(args.query, top_k=args.top_k)
    print(f"Found {len(results)} results")
    for i, r in enumerate(results, 1):
        print(f"\n--- Result {i} (score: {r.get('score')}) ---")
        print((r.get("text") or "")[:400])
