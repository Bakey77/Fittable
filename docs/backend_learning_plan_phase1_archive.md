# 阶段一存档：并发编程 + Redis 连接池

> 此文件为阶段一的完整存档，包含你的学习笔记。已完成于 2026-05-03 ~ 2026-05-04。
> 进度追踪已迁移到主文档。

（以下为原文，一字未改）
## 阶段一：并发编程 + Redis 连接池

### 覆盖知识点

- Python GIL 与多线程（IO 密集型 vs CPU 密集型）
- `threading.Lock` 使用场景与死锁
- `ThreadPoolExecutor` 线程池原理
- Redis 五大数据结构：String、Hash、List、Set、Sorted Set
- Redis 连接池原理
- Lua 脚本在 Redis 中的原子性

---

### 步骤 1.0：阅读 `session_memory.py` 并回答问题

**文件**：`Agent/memory/session_memory.py`

重点看 `_get_redis_client()` 函数（第 165-199 行），然后回答以下 3 个问题。

**问题 1**：现在 `_get_redis_client()` 在哪些情况下会返回 `None`？

<details>
<summary>提问目的（先自己想答案，再看这个）</summary>

这道题考察你**对代码分支的理解**。一个函数返回 None 意味着调用方必须有兜底逻辑——你后续会看到 session_memory.py 中所有调用 `_get_redis_client()` 的地方都有一个 `if client is None:` 的分支。理解"什么情况下返回 None"等于理解了这个模块的故障降级机制。

</details>

<details>
<summary>参考答案</summary>

两种情况：
1. `redis` 包未安装（`import redis` 失败，`redis is None`）
2. Redis 服务连不上：`client.ping()` 抛出 `ConnectionError`、`TimeoutError` 或 `RedisError`

注意：**每次调用 `_get_redis_client()` 都新建连接**，所以即使上一次 ping 通了，下一次也可能因为网络抖动返回 None。

</details>

**问题 2**：如果第一次调用时 Redis 正常，第二次调用时 Redis 挂了，锁住在 fallback dict 里的数据会怎样？

<details>
<summary>提问目的</summary>

这道题考察你**对状态迁移的理解**。分布式系统中"部分故障"是最常见的场景——依赖服务时好时坏时，系统行为是什么？`_redis_was_down` 和 `_fallback_store` 两个模块级变量的配合使用是典型的降级方案，理解它有助于你后续自己做类似设计。

</details>

<details>
<summary>参考答案</summary>

- 第二次调用时 `client.ping()` 失败 → 返回 None → 所有后续操作走 `_fallback_store`（内存字典），数据不会丢失
- `_redis_was_down` 被设为 True
- 数据存在进程内存中，**如果进程重启则丢失**

</details>

**问题 3**：`_redis_was_down` 变量是干什么用的？`_fallback_store` 里的数据什么时候被同步回 Redis？

<details>
<summary>提问目的</summary>

这道题考察你**对恢复机制的理解**。降级只是第一步，更重要的是——故障恢复后如何自动切回去？注意看代码 178-184 行的逻辑，这是一个典型的"自动恢复"模式。

</details>

<details>
<summary>参考答案</summary>

`_redis_was_down` 是一个"脏标记"（dirty flag），表示 Redis 曾经挂过。

同步回 Redis 的时机：当某次 `_get_redis_client()` 调用**成功** ping 通 Redis **且** `_redis_was_down == True` 时（第 179 行），会把 `_fallback_store` 中所有 session 的数据全量写入 Redis，然后清空 `_fallback_store`，重置 `_redis_was_down = False`。

**潜在问题**：如果 `_fallback_store` 有 5000 个 session，恢复时会**一次性**同步全部，可能阻塞当前请求。这是后续可以优化的点。

</details>

---

### 步骤 1.1：概念讲解 —— 为什么需要 Redis 连接池？

#### 当前代码的问题

打开 `session_memory.py` 第 171 行，看这一行：

```python
client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
```

每次调用 `_get_redis_client()` 都执行这行代码。每次执行时：

```
你的代码                        操作系统做的事
─────────────────────────────────────────────
redis.Redis(...)  ──→  创建 socket 文件描述符
                  ──→  TCP 三次握手 (约 1-2ms)
                  ──→  发送 Redis AUTH 命令（如有密码）
                  ──→  连接就绪

... 用完这个 client 后 ...

client 被 GC 回收    ──→  TCP 四次挥手
                  ──→  释放 socket 文件描述符
```

**后果**：100 个并发请求 = 每次请求都新建连接 = 100 次 TCP 握手 + 100 次挥手。不仅慢，还可能耗尽操作系统的临时端口。

#### 连接池怎么解决？

```
应用启动时：
  预创建 10 个 TCP 连接，放进"池子"里，全部保持连接（不断开）

请求1 来：
  从池子里取出连接 A → 执行 SET/GET → 放回池子（不挥手）
请求2 来：
  从池子里取出连接 B → 执行 SET/GET → 放回池子（不挥手）
...
请求10 来：
  从池子里取出连接 J → 执行中...

请求11 来（池子空了）：
  → 等待，直到有人归还连接
  → 或者：抛异常（取决于配置）
```

核心参数说明：

| 参数 | 含义 | 建议值 |
|------|------|--------|
| `max_connections` | 池子里最多放几个连接 | 10-20 |
| `socket_timeout` | 单次 Redis 操作最多等多久 | 5 秒 |
| `socket_connect_timeout` | TCP 握手最多等多久 | 2 秒 |
| `retry_on_timeout` | 超时后是否自动重试 | True |
| `health_check_interval` | 多久检查闲置连接是否还活着 | 30 秒 |

#### 改了之后的效果

```
改之前：100 个请求 = 100 次新建连接
改之后：100 个请求 = 复用 10 个长连接
```

---

### 步骤 1.1：动手操作

#### 第 1 小步：在 `config.py` 中添加 Redis 配置

打开 `config.py`，在文件末尾添加以下内容：

```python
    # Redis 配置
    REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
    REDIS_DB = int(os.getenv("REDIS_DB", "0"))
    REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)
    REDIS_MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS", "10"))
    REDIS_SOCKET_TIMEOUT = int(os.getenv("REDIS_SOCKET_TIMEOUT", "5"))
```

**每个字段的含义**：

| 配置项 | 含义 | 默认值 | 为什么这个默认值 |
|--------|------|--------|----------------|
| `REDIS_HOST` | Redis 服务器的 IP/域名 | localhost | 你和 Redis 在同一台机器 |
| `REDIS_PORT` | Redis 端口 | 6379 | Redis 默认端口 |
| `REDIS_DB` | 使用哪个数据库编号 | 0 | 默认第一个库够用 |
| `REDIS_PASSWORD` | 连接密码 | None | 本地开发通常没密码 |
| `REDIS_MAX_CONNECTIONS` | 连接池最大连接数 | 10 | 你目前是单机开发，10 个够了 |
| `REDIS_SOCKET_TIMEOUT` | 操作超时秒数 | 5 | 超过 5 秒没响应就该报错了 |

#### 第 2 小步：改造 `session_memory.py` 中的 `_get_redis_client()`

打开 `Agent/memory/session_memory.py`，找到 `_get_redis_client()` 函数（约第 165 行），做以下改动：

**改动 A**：在文件顶部（`_fallback_lock = threading.Lock()` 那行下面）添加连接池单例变量：

```python
# 在 _fallback_lock 那行下面添加：
_pool = None  # Redis 连接池单例
```

**改动 B**：新增一个 `_get_pool()` 函数（插入在 `_get_redis_client()` 之前）：

```python
def _get_pool():
    """返回 Redis 连接池单例。池是线程安全的，多线程可共享。"""
    global _pool
    if _pool is not None:
        return _pool
    if redis is None:
        return None
_pool = redis.ConnectionPool(
        host="localhost",
        port=6379,
        db=0,
        decode_responses=True,
        max_connections=10,
        socket_timeout=5,
        socket_connect_timeout=2,
        retry_on_timeout=True,
        health_check_interval=30,
    )
    return _pool
```

**等一下**——这个写法有问题。`host`、`port` 等值是硬编码的，跟原来一样。我们应该从 `config.py` 读。正确的写法是：

```python
def _get_pool():
    """返回 Redis 连接池单例。池是线程安全的，多线程可共享。"""
    global _pool
    if _pool is not None:
        return _pool
    if redis is None:
        return None

    from config import Config
    _pool = redis.ConnectionPool(
        host=Config.REDIS_HOST,
        port=Config.REDIS_PORT,
        db=Config.REDIS_DB,
        password=Config.REDIS_PASSWORD or None,
        decode_responses=True,
        max_connections=Config.REDIS_MAX_CONNECTIONS,
        socket_timeout=Config.REDIS_SOCKET_TIMEOUT,
        socket_connect_timeout=2,
        retry_on_timeout=True,
        health_check_interval=30,
    )
    return _pool
```

**改动 C**：替换 `_get_redis_client()` 函数体。找到函数中这一行：

```python
client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
```

改成：

```python
pool = _get_pool()
    if pool is None:
        return None
    client = redis.Redis(connection_pool=pool)
```

完整的 `_get_redis_client()` 新版本如下（你可以对照原来的代码逐行替换）：

```python
def _get_redis_client():
    global _redis_client, _redis_was_down, _registered_scripts

    if redis is None:
        return None

    pool = _get_pool()
    if pool is None:
        return None

    client = redis.Redis(connection_pool=pool)

    try:
        client.ping()
    except (redis.ConnectionError, redis.TimeoutError, redis.RedisError):
        _redis_was_down = True
        return None

    # Redis 恢复了！把 _fallback_store 同步回去
    if _redis_was_down and _fallback_store:
        for session_id, memory in _fallback_store.items():
            key = _key(session_id)
            client.setex(key, _SESSION_TTL, json.dumps(memory))
        _fallback_store.clear()
        _redis_was_down = False

    _redis_client = client

    # 注册 Lua 脚本
    scripts = {
        "append_turn": _LUA_APPEND_TURN,
        "update_working_memory": _LUA_UPDATE_WORKING_MEMORY,
        "trim": _LUA_TRIM,
        "update_metadata": _LUA_UPDATE_METADATA,
    }
    for name, script in scripts.items():
        if name not in _registered_scripts:
            _registered_scripts[name] = client.script_load(script)

    return client
```

**关键变化说明**：

| 旧代码 | 新代码 | 为什么 |
|--------|--------|--------|
| `redis.Redis(host="localhost", ...)` | `redis.Redis(connection_pool=pool)` | 不再每次创建新连接，从池里借 |
| 每次新建，用完即弃 | 池里 10 个连接循环复用 | 省去了 TCP 握手/挥手的开销 |
| host/port/db 硬编码 | 从 `config.py` 读 | 不同环境可以配置不同值 |

#### 验证方法

**验证 1**：确认代码能加载不报错。

```bash
cd /Users/liaomeiqi/Downloads/Fit-Agent
python -c "from Agent.memory.session_memory import _get_redis_client; print('OK')"
```

预期输出：
```
OK
```

如果报错，检查：
- 是不是少打了冒号或缩进不对？
- `from config import Config` 的 import 路径是否写对了？

**验证 2**：确认连接池真的生效了。

```bash
python -c "
from Agent.memory.session_memory import _get_pool, _get_redis_client
pool = _get_pool()
print(f'连接池类型: {type(pool).__name__}')
print(f'最大连接数: {pool.max_connections}')
client = _get_redis_client()
if client:
    print(f'client 连接池: id={id(client.connection_pool)}')
    print('连接正常')
else:
    print('Redis 未连接（如果没启动 Redis，这是正常的）')
"
```

预期输出（Redis 启动时）：
```
连接池类型: ConnectionPool
最大连接数: 10
client 连接池: id=4371234567
连接正常
```

**验证 3**（加分项）：开启 Redis 日志观察连接数。

另开一个终端：
```bash
redis-cli
> CLIENT LIST
```

在你跑上面 Python 脚本的瞬间执行 `CLIENT LIST`，看看列出的连接数。然后用旧代码（不改之前）和你改之后的代码分别观察。

**预期**：
- 旧代码：连续跑 3 次脚本，`CLIENT LIST` 会短暂地增加 1 个连接，连接很快释放
- 新代码：跑第 1 次脚本时 `CLIENT LIST` 增加 1 个连接，第 2、3 次不增加（复用池里的），脚本退出后连接保持

---

### 步骤 1.2：概念讲解 —— 为什么要监控线程池？

#### 当前代码的问题

打开 `backend/main.py` 第 361 行：

```python
result = await loop.run_in_executor(ThreadPoolExecutor(), run_sync_workflow)
```

注意看：**`ThreadPoolExecutor()` 没有参数，而且每次请求都 new 一个新的！**

`ThreadPoolExecutor()` 没有参数时，Python 3.8+ 默认 `max_workers = min(32, os.cpu_count() + 4)`，等于说你每次请求都新建一个有几十个线程的池，用完就扔。你想观察这个池的状态（有几个线程在忙、队列塞了多少任务）完全没法做。

**知识点——线程池内部结构**：

```
ThreadPoolExecutor
├── _max_workers: 最多能创建多少线程
├── _threads: set()            ← 当前活着的线程对象集合
├── _work_queue: SimpleQueue   ← 任务队列
│   └── .qsize()               ← 队列中等待执行的任务数
└── _shutdown: bool            ← 是否正在关闭
```

你要监控的就是这些字段。

**另一个知识点——为什么 FastAPI + ThreadPoolExecutor 能提升并发？**

Python 有 GIL（全局解释器锁），同一时刻只有一个线程在跑 Python 代码。但 FastAPI 中用线程池仍然有效，原因是：

```
请求处理过程：
  ├── 10% 在跑 Python 代码（持有 GIL）    ← 真正受 GIL 限制
  ├── 30% 在等 Redis 返回（IO 等待）      ← 不持有 GIL，可以被其他线程用
  └── 60% 在等 LLM API 返回（网络 IO）    ← 不持有 GIL

关键：Python 在做 IO 操作时会释放 GIL！
```

所以当线程 A 在等 LLM 返回（10 秒），线程 B 可以获取 GIL 处理另一个请求。这就是为什么 IO 密集型应用中多线程有效。

---

### 步骤 1.2：动手操作

#### 第 1 小步：在 `config.py` 中添加线程池配置

打开 `config.py`，在 Redis 配置后面添加：

```python
    # 线程池配置
    THREAD_POOL_MAX_WORKERS = int(os.getenv("THREAD_POOL_MAX_WORKERS", "10"))
```

> **为什么默认 10？** 你的瓶颈是外部 IO（LLM API 调用耗时 5-15 秒），不是 CPU。线程数设太大反而增加上下文切换开销。10 个线程意味着最多同时处理 10 个请求，第 11 个排队。

#### 第 2 小步：在 `backend/main.py` 中创建全局线程池和计数器

打开 `backend/main.py`，找到文件最上面的 import 区域（约第 1-23 行），在现有 import 后面添加：

```python
import threading
```

然后找到 CORS 中间件配置的代码块（约第 41-47 行），在它**下面**添加全局线程池和计数器：

```python
# ---------------------------------------------------------------------------
# 全局线程池（全模块共享，避免每次请求新建）
# ---------------------------------------------------------------------------
from config import Config

_executor = ThreadPoolExecutor(
    max_workers=Config.THREAD_POOL_MAX_WORKERS,
    thread_name_prefix="fit_agent_worker",
)
_tasks_completed = 0
_tasks_lock = threading.Lock()
```

> **为什么要 `threading.Lock` 保护 `_tasks_completed`？**
> `_tasks_completed += 1` 看起来是一行代码，但 Python 底层是三步：读 → 加 → 写。两个线程同时执行时可能读到同一个值，导致计数少 1。加锁确保这三步串行执行。

#### 第 3 小步：改造 `/chat/stream` 端点

找到原来这一行（约第 361 行）：

```python
result = await loop.run_in_executor(ThreadPoolExecutor(), run_sync_workflow)
```

改成：

```python
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, run_sync_workflow)

    # 累计完成任务计数（线程安全）
    global _tasks_completed
    with _tasks_lock:
        _tasks_completed += 1
```

> **注意**：`_tasks_completed += 1` 必须在 `with _tasks_lock:` 里面，否则计数不准。这是学习锁的最简单实践场景。

#### 第 4 小步：改造 `/health` 端点

找到原来 `/health` 端点（约第 279-282 行）：

```python
@app.get("/health")
def health():
    """健康检查"""
    return {"status": "ok"}
```

替换为：

```python
@app.get("/health")
def health():
    """健康检查（含线程池状态）"""
    global _tasks_completed
    return {
        "status": "ok",
        "thread_pool": {
            "active_threads": len(_executor._threads),
            "queue_size": _executor._work_queue.qsize(),
            "max_workers": _executor._max_workers,
            "tasks_completed": _tasks_completed,
        },
    }
```

> **带 `_` 前缀的字段（如 `_threads`）是私有字段**，正常不应该外部访问。但在监控/调试场景，直接读是常见的务实做法。

#### 验证方法

**验证 1**：启动后端，访问 `/health`。

```bash
cd /Users/liaomeiqi/Downloads/Fit-Agent
python -m backend.main
```

另开终端：

```bash
curl http://localhost:8000/health | python -m json.tool
```

预期输出类似：
```json
{
    "status": "ok",
    "thread_pool": {
        "active_threads": 0,
        "queue_size": 0,
        "max_workers": 10,
        "tasks_completed": 0
    }
}
```

**验证 2**：发一个聊天请求，立即再查 `/health`。

```bash
# 终端1：发请求
curl -X POST http://localhost:8000/chat -H 'Content-Type: application/json' -d '{"message": "你好"}'

# 终端2：在请求处理期间立即查 health（多刷新几次）
curl http://localhost:8000/health | python -m json.tool
```

预期：`tasks_completed` 从 0 变成 1。如果在请求处理期间刷新，你会看到 `active_threads` 至少为 1。

**验证 3**：理解线程池排队的场景。

如果你能模拟并发请求（用 `ab` 或 `siege`），同时发 15 个请求，观察 `queue_size` 是否大于 0（第 11-15 个请求在排队）。

#### 补充知识点：`_threads` 的真正含义

`_executor._threads` 是一个 `set`，包含了线程池创建的**所有**线程对象（包括空闲的和正在干活的）。Python 的 `ThreadPoolExecutor` 不会主动销毁线程，所以 `len(_executor._threads)` 最多等于 `max_workers`，且只会增长不会减少（除非线程异常退出）。

这不是"当前正在工作的线程数"，而是"历史上创建过的线程数"。如果你要精确知道"正在工作的"，需要更复杂的实现。但对学习项目来说，`len(_executor._threads)` 已经够用了。

---

### 步骤 1.3：概念讲解 —— 操作计数与慢查询

#### 为什么需要这个？

你现在的代码用 Redis 存 session、用 Redis 做 Lua 原子操作，但你完全不知道：

- 每个请求调了几次 Redis？
- 哪次调用特别慢（可能是网络抖动）？
- 一天下来 Redis 总共被调了多少次？

加入操作计时和慢查询日志后，当某天系统变慢，你可以立刻在日志中看到：

```
WARNING  Slow Redis: trim_and_summarize took 230ms (session_id=abc123)
WARNING  Slow Redis: append_turn took 180ms (session_id=def456)
```

立刻就知道是 Redis 出了性能问题，而不是 LLM 或别的什么。

---

### 步骤 1.3：动手操作

#### 第 1 小步：新建 `Agent/memory/metrics.py`

创建新文件 `Agent/memory/metrics.py`，写入以下内容：

```python
"""
Redis 操作指标收集模块。
记录每次 Redis 操作的耗时，慢查询告警，并提供全局统计。
"""
import time
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 慢查询阈值（毫秒）
SLOW_QUERY_THRESHOLD_MS = 50

# 全局计数器（进程内存，非线程安全，仅用于大致统计）
_global_stats = {
    "total_ops": 0,
    "slow_queries": 0,
    "recent_slow": [],  # 最近 20 条慢查询记录
}


def record_redis_op(
    session_id: str,
    op_name: str,
    elapsed_ms: float,
    client: Optional[object] = None,
) -> None:
    """
    记录一次 Redis 操作的指标。

    参数:
        session_id: 会话 ID
        op_name: 操作名称（如 "append_turn", "trim_and_summarize"）
        elapsed_ms: 耗时（毫秒）
        client: Redis 客户端（可选，有则写入 Hash 统计）
    """
    global _global_stats
    _global_stats["total_ops"] += 1

    # 慢查询告警
    if elapsed_ms > SLOW_QUERY_THRESHOLD_MS:
        _global_stats["slow_queries"] += 1
        _global_stats["recent_slow"].append({
            "session_id": session_id,
            "op": op_name,
            "elapsed_ms": round(elapsed_ms, 1),
        })
        # 只保留最近 20 条
        if len(_global_stats["recent_slow"]) > 20:
            _global_stats["recent_slow"] = _global_stats["recent_slow"][-20:]
        logger.warning(
            f"Slow Redis: {op_name} took {elapsed_ms:.1f}ms (session={session_id[:8]}...)"
        )

    # 如果有 Redis 客户端，写入会话级统计（Hash）
    if client is not None:
        try:
            stats_key = f"fit_agent:session:stats:{session_id}"
            client.hincrby(stats_key, "total_ops", 1)
            client.hincrby(stats_key, f"op:{op_name}", 1)
            client.hset(stats_key, "last_access", time.strftime("%Y-%m-%dT%H:%M:%S"))
            client.expire(stats_key, 30 * 24 * 60 * 60)  # 30 天 TTL
        except Exception:
            # 统计写入失败不应影响主流程
            pass


def get_global_stats() -> dict:
    """返回全局统计信息。"""
    return {
        "total_redis_ops": _global_stats["total_ops"],
        "slow_queries": _global_stats["slow_queries"],
        "recent_slow_queries": _global_stats["recent_slow"][-10:],
    }
```

> **关键设计决策解释**：
>
> 1. **`try/except Exception: pass`** — 统计写入失败绝不能抛异常阻塞主流程。这是"非关键路径"原则。
> 2. **`client` 是可选的** — Redis 挂了时，我们也想统计 fallback 操作的耗时，但不需要写入 Redis Hash。
> 3. **`_global_stats` 不用锁** — 因为 `+= 1` 在 CPython 中对于小整数通常是原子的（GIL 保护），且这是一个大致统计，不需要精确到个位数。

#### 第 2 小步：在 `session_memory.py` 的关键函数中接入计时

打开 `Agent/memory/session_memory.py`，在文件顶部 import 区域添加：

```python
import time
```

然后找到 `from config import Config` 这行（在 `_get_pool()` 函数里已经有了），确保 metrics 能导入。在 `_get_redis_client` 函数之前或之后添加 import：

```python
from Agent.memory.metrics import record_redis_op
```

接下来，在每个关键 Redis 操作函数的**调用 Redis 的地方**加计时。以下是 5 个需要改造的函数：

**函数 1：`append_turn`（约第 384 行）**

在函数体中找到 `client.evalsha(...)` 这一行（约第 399 行），在它**之前**加 `start = time.monotonic()`，在它**之后**加计时调用。完整改动后如下：

```python
def append_turn(session_id: str, role: str, text: str) -> None:
    """追加一轮对话到 recent_turns。role = "user" | "assistant"。原子操作。"""
    client = _get_redis_client()
    start = time.monotonic()

    if client is None:
        with _fallback_lock:
            memory = _fallback_store.get(session_id, None)
            if memory is None:
                memory = _empty_memory()
                _fallback_store[session_id] = memory
            memory["recent_turns"].append({"role": role, "text": text})
        elapsed = (time.monotonic() - start) * 1000
        record_redis_op(session_id, "append_turn_fallback", elapsed)
        return

    sha = _registered_scripts.get("append_turn")
    if sha:
        client.evalsha(sha, 1, _key(session_id), role, text, _SESSION_TTL)
    else:
        client.eval(_LUA_APPEND_TURN, 1, _key(session_id), role, text, _SESSION_TTL)

    elapsed = (time.monotonic() - start) * 1000
    record_redis_op(session_id, "append_turn", elapsed, client)
```

**注意**：fallback 路径（Redis 挂了）的 op_name 加了 `_fallback` 后缀，这样你能区分"走 Redis 的调用"和"走内存兜底的调用"。

**函数 2：`update_working_memory`（约第 405 行）**

在函数体开头加 `start = time.monotonic()`，在每个分支（fallback / evalsha）末尾加计时。op_name 用 `"update_working_memory"` / `"update_working_memory_fallback"`。

**函数 3：`trim_and_summarize`（约第 462 行）**

同样加计时。op_name 用 `"trim_and_summarize"` / `"trim_and_summarize_fallback"`。

**函数 4：`update_metadata`（约第 484 行）**

同样加计时。op_name 用 `"update_metadata"` / `"update_metadata_fallback"`。

**函数 5：`get_session_memory`（约第 379 行）**

注意：`get_session_memory` 调的是 `_get_memory_impl`，不是直接调 `_get_redis_client`。你需要进到 `_get_memory_impl` 函数里面加计时，因为读写操作实际发生在那里。

#### 第 3 小步：在 `/health` 中暴露指标

打开 `backend/main.py`，找到 `/health` 端点，在返回结果中加上 metrics 数据。

先加 import（在文件顶部）：

```python
from Agent.memory.metrics import get_global_stats
```

然后改造 `/health`：

```python
@app.get("/health")
def health():
    """健康检查（含线程池状态 + Redis 操作指标）"""
    global _tasks_completed
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

#### 验证方法

**验证 1**：确保不报错。

```bash
cd /Users/liaomeiqi/Downloads/Fit-Agent
python -c "from Agent.memory.metrics import record_redis_op, get_global_stats; print('OK')"
```

**验证 2**：发送聊天请求，查看慢查询日志。

```bash
# 终端1：启动服务
python -m backend.main

# 终端2：发送请求
curl -X POST http://localhost:8000/chat -H 'Content-Type: application/json' -d '{"message": "你好"}'
```

观察终端 1 的日志输出。正常情况下 Redis 操作很快（< 1ms），你不会看到 WARNING。如果出现 WARNING，说明你的 Redis 有延迟。

**验证 3**：手动触发慢查询模拟。

如果你有 redis-cli，可以先给 Redis 加一点人为延迟（仅测试用）：

```bash
redis-cli
> DEBUG SLEEP 0.1
```

然后发请求，你会在服务端日志中看到 `Slow Redis` 的 WARNING。

**验证 4**：查看指标。

```bash
curl http://localhost:8000/health | python -m json.tool
```

预期看到 `redis_metrics` 字段：
```json
{
    "redis_metrics": {
        "total_redis_ops": 5,
        "slow_queries": 0,
        "recent_slow_queries": []
    }
}
```

---

### 阶段一 理解测试

完成以上 3 个步骤并验证通过后，回答以下问题。每题都标注了**提问目的**。

**问题 1**：Python 有 GIL，为什么 `ThreadPoolExecutor` 在 FastAPI 中仍然能提升并发性能？
我：GIL只限制CPU计算，IO操作执行时，python内部会主动释放GIL，让其他线程有机会执行。
FastAPI的请求处理大部分时间都在等LLM返回、redis响应。GIL等待没有意义，因此可以提高并发
<details>
<summary>提问目的</summary>

这道题检验你是否理解了"GIL 影响什么、不影响什么"。很多 Python 初学者知道 GIL 的存在，但不知道 GIL 在 IO 等待时会被释放。理解这一点是你后续做任何并发优化的基础——你会知道什么时候应该用多线程（IO 密集型），什么时候不该用（CPU 密集型）。

</details>

**问题 2**：如果 Redis 连接池 `max_connections=10`，同时来 20 个请求，第 11-20 个请求会发生什么？你有几种处理方式？
我：：池子设到合理大小 + 设一个短 timeout + 最后的兜底逻辑。
<details>
<summary>提问目的</summary>

这道题检验你是否理解"资源耗尽"场景。这是后端开发中最常见的线上问题之一——所有池（连接池、线程池）都有上限，当请求超过上限时，系统的行为设计决定了是"优雅降级"还是"雪崩"。你的回答应该包含具体的处理策略。

</details>

**问题 3**：在 `session_memory.py` 中，如果 `_redis_was_down = True` 且 `_fallback_store` 里有 5000 个 session，当 Redis 恢复后，`_get_redis_client()` 会一次性把这 5000 个 session 同步回 Redis。这会有什么问题？你会怎么优化？（不需要改代码，说思路即可）

<details>
<summary>提问目的</summary>

这是典型的"恢复风暴"问题。检验你是否能从"功能正确（数据没丢）"升级到"性能正确（恢复不卡顿）"。你的优化思路应该体现"分批处理"或"惰性同步"的思维。  分批处理，每次同步100个session或者等到用户需要时再同步。

</details>

**问题 4**（项目实操题）：用你改造后的代码，记录一次 `/chat` 请求从发起到返回的过程中，`total_redis_ops` 增加了多少。增加的数量反映了什么？
我：我发送了一个你好，增加10，应该说明有十次的redis操作
一次"你好"的 10 次 Redis 操作
                                                                                                                                                                                    
  读阶段（workflow 启动前）：                               
    1. get_working_memory   → Redis GET       ← 读 pending_intent                                                                                                         
    2. get_recent_turns     → Redis GET       ← 读最近对话                                                                                                                          
    （可能还有 load_long_memory 触发的读）                                                                                                                                          
                                                                                                                                                                                    
  写阶段（workflow 完成后）：                                                                                                                                                       
    3. append_turn          → Lua EVALSHA     ← 写 user 消息                                                                                                                        
    4. append_turn          → Lua EVALSHA     ← 写 assistant 回复                                                                                                                   
    5. update_working_memory → Lua EVALSHA    ← 更新 pending 状态                                                                                                                   
    6. update_metadata      → Lua EVALSHA     ← total_turns +1                                                                                                                      
    7. trim_and_summarize   → Lua EVALSHA     ← 裁剪到 10 轮                                                                                                                        
                                                                                                                                                                                    
  workflow 内部（node 执行中）：                                                                                                                                                    
    8-10. 其他读取           ← 如 _get_memory_impl 多次调用


<details>
<summary>提问目的</summary>

这道题让你**实际动手观察**一个请求触发了多少次 Redis 操作。理解"一次请求 = N 次 Redis 调用"这个事实是后续做性能优化的基础——如果 N 很大，说明你可能有优化空间（比如合并操作）。

</details>

