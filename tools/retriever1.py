"""
混合检索模块：结合向量检索 + BM25，使用 RRF 融合
支持 Query 改写
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Optional
from dataclasses import dataclass
import json
import numpy as np

from qdrant_client import QdrantClient
from qdrant_client.models import ScrollResult, ScoredPoint

from rank_bm25 import BM25Okapi
import jieba

from config import AgentConfig


# Query 改写提示词
REWRITE_QUERY_PROMPT = """将用户问题改写为更适合健身知识库检索的形式。

要求：
1. 补充隐含意图（如"我想瘦点"→"减脂方法"）
2. 扩展同义词（如"练胸"→"胸部训练 胸肌"）
3. 分解复杂问题
4. 使用知识库常用的专业术语

原始问题：{query}

改写后的检索 query（只返回改写后的 query，不要其他内容）："""


@dataclass
class RetrievedChunk:
    """检索结果"""
    id: str
    text: str
    score: float
    metadata: dict


def extract_text_from_payload(payload: dict) -> Optional[str]:
    """
    从 LlamaIndex 存储的 payload 中提取文本内容

    LlamaIndex 存储结构：
    - _node_content: JSON 字符串，包含完整的节点信息
    - text: 直接存储的文本（部分情况）
    """
    if not payload:
        return None

    # 优先尝试从 _node_content 解析
    node_content = payload.get("_node_content")
    if node_content:
        try:
            node_data = json.loads(node_content)
            # LlamaIndex 的 text 字段
            if "text" in node_data:
                return node_data["text"]
            # 备选：entire_document 或其他文本字段
            if "header" in node_data:
                return node_data["header"]
        except (json.JSONDecodeError, TypeError):
            pass

    # 备选：直接取 text 字段
    if "text" in payload:
        return payload["text"]

    # 备选：从 title 和其他字段组合
    title = payload.get("title", "")
    text = payload.get("text", "")
    if title and text:
        return f"{title}\n{text}"

    return None


class BM25Retriever:
    """BM25 检索器"""

    def __init__(self, collection_name: str, qdrant_client: QdrantClient):
        self.collection_name = collection_name
        self.client = qdrant_client
        self.documents: list[str] = []
        self.ids: list[str] = []
        self.metadatas: list[dict] = []
        self.bm25: Optional[BM25Okapi] = None
        self._initialized = False

    def _initialize(self):
        """从 Qdrant 加载文档并构建 BM25 索引"""
        if self._initialized:
            return

        # 使用 scroll API 获取所有文档
        results, next_page_offset = self.client.scroll(
            collection_name=self.collection_name,
            limit=1000,
            with_payload=True,
            with_vectors=False
        )

        for point in results:
            # 提取文本（从 payload 中获取）
            text = extract_text_from_payload(point.payload)
            if text:
                self.documents.append(text)
                self.ids.append(str(point.id))
                # 保存有用的 metadata（排除内部字段）
                metadata = {
                    k: v for k, v in point.payload.items()
                    if k not in ("_node_content", "_node_type", "document_id", "doc_id", "ref_doc_id")
                }
                self.metadatas.append(metadata)

        # 构建 BM25 索引
        if self.documents:
            # 使用 jieba 分词（支持中文）
            tokenized_corpus = [list(jieba.cut(doc.lower())) for doc in self.documents]
            self.bm25 = BM25Okapi(tokenized_corpus)

        self._initialized = True
        print(f"[BM25Retriever] Loaded {len(self.documents)} documents for collection {self.collection_name}")

    def retrieve(self, query: str, top_k: int = 10) -> list[RetrievedChunk]:
        """BM25 检索"""
        self._initialize()

        if not self.documents or not self.bm25:
            return []

        # 使用 jieba 分词
        query_tokens = list(jieba.cut(query.lower()))

        # 获取 BM25 分数
        scores = self.bm25.get_scores(query_tokens)

        # 获取 top_k
        top_indices = np.argsort(scores)[::-1][:top_k]

        results = []
        for idx in top_indices:
            if scores[idx] > 0:  # 只返回正分数
                results.append(RetrievedChunk(
                    id=self.ids[idx],
                    text=self.documents[idx],
                    score=float(scores[idx]),
                    metadata=self.metadatas[idx]
                ))

        return results


class VectorRetriever:
    """向量检索器（封装 Qdrant）"""

    def __init__(self, collection_name: str, qdrant_client: QdrantClient, embed_model):
        self.collection_name = collection_name
        self.client = qdrant_client
        self.embed_model = embed_model
        self._initialized = False
        self.documents: list[str] = []
        self.ids: list[str] = []
        self.metadatas: list[dict] = []

    def _initialize(self):
        """从 Qdrant 加载文档信息"""
        if self._initialized:
            return

        results, _ = self.client.scroll(
            collection_name=self.collection_name,
            limit=1000,
            with_payload=True,
            with_vectors=False
        )

        for point in results:
            text = extract_text_from_payload(point.payload)
            if text:
                self.documents.append(text)
                self.ids.append(str(point.id))
                metadata = {
                    k: v for k, v in point.payload.items()
                    if k not in ("_node_content", "_node_type", "document_id", "doc_id", "ref_doc_id")
                }
                self.metadatas.append(metadata)

        self._initialized = True

    def retrieve(self, query: str, top_k: int = 10) -> list[RetrievedChunk]:
        """向量相似度检索"""
        self._initialize()

        if not self.documents:
            return []

        # 生成查询向量
        query_embedding = self.embed_model.get_text_embedding(query)

        # Qdrant 搜索（使用 query_points）
        results = self.client.query_points(
            collection_name=self.collection_name,
            query=query_embedding,
            limit=top_k,
            with_payload=True
        ).points

        retrieved = []
        id_to_idx = {id_val: idx for idx, id_val in enumerate(self.ids)}

        for result in results:
            text = extract_text_from_payload(result.payload)
            if text:
                original_idx = id_to_idx.get(str(result.id))
                if original_idx is not None:
                    metadata = {
                        k: v for k, v in result.payload.items()
                        if k not in ("_node_content", "_node_type", "document_id", "doc_id", "ref_doc_id")
                    }
                    retrieved.append(RetrievedChunk(
                        id=str(result.id),
                        text=text,
                        score=result.score,
                        metadata=metadata
                    ))

        return retrieved


class QueryRewriter:
    """Query 改写器 - 将用户自然语言改写为更适合知识库检索的形式"""

    def __init__(self):
        self._llm = None

    @property
    def llm(self):
        if self._llm is None:
            from agent.llm import get_llm
            self._llm = get_llm()
        return self._llm

    def rewrite(self, query: str) -> str:
        """
        改写 query

        Args:
            query: 原始用户 query

        Returns:
            改写后的 query
        """
        try:
            prompt = REWRITE_QUERY_PROMPT.format(query=query)
            response = self.llm.invoke([{"role": "user", "content": prompt}])
            # 提取返回的 query（去除空白字符）
            rewritten = response.content.strip()
            print(f"[QueryRewriter] '{query}' → '{rewritten}'")
            return rewritten
        except Exception as e:
            print(f"[QueryRewriter] 改写失败，使用原 query: {e}")
            return query


def reciprocal_rank_fusion(results_list: list[list[RetrievedChunk]], k: int = 60) -> list[RetrievedChunk]:
    """
    RRF (Reciprocal Rank Fusion) 融合多个检索结果

    Args:
        results_list: 多个检索器的结果列表
        k: RRF 参数，默认 60
    """
    rrf_scores: dict[str, tuple[float, RetrievedChunk]] = {}

    for results in results_list:
        for rank, result in enumerate(results):
            rrf_score = 1 / (k + rank + 1)
            if result.id in rrf_scores:
                rrf_scores[result.id] = (rrf_scores[result.id][0] + rrf_score, result)
            else:
                rrf_scores[result.id] = (rrf_score, result)

    # 按 RRF 分数排序
    fused = sorted(rrf_scores.values(), key=lambda x: x[0], reverse=True)
    return [item[1] for item in fused]


class HybridRetriever:
    """混合检索器：向量 + BM25 + RRF 融合 + 可选 Query 改写"""

    def __init__(
        self,
        collection_name: str,
        qdrant_client: QdrantClient,
        embed_model,
        vector_top_k: int = None,
        bm25_top_k: int = None,
        fusion_top_k: int = None,
        use_query_rewrite: bool = None
    ):
        self.collection_name = collection_name
        # 从 config 读取默认值
        self.vector_top_k = vector_top_k if vector_top_k is not None else AgentConfig.VECTOR_TOP_K
        self.bm25_top_k = bm25_top_k if bm25_top_k is not None else AgentConfig.BM25_TOP_K
        self.fusion_top_k = fusion_top_k if fusion_top_k is not None else AgentConfig.FUSION_TOP_K
        self.use_query_rewrite = use_query_rewrite if use_query_rewrite is not None else AgentConfig.USE_QUERY_REWRITE

        self.vector_retriever = VectorRetriever(collection_name, qdrant_client, embed_model)
        self.bm25_retriever = BM25Retriever(collection_name, qdrant_client)
        self.query_rewriter = QueryRewriter() if self.use_query_rewrite else None

    def retrieve(self, query: str) -> list[RetrievedChunk]:
        """
        执行混合检索

        Returns:
            按 RRF 分数排序的检索结果
        """
        # Query 改写
        if self.use_query_rewrite and self.query_rewriter:
            query = self.query_rewriter.rewrite(query)

        # 并行执行向量检索和 BM25 检索
        vector_results = self.vector_retriever.retrieve(query, self.vector_top_k)
        bm25_results = self.bm25_retriever.retrieve(query, self.bm25_top_k)

        # RRF 融合
        fused_results = reciprocal_rank_fusion([vector_results, bm25_results])

        return fused_results[:self.fusion_top_k]

    def retrieve_with_scores(self, query: str) -> dict:
        """
        执行混合检索，返回详细分数信息（用于调试）
        """
        # Query 改写
        original_query = query
        if self.use_query_rewrite and self.query_rewriter:
            query = self.query_rewriter.rewrite(query)

        vector_results = self.vector_retriever.retrieve(query, self.vector_top_k)
        bm25_results = self.bm25_retriever.retrieve(query, self.bm25_top_k)
        fused_results = reciprocal_rank_fusion([vector_results, bm25_results])

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
                {"id": r.id, "text": r.text[:100] + "...", "metadata": r.metadata}
                for r in fused_results[:self.fusion_top_k]
            ]
        }


def get_hybrid_retriever(collection_name: str) -> HybridRetriever:
    """创建混合检索器"""
    from qdrant_client import QdrantClient
    from agent.llm import get_embedding_model
    qdrant_client = QdrantClient(host=AgentConfig.QDRANT_HOST, port=AgentConfig.QDRANT_PORT)
    embed_model = get_embedding_model()
    return HybridRetriever(collection_name, qdrant_client, embed_model)
