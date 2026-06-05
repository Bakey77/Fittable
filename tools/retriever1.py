"""
混合检索模块：结合向量检索 + BM25，使用 RRF 融合
支持 Query 改写
"""
import os
import sys
import logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Optional
from dataclasses import dataclass
import json
import numpy as np

from qdrant_client import QdrantClient
from qdrant_client.models import ScrollResult, ScoredPoint

try:
    from rank_bm25 import BM25Okapi
except ImportError:
    BM25Okapi = None
import jieba

from config import Config


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

        if BM25Okapi is None:
            self._initialized = True
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
            from backend.services.llm import get_longcat_llm
            self._llm = get_longcat_llm()
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


# LLM Rerank 提示词
RERANK_PROMPT = """你是一个专业的健身知识检索相关性评估器。
给定一个用户问题和一个知识库片段，你需要评估这个片段对回答用户问题的相关程度。

评分标准：
- 1.0：该片段直接、完整地回答了用户问题，包含关键细节
- 0.7：该片段与用户问题高度相关，包含部分答案
- 0.4：该片段与用户问题有一定关联，但不够精确
- 0.1：该片段与用户问题关联较弱
- 0.0：该片段与用户问题完全不相关

用户问题：{query}

知识库片段：
---
{chunk_text}
---

请只输出一个 0 到 1 之间的小数分数，不要输出其他内容。
分数："""


class LLMReranker:
    """
    LLM 重排序器

    流程：Hybrid 召回 top_n 候选 → LLM 逐条评估相关性 → 按 LLM 分数重排 → 返回 top_k
    """

    def __init__(self, rerank_top_n: int = 20):
        """
        Args:
            rerank_top_n: 最多送入 LLM 重排序的候选数量（控制 LLM 调用次数和耗时）
        """
        self.rerank_top_n = rerank_top_n
        self._llm = None

    @property
    def llm(self):
        if self._llm is None:
            from backend.services.llm import get_longcat_llm
            self._llm = get_longcat_llm()
        return self._llm

    def rerank(
        self,
        query: str,
        candidates: list[RetrievedChunk],
        top_k: int = 5
    ) -> list[RetrievedChunk]:
        """
        对候选 chunks 进行 LLM 重排序

        Args:
            query: 用户查询
            candidates: Hybrid 检索召回的候选 chunks（按 RRF 分数排序）
            top_k: 最终返回的数量

        Returns:
            按 LLM 相关性分数重排序后的 chunks
        """
        if not candidates:
            return []

        # 截断到 rerank_top_n 个候选
        candidates = candidates[:self.rerank_top_n]

        scored = []
        for chunk in candidates:
            try:
                prompt = RERANK_PROMPT.format(
                    query=query,
                    chunk_text=chunk.text[:1500]  # 截断避免 token 过长
                )
                response = self.llm.invoke([{"role": "user", "content": prompt}])
                raw_score = response.content.strip() if hasattr(response, "content") else str(response)

                # 解析分数：提取第一个合法浮点数
                import re
                match = re.search(r"0?\.\d+|1\.0+|0", raw_score)
                llm_score = float(match.group(0)) if match else 0.0
                llm_score = max(0.0, min(1.0, llm_score))  # clamp 到 [0, 1]

            except Exception as e:
                print(f"[LLMReranker] Chunk scoring failed: {e}, score=0.0")
                llm_score = 0.0

            scored.append((llm_score, chunk))

        # 按 LLM 分数降序排列
        scored.sort(key=lambda x: x[0], reverse=True)

        reranked = [chunk for _, chunk in scored]
        print(f"[LLMReranker] query='{query}' | scored {len(candidates)} candidates, top score={scored[0][0] if scored else 0}")

        return reranked[:top_k]


class DashScopeReranker:
    """
    DashScope 专用 Rerank 模型重排序器

    使用 qwen3-v1-rerank 等专用模型，一次 API 调用完成所有候选排序，
    替代 LLMReranker 的逐条 LLM 打分方式，延迟和成本大幅降低。

    API 文档：https://help.aliyun.com/zh/model-studio/text-rerank
    """

    def __init__(self, rerank_top_n: int = 20):
        self.rerank_top_n = rerank_top_n
        self.api_key = Config.LLM_API_KEY
        self.base_url = os.getenv(
            "RERANK_BASE_URL",
            "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
        )
        self.model = Config.RERANK_MODEL or "gte-rerank"

    def rerank(
        self,
        query: str,
        candidates: list[RetrievedChunk],
        top_k: int = 5,
    ) -> list[RetrievedChunk]:
        if not candidates:
            return []

        candidates = candidates[: self.rerank_top_n]
        documents = [c.text[:1500] for c in candidates]

        try:
            import requests
            resp = requests.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "input": {
                        "query": query,
                        "documents": documents,
                    },
                    "parameters": {
                        "top_n": min(top_k, len(documents)),
                    },
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            results = data["output"]["results"]
            # results 按 index 和 relevance_score 返回，重排后取 top_k
            indexed_scores = {r["index"]: r["relevance_score"] for r in results}
            scored = [
                (indexed_scores.get(i, 0.0), candidates[i])
                for i in range(len(candidates))
            ]
            scored.sort(key=lambda x: x[0], reverse=True)

            print(
                f"[DashScopeReranker] query='{query}' | "
                f"model={self.model} | "
                f"scored {len(candidates)} candidates, "
                f"top score={scored[0][0] if scored else 0:.4f}"
            )
        except Exception as e:
            print(f"[DashScopeReranker] Rerank failed: {e}, fallback to RRF order")
            try:
                print(f"[DashScopeReranker] Response body: {resp.text}")
            except Exception:
                pass
            scored = [(0.0, c) for c in candidates]

        reranked = [chunk for _, chunk in scored]
        return reranked[:top_k]


# =============================================================================
# Historical Events Retriever
# =============================================================================

HISTORICAL_EVENTS_COLLECTION = "historical_events"
HISTORICAL_EVENTS_TOP_K = 3


def _get_historical_qdrant_client() -> QdrantClient:
    """获取 Qdrant 客户端（historical_events 专用）。"""
    return QdrantClient(host=Config.QDRANT_HOST, port=int(Config.QDRANT_PORT) if Config.QDRANT_PORT else 6333)


def _get_historical_embed_model():
    """获取 embedding 模型（懒加载）。"""
    from backend.services.llm import get_embedding_model
    return get_embedding_model()


def ensure_historical_events_collection() -> None:
    """确保 historical_events collection 存在于 Qdrant（幂等）。"""
    client = _get_historical_qdrant_client()
    collections = [c.name for c in client.get_collections().collections]
    if HISTORICAL_EVENTS_COLLECTION not in collections:
        from qdrant_client.models import VectorParams, Distance
        client.create_collection(
            collection_name=HISTORICAL_EVENTS_COLLECTION,
            vectors_config=VectorParams(size=1536, distance=Distance.COSINE),
        )
        print(f"[HistoricalEvents] Created Qdrant collection '{HISTORICAL_EVENTS_COLLECTION}'")
    else:
        print(f"[HistoricalEvents] Qdrant collection '{HISTORICAL_EVENTS_COLLECTION}' already exists")


def ensure_historical_event_in_qdrant(
    event_id: int,
    session_id: str,
    content: str,
    source_turns: list[int] | None = None,
    created_at: str = "",
) -> None:
    """
    将一条历史事件写入 Qdrant（幂等 upsert）。

    Args:
        event_id: SQLite historical_events.id（用作 Qdrant point id）
        session_id: 会话标识
        content: 事件文本
        source_turns: 来源轮次
        created_at: 创建时间
    """
    client = _get_historical_qdrant_client()
    embed_model = _get_historical_embed_model()

    vector = embed_model.get_text_embedding(content)
    payload = {
        "session_id": session_id,
        "content": content,
        "source_turns": json.dumps(source_turns or [], ensure_ascii=False),
        "created_at": created_at,
    }

    from qdrant_client.models import PointStruct
    client.upsert(
        collection_name=HISTORICAL_EVENTS_COLLECTION,
        points=[
            PointStruct(
                id=event_id,
                vector=list(vector),
                payload=payload,
            )
        ],
    )


def delete_historical_event_from_qdrant(event_id: int) -> None:
    """从 Qdrant 删除一条历史事件。"""
    client = _get_historical_qdrant_client()
    from qdrant_client.models import PointIdsList
    client.delete(
        collection_name=HISTORICAL_EVENTS_COLLECTION,
        points_selector=PointIdsList(points=[event_id]),
    )


class HistoricalEventsRetriever:
    """
    历史事件语义检索器。

    通过 Qdrant 向量检索 + session_id filter，找到与当前 query 最相关的历史事件。
    """

    def __init__(self):
        self._client: QdrantClient | None = None
        self._embed_model = None

    @property
    def client(self) -> QdrantClient:
        if self._client is None:
            self._client = _get_historical_qdrant_client()
        return self._client

    @property
    def embed_model(self):
        if self._embed_model is None:
            self._embed_model = _get_historical_embed_model()
        return self._embed_model

    def retrieve(
        self,
        query: str,
        session_id: str,
        top_k: int = HISTORICAL_EVENTS_TOP_K,
    ) -> list[dict]:
        """
        语义检索与 query 最相关的历史事件。

        Args:
            query: 当前用户输入
            session_id: 会话标识（强制 filter，隔离不同 session）
            top_k: 返回数量

        Returns:
            [{"content": "...", "source_turns": [...], "created_at": "..."}, ...]
        """
        try:
            query_vector = self.embed_model.get_text_embedding(query)

            from qdrant_client.models import Filter, FieldCondition, MatchValue

            results = self.client.query_points(
                collection_name=HISTORICAL_EVENTS_COLLECTION,
                query=query_vector,
                limit=top_k,
                query_filter=Filter(
                    must=[
                        FieldCondition(
                            key="session_id",
                            match=MatchValue(value=session_id),
                        )
                    ]
                ),
                with_payload=True,
            ).points

            events = []
            for p in results:
                payload = p.payload or {}
                source_turns = []
                raw_turns = payload.get("source_turns", "[]")
                if isinstance(raw_turns, str):
                    try:
                        source_turns = json.loads(raw_turns)
                    except (json.JSONDecodeError, TypeError):
                        pass
                elif isinstance(raw_turns, list):
                    source_turns = raw_turns

                events.append({
                    "content": payload.get("content", ""),
                    "source_turns": source_turns,
                    "created_at": payload.get("created_at", ""),
                    "score": getattr(p, "score", 0.0),
                })

            return events
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.warning(f"[HistoricalEventsRetriever] Retrieval failed: {e}")
            return []


# 单例
_historical_retriever: HistoricalEventsRetriever | None = None


def get_historical_events_retriever() -> HistoricalEventsRetriever:
    global _historical_retriever
    if _historical_retriever is None:
        _historical_retriever = HistoricalEventsRetriever()
    return _historical_retriever


def _normalize_text(text: str) -> str:
    """文本规范化：去空白、去标点、去箭头，用于去重比较。"""
    import re
    normalized = re.sub(r"\s+", "", text)
    # 移除中英文标点、箭头等（注意特殊字符需放在末尾避免被解释为范围）
    normalized = re.sub(r'[.,，。！!？?、：:；;""''「」『』【】()（）…/→↓↑←➜-]', "", normalized)
    return normalized.lower()


def _deduplicate_against_recent_turns(
    events: list[dict],
    recent_turns: list[dict] | None,
) -> list[dict]:
    """
    去重：如果历史事件内容与 recent_turns 中的某条高度相似，则去掉该事件。
    使用文本规范化精确去重兜底。
    """
    if not recent_turns:
        return events

    recent_texts = set()
    for turn in recent_turns:
        text = turn.get("text", "")
        norm = _normalize_text(text)
        if len(norm) >= 4:  # 太短的文本不去重
            recent_texts.add(norm)

    filtered = []
    for event in events:
        content = event.get("content", "")
        norm = _normalize_text(content)
        # 精确规范化匹配
        if norm in recent_texts:
            continue
        # 子串包含检查
        is_dup = False
        for rt in recent_texts:
            if len(norm) >= 8 and len(rt) >= 8:
                if norm in rt or rt in norm:
                    is_dup = True
                    break
        if not is_dup:
            filtered.append(event)

    return filtered


def get_formatted_historical_events(
    query: str,
    session_id: str,
    recent_turns: list[dict] | None = None,
    top_k: int = HISTORICAL_EVENTS_TOP_K,
) -> str:
    """
    检索并格式化历史摘要，供节点 prompt 注入使用。

    Returns:
        格式化后的 markdown 块，如：
        【长期记忆 - 历史摘要】
        - [0509] 俯卧撑手腕不适 → 改推胸机
        如果无相关事件则返回空字符串。
    """
    try:
        retriever = get_historical_events_retriever()
        events = retriever.retrieve(query, session_id, top_k=top_k)

        if not events:
            return ""

        # 去重
        events = _deduplicate_against_recent_turns(events, recent_turns)

        if not events:
            return ""

        lines = ["【长期记忆 - 历史摘要】"]
        for e in events:
            content = e.get("content", "")
            created_at = e.get("created_at", "")
            date_prefix = ""
            if created_at:
                # 格式化为 MMDD
                try:
                    date_prefix = created_at[5:10].replace("-", "")  # "0509"
                    date_prefix = f"[{date_prefix}] "
                except Exception:
                    pass
            lines.append(f"- {date_prefix}{content}")

        return "\n".join(lines)
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.warning(f"[HistoricalEvents] Format failed: {e}")
        return ""


def get_hybrid_retriever(collection_name: str) -> HybridRetriever:
    """创建混合检索器"""
    from qdrant_client import QdrantClient
    from backend.services.llm import get_embedding_model
    qdrant_client = QdrantClient(host=Config.QDRANT_HOST, port=Config.QDRANT_PORT)
    embed_model = get_embedding_model()
    return HybridRetriever(collection_name, qdrant_client, embed_model)
