# Fit-Agent 后端技能学习计划（手把手教学手册）

> **教学模式**：概念讲解 → 为什么要改 → 给你代码 → 教你验证 → 理解测试 → 下一阶段
>
> **适合人群**：有 Python 基础，但后端/数据库/分布式经验不足的开发者

---

## 使用说明

本文档的每个实操步骤都包含：

| 区块 | 内容 |
|------|------|
| 概念讲解 | 这个技术是什么，为什么需要 |
| 现状分析 | 当前项目代码哪里不够好 |
| 动手操作 | 你具体要执行什么命令、写什么代码 |
| 验证方法 | 怎么确认改对了（命令 + 预期输出） |
| 理解测试 | 检验你是否真正理解的题目，每题下方标注了**提问目的** |

---

## 目录

- [项目技术摸底](#项目技术摸底)
- [学习路线总览](#学习路线总览)
- [阶段一：并发编程 + Redis 连接池](#阶段一并发编程--redis-连接池)
- [阶段二：缓存体系设计](#阶段二缓存体系设计)
- [阶段三：关系型数据库入门](#阶段三关系型数据库入门)
- [阶段四：微服务基础 + 消息队列](#阶段四微服务基础--消息队列)
- [阶段五：分布式系统进阶](#阶段五分布式系统进阶)
- [阶段六：性能压测 + 系统稳定性](#阶段六性能压测--系统稳定性)

---

## 项目技术摸底

在开始写代码之前，先搞清楚当前项目长什么样。

### 你需要读的文件（按顺序）

| 序号 | 文件 | 核心关注点 |
|------|------|-----------|
| 1 | `config.py` | 项目依赖哪些外部服务？各自怎么配置？ |
由于生成回答内容的llm：longcat，用于向量化的qwen的text-embedding-v4，用于查询不到结果时候兜底的tavily，以及用于存储分块向量化后的向量数据库qdrant。
| 2 | `backend/main.py` | 一个 HTTP 请求进来后走什么流程？ |
一个http请求来了之后，首先触发鉴权机制，判断是否有权限访问。如果有权限，就继续workflow的内容，并且把workflow返回的内容返回给客户端。
| 3 | `Agent/workflow.py` | 系统支持几种 intent？分别路由到哪？ |
食物营养成分分析，食谱推荐、训练计划、训练动作指导、以及一般意图用于兜底
| 4 | `Agent/memory/session_memory.py` | 短期记忆存在哪里？怎么保证并发安全？ |
1.短期记忆我只是把它存在了redis，具体的实现我不太清楚，2.之前出现过并发覆盖的问题，我使用了lua原子化的方法来修复，但是我只是提出了一个解决方案，具体的实现依靠code agent来执行的.没有使用redis的乐观锁等机制是因为会引入更高重试延迟，lua原子化基于当前的操作，全都是在redis完成的，使用很适合使用，延迟低。如果我表达的不完整或者有错误，请帮我纠正
pending_intent = "training_plan"    ← "我还需要继续处理这个 intent"
把 "增肌" 填入 pending_entities.goal
pending_entities 收集完毕 → 生成训练计划
清空working_memory
| 5 | `Agent/retriever.py` | 检索流程几步？向量 + BM25 怎么融合？ |
检索流程我也忘记了。
| 6 | `Agent/nodes/state.py` | AgentState 有哪些字段？哪些跟多轮对话有关？ |
我只知道agentstate会维护一个全局的状态表。它会在workflow流动过程中不断更新这些字段的内容。具体的信息我也不清楚

每读完一个文件，告诉我你的理解（用自己的话概括这个文件做了什么），我确认后再读下一个。

---

## 学习路线总览

| 阶段 | 主题 | 核心改造文件 | 知识点 | 难度 |
|------|------|------------|--------|------|
| 一 | 并发 + Redis | `session_memory.py`, `config.py`, `main.py` | 线程池、连接池、Lua 原子操作 | ⭐⭐ |
| 二 | 缓存设计 | `cache.py`(新), `retriever.py` | 穿透/击穿/雪崩、缓存策略 | ⭐⭐⭐ |
| 三 | 关系型数据库 | `db.py`(新), `long_memory.py` | SQL、索引、事务 ACID | ⭐⭐⭐ |
| 四 | 消息队列 | `worker.py`(新), `main.py` | 异步解耦、幂等、限流 | ⭐⭐⭐⭐ |
| 五 | 分布式系统 | `lock.py`(新), 全系统 | 分布式锁、链路追踪、分库分表 | ⭐⭐⭐⭐⭐ |
| 六 | 压测 + 稳定性 | 全系统 | QPS/延迟、优雅关闭、健康检查 | ⭐⭐⭐ |

**当前进度：阶段三**

---

## 阶段一：并发编程 + Redis 连接池 ✅

> **已完成**。详细内容（含个人笔记）已存档到 → [`docs/backend_learning_plan_phase1_archive.md`](./backend_learning_plan_phase1_archive.md)

**成果摘要**：
- 为 Redis 添加了 `ConnectionPool`（10 个长连接复用，替代每次新建）
- 为线程池创建了全局 `ThreadPoolExecutor`（替代每次 `new` 后销毁）
- 新建 `metrics.py`，为 5 个 Redis 操作函数添加了耗时计时和慢查询告警
- `/health` 端点现在暴露线程池状态 + Redis 操作指标

**核心知识点**：GIL 在 IO 等待时自动释放 → IO 密集型多线程有效 → 连接池复用 TCP → 线程池控制并发上限 → 监控指标防雪崩

---

## 阶段二：缓存体系设计 ✅

> **已完成**。详细内容（含个人笔记）已存档到 → [`docs/backend_learning_plan_phase2_archive.md`](./backend_learning_plan_phase2_archive.md)

**成果摘要**：
- 新建 `Agent/cache.py`，包含 `RetrievalCache` 和 `FoodCache` 两个类
- 检索结果缓存（TTL 600s，空值 60s，带 TTL 抖动防雪崩）
- 食物查询缓存（TTL 1800s，空值 300s）
- `retriever.py` + `diet_analysis_node.py` 接入 Cache-Aside 模式
- `/health` 端点新增 `cache_stats` + `food_cache_stats`

**核心知识点**：穿透（空值短 TTL）→ 雪崩（TTL 随机抖动 ±30s）→ 击穿（代码无防护，需互斥锁）→ Cache-Aside 模式 → 缓存一致性（主动失效）

---

## 阶段三：关系型数据库入门（SQLite）✅

> **已完成**。详细内容（含个人笔记）已存档到 → [`docs/backend_learning_plan_phase3_archive.md`](./backend_learning_plan_phase3_archive.md)

**成果摘要**：
- 新建 `Agent/db.py`，用 SQLite 替代 Markdown 文件存储长期记忆（4 张表：`user_profile`、`user_attributes`、`conflict_log`、`chat_log`）
- 迁移 `long_memory.py` 的读写链路：`load_long_memory()` 从 SQLite 拼回 dict，`save_long_memory()` 拆解 dict 写 SQLite，对外接口不变
- 新增 `_sync_high_value_memory_if_changed()` 即时同步层：name/age/height/weight/gender/goal 用正则检测直接写库，不再等 10 轮 LLM 批次
- `planning_node` 计划生成成功后立即同步 `Active Plan Facts` 和 `user_profile.goal`
- 修复 15+ 个 bug：全角分号、缺分号、缺 UNIQUE 约束、变量未定义、缩进错位、None or 回退覆盖等
- `chat_log` 表持久化所有对话记录，为阶段四的事实回溯提供第二证据源

**核心知识点**：关系型 vs KV vs 向量数据库的访问模式差异 → 表设计（主键/外键/范式/EAV 模式）→ B+ 树索引原理（SCAN vs SEARCH）→ WAL 读写并发 → 事务 ACID → 并发写冲突（丢失更新 + 跨行保存无事务）→ 封装变化（改存储不改接口）

---
## 阶段四：微服务基础 + 消息队列 ✅

> **已完成**。详细内容（含个人笔记）已存档到 → [`docs/backend_learning_plan_phase4_archive.md`](./backend_learning_plan_phase4_archive.md)

**成果摘要**：
- 新建 `Agent/worker.py`，用 Redis List（LPUSH/BRPOP）实现轻量消息队列，替代同步 `update_if_needed()`
- 长期记忆更新异步化：触发时入队即返回，后台 Worker 线程独立消费，请求线程不再被 LLM 分析阻塞
- 新增 session 级滑动窗口限流（Redis Sorted Set，6 次/分钟）+ IP 级兜底限流（30 次/分钟，防不带 Cookie 绕过）
- 修复 Worker 独立 Redis 连接池（socket_timeout=30s）避免 BRPOP 与 socket 超时冲突
- 用 `lifespan` 上下文管理器替代 FastAPI 已弃用的 `@app.on_event()` 管理 Worker 生命周期

**核心知识点**：同步 vs 异步（慢操作拆出请求链路）→ Producer/Consumer 解耦 → BRPOP 阻塞消费 (timeout 0/5/60 的区别) → Redis Sorted Set 滑动窗口限流（ZADD/ZCARD/ZREMRANGEBYSCORE）→ fail-open 降级策略 → 幂等性（upsert 天然幂等）→ socket_timeout vs BRPOP timeout 冲突 → daemon 线程 vs 请求线程 → 有状态 vs 无状态（进程内 vs 进程外共享）

---

## 阶段五：分布式系统进阶

### 覆盖知识点

- 分布式锁：Redis SETNX + Lua 原子解锁、Redlock 算法、锁续期（watchdog）
- 链路追踪：Trace ID、Span、Context Propagation
- CAP 定理：一致性 vs 可用性 vs 分区容错
- 分库分表设计：垂直拆分 vs 水平拆分、分片键选择
- 分布式事务概念：2PC、TCC、Saga（了解即可）
- 负载均衡概念：轮询、加权轮询、一致性哈希（了解即可）

---

### 步骤 5.0：阅读关键代码并回答问题

打开以下文件，重点关注**没有防护**的地方：

- `Agent/cache.py` → `RetrievalCache.get()`（第 40 行）：热点 key 过期时，多个并发请求同时回源，目前**没有互斥锁防护**（阶段二已识别为已知风险）
- `Agent/workflow.py` → `run_workflow()`（第 510-542 行）：每次请求没有唯一追踪 ID，出了 bug 无法串联日志
- `Agent/db.py` → 4 张表全部在一个 `.db` 文件里：如果用户量从 100 涨到 10 万，怎么分？

**问题 1**：阶段二我们做了穿透和雪崩防护，但击穿没做。回顾缓存击穿的场景：热点 key（"深蹲怎么做"）刚好过期，10 个并发请求同时到来——现在的代码会发生什么？
我：由于热点key是会被频繁访问的key，如果突然过期，此时如果多个请求并发到来，缓存也无法命中，在写缓存的这段时间，所有请求全部打到数据库上，导致数据库压力过大。

embedding API被打10次（浪费钱、慢） Qdrant被查10次相同的query 10次结果依次写回缓存，最后一个覆盖前面的

<details>
<summary>提问目的</summary>

让你在自己的代码里找到真实存在的分布式并发问题，而不是抽象地学"分布式锁"。你会亲自用锁修好它。

</details>

<details>
<summary>参考答案</summary>

10 个请求全部 `cache.get(query)` 返回 None（key 已过期），然后全部进入 `retriever.retrieve()`，全部调用 embedding API + Qdrant 搜索。结果：
- embedding API 被打 10 次（浪费钱、慢）
- Qdrant 被查 10 次同样的 query
- 10 个结果依次写回缓存（最后一个覆盖前 9 个）

正确的行为：只有**第一个**请求去回源，其他 9 个等结果。

</details>

**问题 2**：`db.py` 里的 `user_profile`、`user_attributes`、`conflict_log`、`chat_log` 四张表，如果数据量涨到 1000 万行，哪张表最先成为瓶颈？为什么？

<details>
<summary>提问目的</summary>

训练你的"数据增长直觉"——不是所有表同步增长，chat_log 是增长最快的。理解各表的读写比例和增长速度是分库分表的前提。

</details>

<details>
<summary>参考答案</summary>

`chat_log` 最先成为瓶颈。原因：每次对话写入 2 条（user + assistant），增长速度为 O(请求数 × 2)。user_profile 行数 = 用户数（增长慢），user_attributes 行数 = 用户数 × 平均属性数（增长慢），conflict_log 只写冲突时（极少）。chat_log 是唯一"每请求必写"且只增不减的表。

当前 `idx_chat_session(session_id, created_at)` 只覆盖了按 session 查的路径。如果要跨 session 统计分析（"昨天所有用户的 intent 分布"），这个索引不够。

</details>

---

### 步骤 5.1：概念讲解 —— 分布式锁

#### 为什么需要分布式锁？

单机多线程用 `threading.Lock()` 就够了。但当前项目里，多个线程共享同一个 Redis 缓存，`threading.Lock()` 管不了——因为不同请求可能在不同线程甚至不同进程里处理。

分布式锁 = 一个**所有线程/进程都能看到的锁**，存在 Redis 里。

```
threading.Lock():           Redis 分布式锁:
  ├── 线程 A 拿到锁           ├── 请求 A: SETNX lock:key "owner-A" → 成功
  ├── 线程 B 阻塞等待         ├── 请求 B: SETNX lock:key "owner-B" → 失败
  └── 只在本进程内有效         ├── 请求 A 干完活，DEL lock:key
                              └── 所有连接 Redis 的进程都能看到
```

#### Redis 分布式锁的三个核心操作

| 操作 | Redis 命令 | 含义 |
|------|-----------|------|
| 加锁 | `SET lock:key owner_id NX PX 30000` | "如果 key 不存在，设为 owner_id，30 秒后自动过期" |
| 续期 | `EXPIRE lock:key 30000` | 防止锁在任务完成前过期（watchdog） |
| 解锁 | Lua 脚本：`if GET key == owner_id then DEL key end` | **必须判断 owner**，不能删别人的锁 |

**为什么解锁必须用 Lua？**

```
危险场景（非原子）：
  请求 A: GET lock:key → "owner-A"  ✓ 是我的锁
                    ← 此时锁过期了！
  请求 B: SETNX lock:key "owner-B" → 成功
  请求 A: DEL lock:key → 把 B 的锁删了！

安全场景（Lua 原子）：
  请求 A: EVAL "if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end"
          GET + DEL 在 Redis 服务端一条指令完成，不会被插入其他操作
```

#### Redlock 是什么？

你当前是单机 Redis，一把锁够用。但生产环境如果 Redis 是主从集群：

```
主 Redis 挂了，从 Redis 升为主：
  - 请求 A 在旧主上加锁（这条数据可能还没同步到从）
  - Redis 主从切换
  - 请求 B 在新主上加锁 → 成功（因为新主没收到 A 的锁数据）
  → 两个请求同时认为自己持有锁
```

Redlock 解决这个问题：向 N 个独立的 Redis 节点（通常是 5 个）分别加锁，超过半数（N/2+1）成功才算拿到锁。我们本阶段只用单机版本，Redlock 作为概念了解。

---

### 步骤 5.1：动手操作 —— 实现 Redis 分布式锁

#### 背景

实现一个 `RedisLock` 类，然后用它修复阶段二留下的缓存击穿问题。

#### 设计

```
加锁流程：
  SET lock:cache:retrieval:{digest} {owner_id} NX PX 30000
  → 成功：拿到锁，去回源
  → 失败：等 100ms，再试（最多重试 10 次）

回源完成后：
  → 写缓存（跟原来一样）
  → 释放锁（Lua 原子释放）

解锁流程（Lua）：
  if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
  else
      return 0
  end
```

#### 代码

新建 `Agent/lock.py`：

```python
"""
Redis 分布式锁模块。
用于修复缓存击穿等需要跨线程/跨进程互斥的场景。
"""
import uuid
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 锁的默认超时（毫秒）
DEFAULT_LOCK_TTL = 30000  # 30 秒

# 获取锁的最大重试次数和间隔
MAX_RETRIES = 10
RETRY_INTERVAL_MS = 100  # 100 毫秒

# Lua 解锁脚本（原子判断 owner + 删除）
_LUA_UNLOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


class RedisLock:
    """
    Redis 分布式锁。

    用法（上下文管理器）：
        lock = RedisLock("my_key")
        if lock.acquire():
            try:
                # 临界区代码
                pass
            finally:
                lock.release()

    或：
        with RedisLock("my_key") as acquired:
            if acquired:
                # 临界区代码
    """

    def __init__(self, lock_key: str, ttl_ms: int = DEFAULT_LOCK_TTL):
        self._lock_key = f"fit:lock:{lock_key}"
        self._ttl_ms = ttl_ms
        self._owner = str(uuid.uuid4())  # 每个锁实例有唯一 owner，防止误删
        self._acquired = False

    def acquire(self, retries: int = MAX_RETRIES) -> bool:
        """
        尝试获取锁。

        参数:
            retries: 最大重试次数

        返回:
            True 表示拿到锁，False 表示超时未拿到
        """
        from Agent.memory.session_memory import _get_redis_client

        for attempt in range(retries):
            client = _get_redis_client()
            if client is None:
                logger.warning(f"[LOCK] Redis unavailable, cannot acquire lock: {self._lock_key}")
                return False

            # SET key value NX PX ttl → 成功返回 True（key 之前不存在），失败返回 None
            acquired = client.set(
                self._lock_key,
                self._owner,
                nx=True,
                px=self._ttl_ms,
            )

            if acquired:
                self._acquired = True
                logger.debug(f"[LOCK] Acquired: {self._lock_key} by {self._owner[:8]}...")
                return True

            # 没拿到，等一会儿再试
            if attempt < retries - 1:
                time.sleep(RETRY_INTERVAL_MS / 1000.0)

        logger.warning(f"[LOCK] Failed to acquire after {retries} retries: {self._lock_key}")
        return False

    def release(self) -> bool:
        """
        释放锁（原子操作：只有 owner 匹配时才删除）。

        返回:
            True 表示成功释放，False 表示锁已被其他人持有或已过期
        """
        if not self._acquired:
            return False

        from Agent.memory.session_memory import _get_redis_client

        client = _get_redis_client()
        if client is None:
            logger.warning(f"[LOCK] Redis unavailable, cannot release lock: {self._lock_key}")
            self._acquired = False
            return False

        try:
            # 原子解锁：检查 owner 后再删除
            result = client.eval(_LUA_UNLOCK, 1, self._lock_key, self._owner)
            if result == 1:
                logger.debug(f"[LOCK] Released: {self._lock_key}")
            else:
                logger.warning(
                    f"[LOCK] Release failed (owner mismatch or expired): "
                    f"{self._lock_key} (owner={self._owner[:8]}...)"
                )
            self._acquired = False
            return result == 1
        except Exception as e:
            logger.error(f"[LOCK] Release error: {e}", exc_info=True)
            self._acquired = False
            return False

    def __enter__(self):
        self.acquire()
        return self._acquired

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False  # 不吞异常


def get_cache_lock(query: str) -> RedisLock:
    """
    获取缓存回源锁（解决缓存击穿）。
    用 normalized query 的 MD5 作为锁 key 的一部分。
    """
    import hashlib

    q = " ".join(query.strip().lower().split())
    digest = hashlib.md5(q.encode()).hexdigest()
    return RedisLock(f"cache:retrieval:{digest}", ttl_ms=10000)  # 10 秒足够检索
```

> **关键设计解释**：
>
> 1. **`owner = uuid4()`**：每个锁实例生成唯一 ID，解锁时校验，防止删别人的锁。
> 2. **`SET ... NX PX`**：NX = 只在 key 不存在时写入，PX = 设置毫秒级 TTL。**加锁 + 设过期是原子操作**（一条 Redis 命令），不会出现"加了锁但没设 TTL → 死锁"。
> 3. **解锁用 Lua**：`GET + 比较 + DEL` 三步在 Redis 服务端原子执行，避免"读到是自己的锁，删的时候发现已经过期被别人拿了"的竞态。
> 4. **重试不是自旋锁**：每次失败等 100ms，最多 10 次 = 1 秒。超过就直接返回 False，**调用方要处理拿不到锁的情况**（不能无限等）。
> 5. **`__enter__` / `__exit__`**：支持 `with` 语句，防止忘记释放锁。

---

### 步骤 5.1：动手操作 —— 用分布式锁修复缓存击穿

打开 `Agent/cache.py`，改造 `RetrievalCache.get()` 方法。核心思路：缓存 miss 时不直接返回 None，而是尝试拿锁 → 拿到锁的人去回源 → 其他人等一小段时间后再查缓存。

```python
def get(self, query: str, use_lock: bool = True) -> Optional[list[dict]]:
    """"
    查缓存，命中返回结果列表，未命中返回 None。
    use_lock=True 时启用分布式锁防击穿。
    """
    client = _get_redis_client()
    if client is None:
        self._misses += 1
        return None

    key = self._cache_key(query)
    data = client.get(key)
    if data is not None:
        self._hits += 1
        return json.loads(data)

    # 缓存未命中 —— 如果启用了锁防护，尝试获取回源锁
    if use_lock:
        from Agent.lock import get_cache_lock

        lock = get_cache_lock(query)
        if lock.acquire():
            # 拿到锁 —— 我负责回源（调用方负责回源后调 set()）
            # 注意：不在这里释放锁！调用方回源 + set() 后再释放。
            # 把锁对象存到实例变量，让调用方通过 release_lock() 释放。
            self._pending_lock = lock
            self._misses += 1
            return None

        # 没拿到锁 —— 别人正在回源，等一会儿再查缓存
        for _ in range(5):  # 最多等 500ms
            time.sleep(0.1)
            data = client.get(key)
            if data is not None:
                self._hits += 1
                return json.loads(data)

    # 最终 miss（锁也没拿到，等了也没等到）
    self._misses += 1
    return None


def release_after_backfill(self, query: str) -> None:
    """回源完成并写入缓存后，释放分布式锁（由调用方在 set() 后调用）。"""
    if hasattr(self, '_pending_lock') and self._pending_lock:
        self._pending_lock.release()
        self._pending_lock = None
```

**为什么要加 `release_after_backfill` 而不是在 `get` 里释放？**

因为锁要保护的不是"读缓存"这一步，而是"读缓存 miss → 回源检索 → 写缓存"这整段。

```
时间线（不加锁/锁提前释放）：
  请求A: cache miss → 拿锁 → 释放锁(?) → 开始回源...
  请求B: cache miss → 拿锁成功(因为A释放了!) → 也开始回源...
  → 又变成了 2 次回源，锁没起作用

时间线（正确）：
  请求A: cache miss → 拿锁 → 回源 → set() → release()
  请求B: cache miss → 拿锁失败 → 轮询等缓存 → cache hit!
  → 只有 1 次回源
```

#### 接入 retriever.py

打开 `Agent/retriever.py`，在 `retrieve()` 方法中，缓存 miss 走锁保护回源：

```python
def retrieve(self, query: str, top_k: int = 3, use_cache: bool = True) -> list[dict[str, Any]]:
    # ---- 查缓存（分布式锁防击穿）----
    if use_cache:
        from Agent.cache import get_retrieval_cache
        cache = get_retrieval_cache()
        cached = cache.get(query, use_lock=True)
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
        cache.release_after_backfill(query)  # 释放分布式锁

    return result
```

#### 验证方法

**验证 1**：确保不报错。

```bash
cd /Users/liaomeiqi/Downloads/Fit-Agent
python -c "from Agent.lock import RedisLock; print('OK')"
```

**验证 2**：手动模拟锁的互斥行为。

```python
# test_lock.py
import threading
import time
from Agent.lock import RedisLock

def worker(name):
    lock = RedisLock("test:concurrent", ttl_ms=5000)
    if lock.acquire(retries=10):
        print(f"[{name}] 拿到锁，开始干活...")
        time.sleep(2)  # 模拟回源耗时
        lock.release()
        print(f"[{name}] 释放锁")
    else:
        print(f"[{name}] 未拿到锁，放弃")

# 3 个线程同时抢锁
threads = [threading.Thread(target=worker, args=(f"worker-{i}",)) for i in range(3)]
for t in threads:
    t.start()
for t in threads:
    t.join()
```

预期输出：
```
[worker-0] 拿到锁，开始干活...
[worker-1] 未拿到锁，放弃
[worker-2] 未拿到锁，放弃
[worker-0] 释放锁
```

**验证 3**：确认锁不会误删。

```bash
python -c "
from Agent.lock import RedisLock
lock_a = RedisLock('test:owner', ttl_ms=10000)
lock_b = RedisLock('test:owner', ttl_ms=10000)

lock_a.acquire()
print(f'A拿到锁, owner={lock_a._owner[:8]}')

# B 尝试释放 A 的锁（用 B 自己的 owner）
result = lock_b.release()
print(f'B尝试释放锁: {"成功(这是bug!)" if result else "失败(正确！)"}')

# A 释放自己的锁
result = lock_a.release()
print(f'A释放自己的锁: {"成功" if result else "失败"}')
"
```

预期输出：
```
A拿到锁, owner=abc12345
B尝试释放锁: 失败(正确！)
A释放自己的锁: 成功
```

---

### 步骤 5.2：概念讲解 —— 链路追踪

#### 为什么需要链路追踪？

你现在排查问题是这样的：

```
用户报 bug："我刚才问深蹲怎么做，等了 10 秒才回复"

你去看日志：
  [LONG_MEMORY] TRIGGER: ...
  Slow Redis: trim_and_summarize took 230ms
  [WORKER] long_memory_update done
  ...

问题：上面这几行日志，哪些是同一个请求产生的？完全不知道。
```

链路追踪在每个请求入口生成一个 `trace_id`，所有后续日志都带上它：

```
[trace=abc123] intent_classifier: general
[trace=abc123] guidance_node: start retrieval
[trace=abc123] Slow Redis: append_turn took 55ms
[trace=abc123] workflow done: 3200ms

一眼就看出来：trace=abc123 这个请求，意图分类 + 检索 + Redis 慢操作，总耗时 3.2 秒。
```

#### 三个核心概念

| 概念 | 含义 | 在这个项目里的实现 |
|------|------|------------------|
| **Trace** | 一次完整请求的全链路（入口到出口） | `trace_id`: 在 `run_workflow()` 入口生成 |
| **Span** | 链路中的一段操作 | 每个 node 执行是一个 span（简化版：在 state 里记录 node 级别的开始/结束时间） |
| **Context Propagation** | 把 trace_id 传给所有下游调用 | 通过 `AgentState` 传 → 每个 node 读 state 记日志 |

---

### 步骤 5.2：动手操作 —— 给项目加链路追踪

#### 第 1 小步：在 AgentState 中加 trace_id

打开 `Agent/nodes/state.py`，在 `AgentState` 中添加 `trace_id` 字段：

```python
class AgentState(TypedDict):
    # ... 原有字段 ...

    # 链路追踪
    trace_id: str | None             # 请求级追踪 ID
```

#### 第 2 小步：在 run_workflow() 入口生成 trace_id

打开 `Agent/workflow.py`，在 `run_workflow()` 组装 `initial_state` 的地方（约第 518 行），加上 `trace_id`：

```python
import uuid

# 在 initial_state 字典中添加：
initial_state: AgentState = {
    # ... 原有字段 ...
    "trace_id": str(uuid.uuid4())[:8],  # 取前 8 位，短小可读
}
```

#### 第 3 小步：每个 node 打印带 trace_id 的日志

打开每个 node 文件（`intent_classifier.py`、`guidance.py`、`planning.py`、`diet_analysis_node.py`、`meal_planning_node.py`、`general_conversation_node.py`），在每个 node 函数的开头和结尾加日志。

示例——`intent_classifier.py` 的 `intent_classifier_node()`：

```python
def intent_classifier_node(state: AgentState) -> dict:
    trace_id = state.get("trace_id", "unknown")
    user_input = state["user_input"]
    logger.info(f"[trace={trace_id}] intent_classifier start")

    # ... 原有逻辑 ...

    logger.info(
        f"[trace={trace_id}] intent_classifier done: "
        f"primary={result.get('primary_intent', {}).get('type')}"
    )
    return result
```

其他 node 同理，在函数开头和结尾加一行 `logger.info(f"[trace={trace_id}] xxx_node start/done")`。

#### 第 4 小步：在 workflow 出口打印总耗时

在 `workflow.py` 的 `run_workflow()` 中，记录 total elapsed：

```python
import time as time_module

def run_workflow(user_input: str, session_id: str) -> dict:
    trace_id = str(uuid.uuid4())[:8]
    t_start = time_module.monotonic()

    # ... 原有逻辑 ...

    # 在 return result 之前：
    elapsed = (time_module.monotonic() - t_start) * 1000
    logger.info(f"[trace={trace_id}] workflow done: {elapsed:.0f}ms, intent={result.get('primary_intent')}")

    return result
```

#### 第 5 小步：trace_id 透传到 API 响应

打开 `backend/main.py`，在 `/chat` 和 `/chat/stream` 的响应中加上 `trace_id`。这样前端报 bug 时可以直接贴 trace_id。

`/chat` 端点：

```python
resp = {
    # ... 原有字段 ...
    "trace_id": result.get("trace_id"),
}
```

#### 验证方法

**验证 1**：发一个请求，在日志中搜索同一个 trace_id。

```bash
# 终端 1：启动后端
python -m backend.main

# 终端 2：发一个请求
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"深蹲怎么做"}' | python -c "import sys,json; d=json.load(sys.stdin); print(f'trace_id={d.get(\"trace_id\")}')"

# 终端 1 的日志中搜索这个 trace_id，确认所有相关日志都带了它
```

预期看到类似：
```
[trace=a1b2c3d4] intent_classifier start
[trace=a1b2c3d4] intent_classifier done: primary=training_guidance
[trace=a1b2c3d4] guidance_node start
...
[trace=a1b2c3d4] workflow done: 3200ms, intent=training_guidance
```

**验证 2**：确认同一 trace_id 贯穿整个链路。

```bash
# 发请求获取 trace_id
TRACE=$(curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"你好"}' | python -c "import sys,json; print(json.load(sys.stdin).get('trace_id',''))")
echo "trace_id=$TRACE"

# 在服务端日志中 grep 这个 trace_id（需要先有日志文件）
# grep "trace=$TRACE" <log_file>
```

---

### 步骤 5.3：概念讲解 —— CAP 定理

你已经不知不觉用到了 CAP 定理中的取舍。CAP = **C**onsistency（一致性）、**A**vailability（可用性）、**P**artition Tolerance（分区容错），三者只能同时满足两个。

**你的项目里已经发生的 CAP 取舍**：

| 场景 | C（一致性） | A（可用性） | P（分区容错） | 你的选择 |
|------|-----------|-----------|------------|---------|
| Redis 挂了，session memory fallback 到进程内存 | 牺牲（不同进程看到不同数据） | 保证（请求不打挂） | 保证 | **AP**（阶段一 `_fallback_store`） |
| Rate limit Redis 挂了，fail-open 放行 | 牺牲（限流失效） | 保证（请求不打挂） | 保证 | **AP**（阶段四 `check_rate_limit` 的 `except: return True`） |
| SQLite 单文件，无法分布式部署 | 保证（单点强一致） | 牺牲（单机瓶颈） | 牺牲 | **CA** |
| 长期记忆 Worker 异步更新 | 牺牲（更新有延迟） | 保证（请求不阻塞） | 保证 | **AP**（阶段四异步化） |

**为什么大多数场景你选了 AP？**

Fit-Agent 是健身助手，不是银行。对话卡住比数据短暂不一致更影响用户体验。这就是"最终一致性"的哲学——数据最终会一致，但可用性每时每刻都要保证。

---

### 步骤 5.3：动手操作 —— 分库分表设计文档

不写代码，做一个设计练习。当前 `fit_agent.db` 如果从 100 个用户涨到 10 万个，回答问题：
 │                              fit_agent.db                                    │
    └──────────────────────────────────────────────────────────────────────────────┘
    
    
    ┌──────────────────────────────┐     ┌───────────────────────────────────────┐
    │      user_profile            │     │         user_attributes              │
    │      (用户档案，主表)          │     │         (用户属性，EAV 模式)           │
    ├──────────────────────────────┤     ├───────────────────────────────────────┤
    │ session_id   TEXT   PK       │ 1───∞│ id            INTEGER  PK  AI       │
    │ name         TEXT             │     │ session_id    TEXT      FK ──────────┼──┐
    │ gender       TEXT             │     │ category      TEXT      NOT NULL    │  │
    │ age          TEXT             │     │ key           TEXT      NOT NULL    │  │
    │ height       TEXT             │     │ value         TEXT      NOT NULL    │  │
    │ weight       TEXT             │     │ created_at    TEXT      NOT NULL   │  │
    │ training_level TEXT           │     │ updated_at    TEXT      NOT NULL   │  │
    │ goal         TEXT             │     │ UNIQUE(session_id,category,key)      │  │
    │ created_at   TEXT   NOT NULL │     └───────────────────────────────────────┘  │
    │ updated_at   TEXT   NOT NULL │                                               │
    └──────────────────────────────┘                                               │
              │                                                                  │
              │ 1───∞                                                              │
              ▼                                                                   │
    ┌──────────────────────────────┐     ┌───────────────────────────────────────┐
    │      conflict_log             │     │          chat_log                    │
    │      (冲突日志)                 │     │          (聊天日志)                   │
    ├──────────────────────────────┤     ├───────────────────────────────────────┤
    │ id           INTEGER  PK  AI  │     │ id            INTEGER  PK  AI         │
    │ session_id   TEXT    FK ──────────│ session_id    TEXT      FK ──────────┼──┘
    │ field_name   TEXT    NOT NULL│     │ role          TEXT      NOT NULL  (user/agent)│
    │ old_value    TEXT             │     │ content       TEXT      NOT NULL     │
    │ new_value    TEXT             │     │ intent        TEXT      (预留，暂未用)│
    │ source       TEXT             │     │ created_at    TEXT      NOT NULL     │
    │ created_at   TEXT   NOT NULL  │     └───────────────────────────────────────┘
    └──────────────────────────────┘

**场景**：10 万用户、平均每人 50 轮对话、chat_log 表 500 万行。

#### 问题

**1. 垂直拆分**：把 4 张表拆到不同数据库。怎么拆？理由？
我：垂直和水平是什么意思呢？

**2. 水平拆分**：chat_log 表按什么字段分片？session_id 还是 created_at？各自优缺点？
我：按session_id分片，就有100000个分片，如果安装created_at分片，分片数不知道怎么计算。 我也不确定按什么分

**3. 分片后的跨片查询**："统计所有用户的 intent 分布"怎么查？

把你的答案写在 `docs/backend_learning_plan_phase5_design.md`（新建文件），完成后给我 review。

<details>
<summary>设计提示（先自己想，实在想不出再看）</summary>

**垂直拆分思路**：
- `user_profile` + `user_attributes` → 用户库（读多写少，高频查）
- `chat_log` → 日志库（写多读少，独立存储，可归档）
- `conflict_log` → 审计库（极少读写，可放用户库或独立）

**水平拆分思路**：
- 按 `session_id` hash 分片：同一用户的所有聊天在一起，按用户查方便。但热点用户（一个 session 对话极多）会导致数据不均衡。
- 按 `created_at` 时间分片（如按月分表）：写入均匀，归档方便。但跨月查询需要查多张表。

**跨片查询思路**：
- 应用层聚合：分别查每个分片，在应用代码合并统计
- 或者：保留一份 ES/ClickHouse 副本做分析查询（读写分离）

</details>

---

### 步骤 5.4：分布式事务概念（了解即可）

当前项目 SQLite + Redis，**没有跨存储的事务需求**。但如果未来拆成微服务：

**场景**：用户更新训练目标，需要同时修改 `user_profile.goal`（SQLite）和清除 Redis 缓存中的旧计划。

```
现在（单机，可接受不一致）：
  upsert_profile(sid, {"goal": "减脂"})   ← 成功
  redis.delete("plan:cache:{sid}")         ← 失败 → 用户看到的还是旧的计划

未来（需要一致性）：
  BEGIN 分布式事务
    upsert_profile(sid, {"goal": "减脂"})
    redis.delete("plan:cache:{sid}")
  COMMIT / ROLLBACK
```

三种分布式事务方案（了解名字和区别即可）：

| 方案 | 核心思想 | 一致性 | 性能 | 复杂度 |
|------|---------|--------|------|--------|
| **2PC** | 协调者问所有人"准备好了吗" → 全准备好才提交 | 强 | 低（阻塞等） | 低 |
| **TCC** | Try（预留资源）→ Confirm（确认）→ Cancel（回滚） | 最终 | 高 | 高 |
| **Saga** | 每步一个操作 + 一个补偿操作，失败了逐步回滚 | 最终 | 高 | 中 |

对于 Fit-Agent 当前规模，不需要分布式事务。如果拆了微服务，用 Saga 模式最合适（每步独立、天然异步）。

---

### 阶段五 理解测试

**问题 1**：你实现的 `RedisLock` 中，解锁为什么必须用 Lua 脚本？如果改成 `client.delete(key)` 会有什么问题？
我：解锁的过程如果是分步操作的话，例如先解锁，再删key，可能会误删别人的key。所以解锁和del要原子进行
在确认GET检查是不是当前线程的锁结束之后，其他线程可能会立即持有锁，这时候还没有DEL操作，但是我已经验证身份了，所以会误删当前持有锁的key
解锁需要两步：先检查锁的 owner 是不是自己（GET），如果是才删除（DEL）。如果分两步执行，锁可能在 GET 之后、DEL                          
之前过期，另一个线程拿到新锁，我的 DEL 就把别人的锁删了。Lua 脚本把 GET + 比较 + DEL 打包成一条指令在 Redis 
服务端原子执行，中间不会插入任何其他操作。 
<details>
<summary>提问目的</summary>

考察你是否理解了"原子性"在分布式锁中的关键作用。不是 Lua 语法本身重要，而是 GET + 比较 + DEL 三个操作必须在一个原子步骤里完成。

</details>

**问题 2**：`RedisLock` 的 `ttl_ms=30000` 是什么意思？如果你的回源检索需要 40 秒，会发生什么？你会怎么解决？
我：ttl_ms 表示持有锁的最长时间，如果回源检索超过了锁的到期时间，当前的线程就会自动释放锁，其他线程就可以加锁了。回源检索的结果没有被锁保护，相当于没有上锁，可能会出现缓存击穿的情况。
<details>
<summary>提问目的</summary>

考察你是否理解了锁 TTL 和任务耗时之间的关系。如果任务比锁 TTL 长，锁过期了别人也能加锁，互斥失效。解决方案是"续期"（watchdog）——任务执行期间定期延长锁的 TTL。

</details>

**问题 3**：你的链路追踪里，`trace_id` 是存在 `AgentState` 里的。如果未来你把 `chat_log` 的写入从请求链路拆到消息队列（跟长期记忆一样），`trace_id` 怎么在请求线程和 Worker 线程之间传递？
我：我记得异步分离长期记忆的时候，是把某个过程写成一个“纸条”，然后把这个纸条塞进消息队列里面，消息队列收到之后按照先来先处理的方式执行。优点前端用户体验感提升，缺点是增大了数据更新的延迟。所以我觉得把 `chat_log` 的写入从请求链路拆到消息队列，应该也是差不多的步骤。
<details>
<summary>提问目的</summary>

考察你是否理解了"Context Propagation"——trace_id 不能只靠 AgentState 传，因为 AgentState 只在请求线程内。跨线程/跨进程需要把 trace_id 显式塞进消息体。

</details>

**问题 4**：回顾你前四个阶段的所有改造，指出至少两处你已经做了"最终一致性"（牺牲即时一致性换取可用性）的取舍。

<details>
<summary>提问目的</summary>

让你把 CAP 定理和你的代码关联起来，而不是抽象背诵。异步长期记忆更新、fallback 内存字典、fail-open 限流——这些都是 AP 优先于 CP 的实例。

</details>

---

### 进度追踪

### 阶段五：分布式进阶
- [ ] 5.0 阅读关键代码 + 回答教练 2 个问题
- [ ] 5.1.1 理解分布式锁概念
- [ ] 5.1.2 新建 `Agent/lock.py`
- [ ] 5.1.3 用分布式锁修复缓存击穿（改造 `cache.py` + `retriever.py`）
- [ ] 5.1.4 验证锁的互斥 + 防误删
- [ ] 5.2.1 在 AgentState 加 trace_id
- [ ] 5.2.2 各 node 加 trace 日志
- [ ] 5.2.3 workflow 出口打印总耗时
- [ ] 5.2.4 API 响应透传 trace_id
- [ ] 5.2.5 验证链路追踪
- [ ] 5.3 分库分表设计文档
- [ ] 阶段五理解测试（4 题）

---

## 阶段五：分布式系统进阶 ✅

> **已完成**。详细内容（含个人笔记）已存档到 → [`docs/backend_learning_plan_phase5_archive.md`](./backend_learning_plan_phase5_archive.md)

**成果摘要**：
- 新建 `Agent/lock.py`，实现 Redis 分布式锁（SET NX PX + Lua 原子解锁 + UUID owner 防误删）
- 用分布式锁修复缓存击穿：`RetrievalCache.get()` miss 时拿锁回源，其他请求等缓存写入
- 新建 `docs/backend_learning_plan_phase5_design.md` 分库分表设计文档
- 在 `AgentState` 中加 `trace_id`，各 node 和 workflow 出口打印链路日志
- trace_id 透传到 API 响应（`/chat` 和 `/chat/stream`），方便前端报 bug 时关联日志

**核心知识点**：SETNX + PX 原子加锁 → Lua 原子解锁防误删 → 锁 TTL vs 任务耗时的 watchdog 续期 → 缓存击穿（锁保护回源）→ trace_id 串联全链路日志 → CAP 定理 AP 取舍（fallback、fail-open、异步化）→ 垂直拆分 vs 水平拆分 → 分片键选择（session_id hash vs 时间）→ 跨片查询聚合 → 2PC/TCC/Saga 分布式事务

---

## 阶段六：性能压测 + 系统稳定性

> 阶段六是你的最后一个阶段。学完后，你已经完成了 Fit-Agent 后端完整技术体系的学习：并发编程 → 缓存 → 数据库 → 消息队列 → 分布式 → 压测稳定性。

### 覆盖知识点

- 性能指标：QPS、响应延迟（P50/P90/P99）、错误率
- 压测工具：Locust（Python 编写，可脚本化场景）
- 系统瓶颈定位：CPU bound / IO bound / Memory bound
- 容量规划：根据压测结果推算单机上限、扩缩容策略
- 优雅关闭：进程退出时如何确保请求处理完毕再退出
- 健康检查：`/health` 端点的最佳实践（存活探针 + 就绪探针）

### 本阶段要解决什么问题

当前你不知道系统能承受多少并发、瓶颈在哪、哪个节点最慢。压测之后才能做容量规划，知道什么时候该加机器。

---

### 步骤 6.0：概念讲解 —— 性能指标

#### 三个核心指标

| 指标 | 含义 | 你项目里怎么量 |
|------|------|--------------|
| **QPS** | 每秒处理请求数（Throughput） | 压测工具控制并发，观察 `/health` 的 `tasks_completed` 增长速率 |
| **延迟** | 一个请求从发起到收到响应的时间（Latency） | 压测工具自动统计：P50（中位数）、P90（90%请求在XXms内）、P99 |
| **错误率** | 失败请求 / 总请求 | 压测工具统计 4xx/5xx 响应数量 |

#### 延迟分位值的含义

```
P50 = 200ms  → 50% 的请求在 200ms 内完成
P90 = 500ms  → 90% 的请求在 500ms 内完成
P99 = 2000ms → 99% 的请求在 2000ms 内完成

如果 P50 和 P99 差距很大，说明有少量"慢请求"拉高了尾延迟
（比如 Redis 偶尔抖动、LLM API 不稳定）
```

#### 你的系统的延迟构成

```
用户请求延迟 ≈ LLM 生成时间（3-10s，波动大）
              + Redis 操作（1-5ms，稳定）
              + workflow node 执行（50-200ms）

其中 LLM 是最大的不稳定因素
```

---

### 步骤 6.1：概念讲解 —— Locust 压测工具

#### 为什么用 Locust

| 压测工具 | 特点 |
|---------|------|
| `ab`（Apache Bench） | 简单，但不能模拟真实用户行为，只能发固定请求 |
| `wrk` | 高性能，但脚本能力弱 |
| **Locust** | Python 脚本定义用户行为，支持 Assertions，统计详细，**推荐** |

Locust 的核心概念：
- **TaskSet**：一组用户行为（访问哪些接口、按什么顺序）
- **Task**：单个行为（如发 POST /chat）
- **HttpUser**：模拟一个真实用户
- **Locust**（Master）：协调进程，分发任务到 Slave

#### Locust vs 线程/进程压测的区别

```
ab/wrk：直接发请求，不关心"用户是谁"
Locust：模拟"真实用户"的行为
  → 用户A：发消息 → 等3秒 → 发下一条
  → 用户B：发消息 → 等8秒 → 发下一条
  → 更接近生产环境的真实流量
```

---

### 步骤 6.1：动手操作 —— 编写 Locust 压测脚本

#### 安装

```bash
pip install locust
```

#### 编写压测脚本

在项目根目录新建 `locustfile.py`：

```python
"""
Locust 压测脚本：模拟真实用户对 Fit-Agent 的聊天请求。
"""
import json
import random
from locust import HttpUser, task, between, events

# 示例查询，模拟真实用户问题
SAMPLE_MESSAGES = [
    "深蹲怎么做",
    "减脂饮食计划",
    "跑步最佳时间",
    "增肌吃什么",
    "训练后拉伸动作",
    "今天是力量训练还是和有氧",
    "我体重 70kg，身高 175cm，怎么饮食",
    "卧推怎么练",
    "俯卧撑一天做多少个合适",
    "你好",
]


class ChatUser(HttpUser):
    """
    模拟一个真实聊天用户的行为。
    """
    # 等待时间：模拟用户打字、思考的时间（1-5秒随机）
    wait_time = between(1, 5)

    def on_start(self):
        """用户开始时执行一次：建立会话"""
        # 不需要登录，直接发消息创建 session
        self.session_id = None

    @task
    def chat_message(self):
        """发送一条聊天消息"""
        message = random.choice(SAMPLE_MESSAGES)

        # 第一次发消息没有 session_id，后续带上
        payload = {"message": message}
        if self.session_id:
            payload["session_id"] = self.session_id

        with self.client.post(
            "/chat",
            json=payload,
            catch_response=True,
            name="/chat"
        ) as resp:
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    # 保存 session_id 用于后续请求
                    if "session_id" in data:
                        self.session_id = data["session_id"]
                    resp.success()
                except Exception:
                    resp.failure("Invalid JSON response")
            elif resp.status_code == 429:
                # 限流，标记为预期行为，不算失败
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}")


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print("压测开始！")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    print("压测结束！")
```

#### 执行压测

**方式 1：单机压测（终端运行）**

```bash
cd /Users/liaomeiqi/Downloads/Fit-Agent
locust -f locustfile.py --host=http://localhost:8000 --headless -u 10 -r 2 -t 60s
```

参数说明：
| 参数 | 含义 |
|------|------|
| `-u 10` | 10 个并发用户 |
| `-r 2` | 每秒启动 2 个新用户（ ramp-up） |
| `-t 60s` | 总共压测 60 秒 |
| `--headless` | 无界面，终端运行 |
| `--host` | 目标地址 |

**方式 2：有界面运行（浏览器操作）**

```bash
locust -f locustfile.py --host=http://localhost:8000
# 然后打开浏览器访问 http://localhost:8089
```

---

### 步骤 6.1：动手操作 —— 运行压测并解读报告

#### 压测步骤

```bash
# 1. 启动后端服务（终端1）
cd /Users/liaomeiqi/Downloads/Fit-Agent
python -m backend.main

# 2. 启动 Locust 压测（终端2）
# 先小规模压测：5用户，30秒
locust -f locustfile.py --host=http://localhost:8000 --headless -u 5 -r 1 -t 30s --print-stats
```

#### 压测报告解读

```
Statistics
-----------
Type          | Name    | # reqs  | # fails | Avg    | P50   | P90   | P99  | Byte
--------------+---------+---------+---------+--------+-------+-------+------+------
GET           | /health |    150  | 0 (0%)  | 3ms    | 3ms   | 4ms   | 8ms  | 256
POST          | /chat   |     50  | 3 (6%)  | 5234ms | 4500ms| 8000ms|12000ms| 512

Percentiles (ms)
-----------
50%      4500
66%      5500
75%      7000
80%      7500
90%      8000
95%      9500
99%     12000
99.9%   15000
```

**关键观察**：

1. **`/chat` P99 = 12s** → 99% 的请求在 12 秒内完成，1% 的请求超过 12 秒
2. **错误率 6%** → 有 3 个请求失败（可能是限流 429 或超时）
3. **`/health` 稳定在 3-4ms** → Redis 和系统本身很快，慢在 LLM 生成

#### 找瓶颈

逐步加压，观察哪里先扛不住：

```
5 用户：全部正常，P90 < 8s
20 用户：开始出现 429（P99 下降，说明有限流保护）
50 用户：429 增多，说明线程池/限流开始生效
```

---

### 步骤 6.2：概念讲解 —— 瓶颈定位

#### 三种系统瓶颈

| 瓶颈类型 | 特征 | 解决方法 |
|----------|------|----------|
| **CPU bound** | CPU 使用率 100%，请求排队等 CPU | 多进程、升配置、减少计算 |
| **IO bound** | 大部分时间在等 IO（网络/磁盘），CPU 空闲 | 异步 IO、增加并发、缓存 |
| **Memory bound** | 内存耗尽，频繁 GC 或 OOM | 减少内存占用、加内存 |

#### 你的系统的瓶颈分析

```
Fit-Agent 是典型的 IO bound 系统：
  → 等 LLM API 返回（最大瓶颈，5-15秒/请求）
  → 等 Redis 返回（< 5ms，可忽略）
  → 等 Qdrant 返回（< 50ms，可忽略）

线程池 max_workers=10 的意义：
  → 10 个线程同时等 LLM
  → 11-20 个请求在队列等

如果 LLM 响应时间变长：
  → 线程池打满 → 队列积压 → P99 暴涨
  → 这是你系统的扩容信号
```

---

### 步骤 6.2：动手操作 —— 系统压测 + 优化报告

#### 第 1 步：记录基准数据

用 Locust 做一次完整压测，记录以下数据：

```bash
# 保存压测结果到 CSV
locust -f locustfile.py --host=http://localhost:8000 \
  --headless -u 20 -r 2 -t 120s \
  --csv=/tmp/locust_report/baseline

# 查看生成的文件
ls /tmp/locust_report/
# baseline_stats.csv  baseline_stats_history.csv  baseline_failures.csv  baseline_exceptions.csv
```

#### 第 2 步：分析数据

```python
# 分析 baseline_stats.csv
import pandas as pd

df = pd.read_csv('/tmp/locust_report/baseline_stats.csv')
chat_row = df[df['Name'] == '/chat'].iloc[0]

print(f"QPS: {chat_row['Requests/s']:.2f}")
print(f"P50 延迟: {chat_row['50%']} ms")
print(f"P90 延迟: {chat_row['90%']} ms")
print(f"P99 延迟: {chat_row['99%']} ms")
print(f"错误率: {chat_row['Failures'] / (chat_row['Requests'] + chat_row['Failures']) * 100:.1f}%")
```

#### 第 3 步：识别瓶颈点

用 `/health` 端点监控线程池状态：

```bash
# 压测期间，每 5 秒查一次 health
while true; do
  curl -s http://localhost:8000/health | python -m json.tool
  sleep 5
done
```

观察：
- `queue_size` 是不是在涨？（线程池队列积压）
- `active_threads` 是不是接近 `max_workers`？（线程池打满）

#### 第 4 步：写优化报告

在 `docs/backend_learning_plan_phase6_report.md` 中记录：

```
# 压测报告

## 测试环境
- 机器配置：MacBook Pro M2, 16GB RAM
- 后端：Python 3.11, FastAPI + Uvicorn
- 目标服务：http://localhost:8000

## 基线测试（20 用户，120 秒）

| 指标 | 数值 |
|------|------|
| 总请求数 | X |
| QPS | X |
| P50 延迟 | X ms |
| P90 延迟 | X ms |
| P99 延迟 | X ms |
| 错误率 | X% |

## 瓶颈分析

1. 线程池状态：max_workers=10，active_threads 经常打满
2. 主要延迟来源：LLM API（5-15s），Redis < 5ms
3. 限流生效：超过 6 req/min 的 session 开始返回 429

## 优化建议

1. 增加线程池 max_workers（如果 LLM 是瓶颈，增加线程帮助并发）
2. 添加更多 LLM API 副本（如果公司有资源）
3. 优化 prompt 长度（减少 token 数量可显著降低 LLM 延迟）
```

---

### 步骤 6.3：概念讲解 —— 优雅关闭

#### 什么是优雅关闭？

```
暴力关闭（不优雅）：
  kill -9 进程
  → 请求正在处理中，直接中断
  → 用户看到 connection reset 错误
  → 可能丢失数据

优雅关闭：
  kill -15（SIGTERM）进程
  → 进程收到信号，停止接收新请求
  → 处理完当前所有请求（包括队列里的）
  → 然后才退出
```

#### 你现在的代码有没有优雅关闭？

检查 `main.py` 中 FastAPI 的启动方式：

```python
# 错误写法（不优雅）：
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
    # Ctrl+C 时，如果有请求还在处理，会直接中断

# 正确写法（使用 lifespan 或 on_event）：
# 阶段四已经用 lifespan 管理 Worker 生命周期
# uvicorn 默认支持优雅关闭（SIGTERM 处理）
```

#### 验证优雅关闭

```bash
# 1. 启动服务
python -m backend.main

# 2. 发起一个耗时请求（不要 kill，先发请求）
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"深蹲怎么做"}'

# 3. 在服务运行时，按 Ctrl+C（发送 SIGINT）
# 观察服务是否等到请求处理完才退出（应该看到 "Shutting down" 日志）
```

---

### 步骤 6.3：动手操作 —— 健康检查最佳实践

#### 当前 `/health` 的问题

现在 `/health` 返回：

```json
{
  "status": "ok",
  "thread_pool": {...},
  "redis_metrics": {...}
}
```

但它**没有区分两种状态**：
- **Liveness（存活）**：进程还活着，能处理请求吗？（Redis 挂了也返回 ok）
- **Readiness（就绪）**：能接收新请求吗？（依赖的 LLM/Redis/Qdrant 都正常吗）

#### 改造成两个端点

```python
@app.get("/health/live")
def liveness():
    """
    存活探针：Kubernetes 判断进程是否活着。
    只要进程在运行就返回 ok，不检查依赖。
    """
    return {"status": "ok"}


@app.get("/health/ready")
def readiness():
    """
    就绪探针：判断是否可以接收新请求。
    检查所有依赖（Redis、模型加载等）是否正常。
    """
    issues = []

    # 检查 Redis
    try:
        from Agent.memory import session_memory
        client = session_memory._get_redis_client()
        if client is None:
            issues.append("Redis unavailable")
    except Exception as e:
        issues.append(f"Redis error: {e}")

    # 检查线程池
    if len(_executor._threads) >= _executor._max_workers * 1.5:
        issues.append("Thread pool overloaded")

    if issues:
        return {"status": "not ready", "issues": issues}

    return {"status": "ready"}


@app.get("/health")
def health():
    """
    综合健康检查（含详细指标）。
    """
    global _tasks_completed
    from Agent.memory.metrics import get_global_stats

    stats = get_global_stats()
    return {
        "status": "ok",
        "thread_pool": {
            "active_threads": len(_executor._threads),
            "queue_size": _executor._work_queue.qsize(),
            "max_workers": _executor._max_workers,
            "tasks_completed": _tasks_completed,
        },
        "redis_metrics": stats,
    }
```

#### Kubernetes 探针配置示例

```yaml
# deployment.yaml（了解即可，不用写）
livenessProbe:
  httpGet:
    path: /health/live
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10

readinessProbe:
  httpGet:
    path: /health/ready
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 5
```

---

### 步骤 6.4：概念讲解 —— 容量规划

#### 如何根据压测结果推算容量

```
基准测试：20 用户，P99 = 10s，QPS = 5

容量推算：
  假设 LLM 响应时间分布不变
  → 每增加 1 个并发用户，P99 延迟约增加 0.3-0.5s
  → 100 用户时，P99 ≈ 30-50s（不可接受）

扩容信号：
  当 queue_size 经常 > 0 → 需要加线程池大小
  当 P99 > 合理阈值（如 15s）→ 需要加机器或优化 LLM 延迟
```

#### 一句话总结

```
QPS 反映系统吞吐量
P99 反映用户体验（最慢的 1% 用户等多久）
错误率反映系统稳定性

三个指标要一起看，缺一不可
```

---

### 步骤 6.4：动手操作 —— 写最终优化报告

在 `docs/backend_learning_plan_phase6_report.md` 中补充：

```
## 容量规划

### 当前单机容量
| 指标 | 数值 | 评估 |
|------|------|------|
| 最大并发用户 | ~15-20 | 受线程池和 LLM 延迟限制 |
| 单机 QPS 上限 | ~3-5 | 主要瓶颈是 LLM 响应时间 |
| P99 延迟上限 | ~10-15s | LLM 生成时间决定 |

### 扩容策略
1. **垂直扩容**：增加线程池 max_workers（受 LLM 并发限制）
2. **水平扩容**：多台机器跑后端实例，前面加负载均衡（Nginx/K8s）
3. **LLM 优化**：减少 prompt token 数、使用流式输出降低感知延迟

### 下一步建议
- 添加 Prometheus + Grafana 监控（持续收集 QPS/延迟/错误率）
- 考虑使用异步 HTTP 客户端（httpx）进一步提升并发能力
- 添加缓存预热机制（系统启动时主动填充热点数据）
```

---

### 阶段六 理解测试

**问题 1**：P50 = 200ms、P90 = 500ms、P99 = 2000ms，说明什么？你会怎么排查那个最慢的 1% 请求？

<details>
<summary>参考答案</summary>

P50=200ms 说明一半请求很快，但 P99=2000ms 说明有 1% 请求异常慢。可能原因：Redis 偶尔抖动、GC 暂停、LLM API 不稳定、网络瞬时抖动。

排查方法：用 trace_id 找到那 1% 请求的完整日志，看是哪一步拖慢了。

</details>

**问题 2**：压测时 QPS 上不去，但 CPU 使用率很低，说明系统瓶颈在哪？

<details>
<summary>参考答案</summary>

CPU 低但 QPS 上不去，是典型的 IO bound 瓶颈——系统在等外部 IO（通常是 LLM API 或 Redis），CPU 没事干所以利用率低。不是加 CPU 能解决的，要增加并发请求数或优化 IO 延迟。

</details>

**问题 3**：优雅关闭的 SIGTERM 信号和普通关闭有什么区别？你的项目现在支持优雅关闭吗？

<details>
<summary>参考答案</summary>

SIGTERM 是"请求退出"信号，进程收到后停止接收新请求、处理完当前请求才退出。SIGKILL（kill -9）是直接杀死，不等请求处理完。

FastAPI + Uvicorn 默认支持 SIGTERM 优雅关闭。阶段四的 Worker 用 `lifespan` 管理，收到退出信号也会等队列处理完再停。

</details>

**问题 4**：你现在的 `/health` 端点，为什么不应该在 liveness 探针里检查 Redis？

<details>
<summary>参考答案</summary>

Liveness 探针只检查"进程还活着吗"，如果把 Redis 检查放进去：Redis 挂了 → `/health/live` 返回失败 → K8s 认为进程死了 → 重启 Pod → 实际上进程没问题，只是 Redis 挂了，重启也解决不了，反而造成更大故障。

Redis 挂了应该影响 Readiness（不接收新请求），不应该影响 Liveness（进程还活着）。

</details>

---

### 进度追踪

### 阶段六：压测 + 稳定性
- [ ] 6.0 理解性能指标（QPS/P50/P90/P99/错误率）
- [ ] 6.1.1 安装 Locust
- [ ] 6.1.2 编写 `locustfile.py` 压测脚本
- [ ] 6.1.3 运行基线压测（20 用户，120 秒）
- [ ] 6.1.4 解读压测报告
- [ ] 6.2.1 记录 `/health` 线程池状态
- [ ] 6.2.2 分析瓶颈类型（IO bound / CPU bound）
- [ ] 6.2.3 写压测优化报告到 `docs/backend_learning_plan_phase6_report.md`
- [ ] 6.3.1 验证优雅关闭（SIGTERM vs SIGKILL）
- [ ] 6.3.2 区分 liveness 和 readiness 探针
- [ ] 6.3.3 改造 `/health/live` 和 `/health/ready`
- [ ] 6.4.1 容量规划：根据压测结果推算单机上限
- [ ] 6.4.2 写最终优化报告
- [ ] 阶段六理解测试（4 题）

---

## 进度追踪

### 阶段一：并发 + Redis
- [x] 1.0 阅读 `session_memory.py` + 回答 3 个问题
- [x] 1.1.1 在 `config.py` 中添加 Redis 配置
- [x] 1.1.2 改造 `session_memory.py` 添加连接池
- [x] 1.1.3 验证连接池生效
- [x] 1.2.1 在 `config.py` 中添加线程池配置
- [x] 1.2.2 在 `main.py` 中创建全局线程池
- [x] 1.2.3 改造 `/chat/stream` 端点
- [x] 1.2.4 改造 `/health` 端点
- [x] 1.2.5 验证线程池监控
- [x] 1.3.1 新建 `Agent/memory/metrics.py`
- [x] 1.3.2 在 5 个函数中接入计时
- [x] 1.3.3 在 `/health` 中暴露指标
- [x] 1.3.4 验证慢查询日志
- [x] 阶段一理解测试（4 题）

### 阶段二：缓存体系
- [x] 2.0 阅读 `retriever.py` + `guidance.py` + 回答教练 2 个问题
- [x] 2.1.1 新建 `Agent/cache.py`（RetrievalCache 类）
- [x] 2.1.2 接入 `FitnessGuideRetrieverImpl.retrieve()`
- [x] 2.1.3 在 `/health` 暴露缓存命中率
- [x] 2.1.4 验证检索缓存生效
- [x] 2.2.1 添加 `FoodCache` 类
- [x] 2.2.2 接入 `FoodDataSearcher.search()`
- [x] 2.2.3 验证食物缓存 + 空值缓存
- [x] 阶段二理解测试（3 题）

### 阶段三：数据库
- [ ] 3.0 阅读 `long_memory.py` + 回答教练 2 个问题
- [ ] 3.1 新建 `Agent/db.py` + 表结构初始化
- [ ] 3.2 改造 `long_memory.py` 读写逻辑接入 SQLite
- [ ] 3.3 验证数据库读写 + SQL 查询
- [ ] 3.4 SQL 实战练习（JOIN、GROUP BY、EXPLAIN）
- [ ] 3.5 聊天日志持久化接入
- [ ] 阶段三理解测试（4 题）

### 阶段四：微服务 + 消息队列
- [ ] 4.0 阅读关键代码 + 回答教练问题
- [ ] 4.1 用 Redis List 实现任务队列
- [ ] 4.2 请求幂等
- [ ] 4.3 Session 级别限流
- [ ] 阶段四理解测试

### 阶段五：分布式进阶
- [x] 5.0 阅读关键代码 + 回答教练 2 个问题
- [x] 5.1.1 理解分布式锁概念
- [x] 5.1.2 新建 `Agent/lock.py`
- [x] 5.1.3 用分布式锁修复缓存击穿（改造 `cache.py` + `retriever.py`）
- [x] 5.1.4 验证锁的互斥 + 防误删
- [x] 5.2.1 在 AgentState 加 trace_id
- [x] 5.2.2 各 node 加 trace 日志
- [x] 5.2.3 workflow 出口打印总耗时
- [x] 5.2.4 API 响应透传 trace_id
- [x] 5.2.5 验证链路追踪
- [x] 5.3 分库分表设计文档（新建 `docs/backend_learning_plan_phase5_design.md`）
- [x] 阶段五理解测试（4 题）

### 阶段六：压测 + 稳定性
- [ ] 6.1 Locust 压测脚本 + 执行 + 报告
- [ ] 6.2 优化迭代 + 优化报告
- [ ] 6.3 优雅关闭 + 健康检查
- [ ] 阶段六理解测试

---

## 学习规范

1. **先理解再动手**：每个步骤开头都有"概念讲解"和"当前代码的问题"，读完再开始写代码
2. **照着代码敲，不要复制粘贴**：手敲的过程是在建立肌肉记忆
3. **每步都要验证**：每个"验证方法"环节都要跑一遍，确认输出符合预期再进下一步
4. **小步提交**：每完成一个子步骤，执行 `git add -A && git commit -m "阶段X.X: XXX"`，出问题可以精确回滚
5. **自己做测试题**：做完再翻参考答案，不要先看答案
