# 阶段二存档：缓存体系设计

> 此文件为阶段二的完整存档，包含你的学习笔记。已完成于 2026-05-04。
> 进度追踪已迁移到主文档。

（以下为原文，一字未改）

---

## 阶段二：缓存体系设计

### 覆盖知识点

- 缓存穿透、击穿、雪崩的区别与解决方案
- Redis Hash 和 Sorted Set 的缓存场景
- 布隆过滤器原理（选学）
- 缓存更新策略：Cache-Aside、Read-Through、Write-Behind
- TTL 设计与缓存淘汰策略（LRU、LFU）

---

### 步骤 2.0：阅读关键代码并回答问题

打开 `Agent/retriever.py`，找到 `FitnessGuideRetrieverImpl.retrieve()` 方法（第 316 行）。打开 `Agent/nodes/guidance.py`，看 `guidance_node()` 如何使用 retriever。
guidance_node()会根据检索模型选择向量、BM25、还是混合检索。

**问题 1**：如果用户在 5 秒内问了两次完全相同的问题，检索会执行几次？Qdrant 会被查几次？
当前的逻辑是，每次请求都走完整检索流程。也就是两次检索，Qdrant 会被查4次。 一次 hybrid 检索实际命中 Qdrant 至少 2 次（向量搜索 1 次 + BM25 首次 scroll 1 次

<details>
<summary>提问目的</summary>

这道题让你意识到"没有缓存"的代价——每次请求都是全量检索流程，即使结果在一秒前刚查过。这是阶段二要解决的问题。理解检索的实际执行次数，才能感受到缓存的价值。

</details>

<details>
<summary>参考答案</summary>

两次问题 = 两次完整检索。每次都走完：embedding 向量化 → Qdrant 查询 → BM25 分词 → jieba 分词 → 排序打分 → RRF 融合。即使结果完全相同。

</details>

**问题 2**：检索流程中，哪一步最耗时？（不要求精确，根据你对系统的理解推测）

<details>
<summary>提问目的</summary>

这道题训练你的"直觉判断"能力——在没看到指标的情况下，根据架构推断瓶颈。向量化需要调 embedding API，BM25 需要从 Qdrant 拉全量文档。哪个更慢？

</details>

<details>
<summary>参考答案</summary>

- **embedding 向量化**：要调 DashScope API（网络 IO，耗时 ~100-500ms）
- **Qdrant 向量搜索**：本地（耗时 ~5-20ms）
- **BM25 初始化**：首次需要从 Qdrant scroll 全量文档 + jieba 分词（耗时 ~50-200ms），后续用内存索引（< 5ms）

最慢的是 embedding API 调用，其次是首次 BM25 初始化。

</details>

---

### 步骤 2.1：概念讲解 —— 缓存穿透、击穿、雪崩

这哥仨经常被搞混，用大白话解释：

- **缓存穿透**：查询一个不存在的数据，缓存和数据库都没有，每次请求都打到数据库。防护：空值缓存（短 TTL）
- **缓存击穿**：热点 key 过期瞬间，大量并发请求同时回源。防护：互斥锁
- **缓存雪崩**：大量 key 同时过期，数据库压力瞬间暴增。防护：TTL 随机偏移

---

### 步骤 2.1：动手操作 —— 检索结果缓存

**背景**：你的 `FitnessGuideRetrieverImpl.retrieve(query, top_k=3)` 每次调用都走完整检索流程，即使相同查询刚刚查过。

#### 第 1 小步：设计缓存 Key

思考题（先自己想，再看答案）：
- 直接用 query 字符串作为缓存 key 行不行？"如何练胸肌" 和 "如何 练胸肌" 算同一个查询吗？

<details>
<summary>参考答案</summary>

直接用原始 query 不行——多一个空格、换一个同义词就会 miss。最简单的做法是用 query 的 MD5 作为 key，在 hash 前做预处理（去多余空格、统一小写）。

对于本阶段学习，MD5 足够。后续如果要做语义去重（"练胸肌" = "胸肌训练"），那是 LLM rewrite 的事情，暂不考虑。

</details>

#### 第 2 小步：新建 `Agent/cache.py`

创建新文件 `Agent/cache.py`，内容如下：

```python
"""
缓存模块：检索结果缓存 + 食物查询缓存。
复用阶段一改造好的 Redis 连接池。
"""
import json
import hashlib
import time
import random
import logging
from typing import Optional

from Agent.memory.session_memory import _get_redis_client

logger = logging.getLogger(__name__)

# 缓存默认 TTL（秒）
RETRIEVAL_CACHE_TTL = 600       # 检索结果缓存 10 分钟
RETRIEVAL_CACHE_JITTER = 30     # TTL 随机偏移 ±30 秒
EMPTY_CACHE_TTL = 60            # 空值缓存 1 分钟（防穿透）


def _normalize_query(query: str) -> str:
    """查询预处理：去多余空格、统一小写"""
    return " ".join(query.strip().lower().split())


class RetrievalCache:
    """检索结果缓存，Cache-Aside 模式"""

    def __init__(self, ttl: int = RETRIEVAL_CACHE_TTL):
        self.ttl = ttl
        self._hits = 0
        self._misses = 0

    def _cache_key(self, query: str) -> str:
        q = _normalize_query(query)
        digest = hashlib.md5(q.encode()).hexdigest()
        return f"fit_agent:cache:retrieval:{digest}"

    def get(self, query: str) -> Optional[list[dict]]:
        """查缓存，命中返回结果列表，未命中返回 None"""
        client = _get_redis_client()
        if client is None:
            self._misses += 1
            return None

        key = self._cache_key(query)
        data = client.get(key)
        if data is None:
            self._misses += 1
            return None

        self._hits += 1
        return json.loads(data)

    def set(self, query: str, results: list[dict], is_empty: bool = False) -> None:
        """写入缓存。
        is_empty=True 表示查询无结果（空值缓存，短 TTL 防穿透）
        """
        client = _get_redis_client()
        if client is None:
            return

        key = self._cache_key(query)
        # TTL 加随机偏移，防止批量过期导致的雪崩
        ttl = EMPTY_CACHE_TTL if is_empty else self.ttl
        ttl += random.randint(-RETRIEVAL_CACHE_JITTER, RETRIEVAL_CACHE_JITTER)
        ttl = max(ttl, 10)  # 最小 10 秒

        try:
            client.setex(key, ttl, json.dumps(results, ensure_ascii=False))
        except Exception:
            logger.warning(f"Failed to write retrieval cache for key={key}", exc_info=True)

    def get_stats(self) -> dict:
        """返回缓存命中率统计"""
        total = self._hits + self._misses
        hit_rate = self._hits / total if total > 0 else 0.0
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(hit_rate, 3),
        }


# 全局单例
_retrieval_cache = RetrievalCache()


def get_retrieval_cache() -> RetrievalCache:
    return _retrieval_cache
```

> **关键设计解释**：
> 1. **MD5 做 key**：query → 预处理 → MD5 → 固定长度 key，避免特殊字符问题
> 2. **空值缓存**：`is_empty=True` 时 TTL 只有 60 秒，防止缓存穿透但不长期占坑
> 3. **TTL 随机偏移**：`random.randint(-30, 30)` 避免所有缓存在同一秒过期（防雪崩）
> 4. **ensure_ascii=False**：中文不转义，存储体积更小，redis-cli 查看也方便

#### 第 3 小步：接入检索流程

打开 `Agent/retriever.py`，找到 `FitnessGuideRetrieverImpl.retrieve()` 方法（约第 316 行）。

在方法**开头**加缓存检查，在**计算完结果后**写缓存。改后如下：

```python
def retrieve(self, query: str, top_k: int = 3, use_cache: bool = True) -> list[dict[str, Any]]:
    # ---- 查缓存 ----
    if use_cache:
        from Agent.cache import get_retrieval_cache
        cache = get_retrieval_cache()
        cached = cache.get(query)
        if cached is not None:
            return cached

    # ---- 原有检索逻辑（不变）----
    if self.retrieval_mode == "vector":
        chunks = self._vector.retrieve(query, top_k=top_k)
    elif self.retrieval_mode == "bm25":
        chunks = self._bm25.retrieve(query, top_k=top_k)
    else:
        self._hybrid.fusion_top_k = top_k
        chunks = self._hybrid.retrieve(query)

    result = [
        {"id": c.id, "text": c.text, "score": c.score, "metadata": c.metadata}
        for c in chunks
    ]

    # ---- 写缓存 ----
    if use_cache:
        cache.set(query, result, is_empty=(len(result) == 0))

    return result
```

#### 第 4 小步：在 `/health` 中暴露缓存命中率

打开 `backend/main.py`，在文件顶部 import 区域添加：

```python
from Agent.cache import get_retrieval_cache
```

找到 `/health` 端点，在返回字典中加上缓存统计：

```python
"cache_stats": get_retrieval_cache().get_stats(),
```

#### 验证方法

**验证 1**：确保不报错

```bash
cd /Users/liaomeiqi/Downloads/Fit-Agent
python -c "from Agent.cache import RetrievalCache, get_retrieval_cache; print('OK')"
```

**验证 2**：发两次相同请求，观察缓存命中

```bash
# 终端1：启动后端
python -m backend.main

# 终端2：
# 第一次请求（冷启动，cache miss）
curl -X POST http://localhost:8000/chat   -H "Content-Type: application/json"   -d "{"message": "深蹲怎么做"}"

# 等返回后，立即再发一次同样的请求（cache hit）
curl -X POST http://localhost:8000/chat   -H "Content-Type: application/json"   -d "{"message": "深蹲怎么做"}"

# 查看缓存统计
curl http://localhost:8000/health | python -m json.tool
```

预期：第二次请求明显更快（省去了 embedding + Qdrant 检索的时间），`cache_stats.hits` 至少为 1。

**验证 3**：验证空值缓存

```bash
# 查询一个不可能存在的内容
curl -X POST http://localhost:8000/chat   -H "Content-Type: application/json"   -d "{"message": "火星健身法"}"

# 再发一次同样的
curl -X POST http://localhost:8000/chat   -H "Content-Type: application/json"   -d "{"message": "火星健身法"}"
```

预期：第二次不会重复查 Qdrant（空值缓存命中，60 秒内直接返回空结果）。

---

### 步骤 2.2：概念讲解 —— 缓存更新策略

你现在实现的是 **Cache-Aside** 模式，也是最常用的：

其他两种模式（了解即可）：

| 模式 | 读 | 写 | 适用场景 |
|------|----|----|---------|
| Cache-Aside | 代码控制查缓存→查DB→回填 | 代码控制写DB→删缓存 | 读多写少（本阶段） |
| Read-Through | 缓存未命中时缓存自己去查DB | — | 完全托管给缓存层 |
| Write-Behind | — | 先写缓存，异步刷DB | 写多，可容忍少量丢失 |

---

### 步骤 2.2：动手操作 —— 食物数据查询缓存

**背景**：`Agent/nodes/diet_analysis_node.py` 里的 `FoodDataSearcher.search()` 每次调用都从 `food_data.json` 加载 + 遍历匹配。虽然 JSON 在内存中（单例缓存），但遍历查找仍有开销。更重要的是：**查不到的结果不会被缓存**，恶意查询可能造成穿透（虽然当前是本地 JSON，影响不大，但这是练习缓存穿透防护的好场景）。

#### 动手任务

在 `Agent/cache.py` 中新增 `FoodCache` 类，复用 `RetrievalCache` 的思路。

设计要求：
- `get(food_name) -> list[str] | None`：返回格式化的食物信息列表
- `set(food_name, results)`：写入缓存，**空结果用短 TTL**
- 接入 `diet_analysis_node.py` 的 `FoodDataSearcher.search()`
- TTL：正常结果 30 分钟，空结果 5 分钟

具体代码：

```python
# 在 Agent/cache.py 末尾添加

class FoodCache:
    """食物查询结果缓存"""

    def __init__(self, ttl: int = 1800):
        self.ttl = ttl  # 正常结果 30 分钟
        self.empty_ttl = 300  # 空值缓存 5 分钟
        self._hits = 0
        self._misses = 0

    def _cache_key(self, food_name: str) -> str:
        q = food_name.strip().lower()
        return f"fit_agent:cache:food:{q}"

    def get(self, food_name: str) -> Optional[list[str]]:
        client = _get_redis_client()
        if client is None:
            self._misses += 1
            return None
        key = self._cache_key(food_name)
        data = client.get(key)
        if data is None:
            self._misses += 1
            return None
        self._hits += 1
        return json.loads(data)

    def set(self, food_name: str, results: list[str]) -> None:
        client = _get_redis_client()
        if client is None:
            return
        key = self._cache_key(food_name)
        is_empty = len(results) == 0
        ttl = self.empty_ttl if is_empty else self.ttl
        ttl += random.randint(-30, 30)
        ttl = max(ttl, 10)
        try:
            client.setex(key, ttl, json.dumps(results, ensure_ascii=False))
        except Exception:
            pass

    def get_stats(self) -> dict:
        total = self._hits + self._misses
        hit_rate = self._hits / total if total > 0 else 0.0
        return {"hits": self._hits, "misses": self._misses, "hit_rate": round(hit_rate, 3)}


_food_cache = FoodCache()


def get_food_cache() -> FoodCache:
    return _food_cache
```

然后在 `diet_analysis_node.py` 的 `FoodDataSearcher.search()` 中接入：

```python
def search(self, query: str, exact_first: bool = True) -> list[str]:
    # ---- 查缓存 ----
    from Agent.cache import get_food_cache
    cache = get_food_cache()
    cached = cache.get(query)
    if cached is not None:
        return cached

    # ---- 原有逻辑（不变）----
    items = self._load_food_data()
    ...
    # （原有检索逻辑保持不动）

    # ---- 写缓存 ----
    cache.set(query, results)
    return results
```

#### 验证方法

```bash
# 查询一个已知食物
curl -X POST http://localhost:8000/chat   -H "Content-Type: application/json"   -d "{"message": "鸡胸肉的热量"}"

# 再查一次，应该更快（走缓存）
curl -X POST http://localhost:8000/chat   -H "Content-Type: application/json"   -d "{"message": "鸡胸肉的热量"}"

# 查一个不存在的食物
curl -X POST http://localhost:8000/chat   -H "Content-Type: application/json"   -d "{"message": "外星人蛋白质的热量"}"

# 立即再查，应该返回空（空值缓存命中）
curl -X POST http://localhost:8000/chat   -H "Content-Type: application/json"   -d "{"message": "外星人蛋白质的热量"}"
```

---

### 阶段二 理解测试

**问题 1**：你的检索缓存可能在哪些场景下发生"缓存穿透"？你在代码中用了什么方案防护？
我：缓存穿透是指，当某个空值（查询不存在的内容）一直被注入（查询）的时候，redis缓存一直不命中，导致一直去查数据库（每次穿透缓存直接打到了数据库），导致数据库奔溃
我们使用的方案是，即使是空值也使用短ttl进行缓存，这样一来可以避免缓存穿透，二来可以让没有作用的空值快速过期，确保redis的性能

<details>
<summary>提问目的</summary>

检验你是否能把"缓存穿透"这个抽象概念映射到你的实际代码中。重点看你是否发现了用户查询不存在内容（不会命中任何 Qdrant 文档）这个穿透场景，以及空值缓存是如何工作的。

</details>

**问题 2**："鸡胸肉的热量"缓存了 30 分钟。如果此时 `food_data.json` 更新了（鸡胸肉热量从 133 改到 165），用户看到的是哪个值？你有哪几种方式让用户看到最新值？
我：更新的内容在数据库里，鸡胸肉的缓存时间也过期了，此时查询应该是缓存不命中，去数据库读，读到了新数据，然后把新内容写回了缓存中❌
主动失效：在更新food_data.json的流程里，同步删除对应的redis key（写DB后删缓存）
手动删除缓存key，下次查询直接miss重新加载

<details>
<summary>提问目的</summary>

检验你对"缓存一致性"问题的理解。这是面试高频题——缓存和数据库数据不一致怎么办？你的回答应该体现"主动失效"的思路。

</details>

**问题 3**：你的检索缓存在哪种情况下可能发生"缓存击穿"？你当前代码有防护吗？
我：我只知道缓存击穿是指某一个热key突然失效，由于他的访问频率非常高，一旦失效，就会导致数据库压力增大。其他的我不太清楚

<details>
<summary>提问目的</summary>

检验你是否理解击穿和穿透的区别。如果你发现当前代码对击穿**没有**防护（热点 key 过期时多个请求同时回源），那说明你理解了问题所在。后续可以用互斥锁或"永不过期 + 异步刷新"来解决。

</details>
