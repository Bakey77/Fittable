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

## 阶段三：关系型数据库入门（SQLite）

### 覆盖知识点

- 关系型数据库 vs KV 存储 vs 向量数据库的本质区别
- SQL：SELECT、INSERT、UPDATE、DELETE、JOIN、GROUP BY
- 表设计：主键、外键、约束、范式
- 索引：B+ 树原理，何时建索引
- `EXPLAIN` 分析查询计划
- 事务 ACID：原子性、一致性、隔离性、持久性
- WAL 模式（Write-Ahead Logging）
- Python sqlite3 标准库使用

### 本阶段要改什么

当前 `long_memory.py` 把长期记忆存为 **Markdown 文件**，一个 session 一个 `.md` 文件。这种方式：
- 查数据需要解析 Markdown → 效率低
- 不能做复杂查询（"找出所有目标是增肌的用户"做不到）
- 并发写入可能冲突（虽然有 `tmp + replace` 原子写入，但无法做行级锁）
- 数据一致性难以保证（Markdown 解析/渲染容易出错）

改造成 **SQLite** 后：结构化存储、SQL 查询、事务保障。

---

### 步骤 3.0：阅读关键代码并回答问题

打开 `Agent/memory/long_memory.py`，重点看以下函数：
- `load_long_memory()`（第 76 行）
- `save_long_memory()`（第 93 行）
- `parse_markdown_to_obj()`（第 364 行）
- `render_markdown()`（第 301 行）
- `update_long_memory()`（第 479 行）

再看一个实际的存储文件，比如 `Agent/memory/long_term_store/` 下的任意 `.md` 文件。

**问题 1**：当前长期记忆存储了什么内容？有哪些 section？如果要做 CRUD 操作（增删改查某个字段），走 Markdown 文件的话需要几步？
我：长期记忆存了用户偏好，一些冲突记录等内容。你说的section是什么意思。  如果要CRUD的话，当前应该需要读取这个sessionid的整个markdown文件，然后修改后，写回。

<details>
<summary>提问目的</summary>

让你对比"文件存储"和"数据库存储"的差异。Markdown 文件的每次读都是全文解析，每次写都是全文渲染+覆盖。而数据库可以只读/写某一个字段。通过这个问题，你能直观感受到为什么结构化数据应该用数据库。

</details>

<details>
<summary>参考答案</summary>

6 个 section：Stable Profile、Preferences、Constraints、Active Plan Facts、Conflict Log、Last Updated。每个 section 下存的是 `- key: value` 格式的行。

如果要改一个字段（比如把 age 从 24 改成 25），Markdown 文件做法：
1. 读整个文件
2. parse_markdown_to_obj() 解析全文
3. 找到 "Stable Profile" section
4. 遍历 raw_lines 找到 "- age: 24"
5. 改成 "- age: 25"
6. render_markdown() 渲染全文
7. 写回整个文件

数据库做法：`UPDATE profile SET age=25 WHERE session_id='xxx'` 一行搞定。
┌────────────┬─────────────────────────────────────┬───────────────────────────┐
  │    维度    │            Markdown 文件            │          SQLite           │
  ├────────────┼─────────────────────────────────────┼───────────────────────────┤
  │ 正确性     │ 两个请求同时更新 → 后写的覆盖先写的 │ 事务隔离，不会丢失更新    │
  ├────────────┼─────────────────────────────────────┼───────────────────────────┤
  │ 查询能力   │ 只能按 session_id 查一个文件        │ 可以跨 session 做统计查询 │
  ├────────────┼─────────────────────────────────────┼───────────────────────────┤
  │ 局部更新   │ 必须全量读→LLM→全量写               │ 可以只改一个字段          │
  ├────────────┼─────────────────────────────────────┼───────────────────────────┤
  │ 代码复杂度 │ 需要 parse + render 两个函数        │ SQL 直接操作              │
  └────────────┴─────────────────────────────────────┴───────────────────────────┘
</details>

**问题 2**：`save_long_memory()` 用了 `tmp_path + replace` 策略。这解决了什么问题？解决不了什么问题？
`tmp_path + replace`好像是把当前用户长期记忆的mardown文件的快照读出来，然后修改后写入。并且保证了这个过程是原子性一次完成的，避免并发写入的冲突❌，是这样吗？
<details>
<summary>提问目的</summary>

考察你对"原子写入"的理解。tmp + replace 保证了写文件时不会出现半成品（要么全是旧的，要么全是新的），但它在并发场景下有局限——两个线程同时写同一个文件时，后写的会覆盖先写的。这引出了数据库事务的必要性。

</details>

<details>
<summary>参考答案</summary>

**解决了**：写入过程中进程崩溃不会损坏文件（tmp 文件损坏没关系，原文件还在）。读者不会读到写完一半的内容（replace 是原子操作，瞬间切换）。

**解决不了**：两个请求同时更新同一个 session 的文件时，后完成的会覆盖先完成的，先完成的更新丢失。这叫"写冲突"或"丢失更新"（Lost Update），需要数据库的行级锁或乐观锁来解决。

</details>

<details>
<summary>教学笔记：原子写入 vs 数据库事务</summary>

`tmp + replace` 只保证**单次写入**的原子性，不保证**多次写入之间**的隔离性。

打个比方：
- 原子写入 = 你一个人在黑板写字，写完了翻转黑板，别人要么看到旧的要么看到新的
- 数据库事务 = 黑板上有锁，你写的时候别人不能写，写完解锁后别人才能看到你的内容

当系统只有一个用户时，原子写入够用。多用户并发时，需要数据库的事务隔离。

</details>

---

### 步骤 3.1：概念讲解 —— 三种存储的本质区别

你现在项目里用了三种存储，搞清各自的定位：

| 存储类型 | 代表 | 存什么 | 怎么查 | 适合场景 |
|----------|------|--------|--------|---------|
| **KV 存储** | Redis | key → value | 按 key 精确查 | 缓存、session、临时状态 |
| **向量数据库** | Qdrant | 向量 + metadata | 按相似度查 | 语义搜索、RAG |
| **关系型数据库** | SQLite | 表（行 + 列） | SQL 任意条件查 | 持久化结构化数据 |

一句话区分：
- Redis："请给我 session_id=abc 的数据"（一把钥匙开一把锁）
- Qdrant："请给我和'深蹲'最相似的 10 个文档"（模糊匹配）
- SQLite："请给我所有目标是增肌、年龄在 20-30 岁的用户"（多条件组合查询）

**为什么长期记忆应该用关系型数据库？**

长期记忆是结构化数据（用户档案、偏好、约束），查询模式是"按 session_id 读全量"或"按某个字段过滤"。这正是关系型数据库最擅长的。

---

### 步骤 3.2：概念讲解 —— SQLite 基础

SQLite 是一个**嵌入式数据库**，不需要单独安装服务端，数据库就是一个 `.db` 文件。Python 标准库自带 `sqlite3` 模块，零依赖。

**核心概念**：

- **数据库（Database）**：一个 `.db` 文件，包含多个表
- **表（Table）**：类似 Excel 的一个 sheet，有固定的列定义
- **行（Row）**：一条记录
- **列（Column）**：一个字段，有类型（INTEGER、TEXT、REAL、BLOB）
- **主键（Primary Key）**：唯一标识一行，不能重复
- **外键（Foreign Key）**：引用另一个表的主键

**基本 SQL**（本阶段实战用）：

```sql
-- 创建表
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);

-- 插入数据
INSERT INTO users (name, age) VALUES ('小小', 24);

-- 查询数据
SELECT * FROM users WHERE age > 20;
SELECT name, age FROM users ORDER BY age DESC;

-- 更新数据
UPDATE users SET age = 25 WHERE name = '小小';

-- 删除数据
DELETE FROM users WHERE name = '小小';
```

**WAL 模式是什么？**

你在 `db.py` 里看到一行 `PRAGMA journal_mode=WAL`，这里解释它干了什么。

SQLite 写数据时默认用 **rollback journal**（回滚日志）：写之前先把旧页复制到 journal 文件，写完后再删 journal。这个过程：
- **写操作会阻塞读操作**（读写互斥）
- 如果写一半崩溃了，journal 文件还在，下次打开数据库时自动回滚恢复

**WAL（Write-Ahead Logging）** 改为"先写日志再写数据"：

```
写请求 → 追加写入 WAL 文件（append only，很快）
       → 后台异步把 WAL 刷回主数据文件
读请求 → 先查主数据文件，再去 WAL 找最新版本（读写不互斥）
```

| 对比 | Rollback Journal | WAL |
|------|-----------------|-----|
| 读写并发 | 写时阻塞读 | 读写可同时进行 |
| 写入速度 | 需要两次写（journal + db） | 一次追加写（WAL） |
| 适合场景 | 单连接、低频读写 | 多连接、高频并发 |

一句话：**WAL 让你的并发读写不互相阻塞**，这就是为什么你的 `db.py` 里要开它。

> 注：你当前用的是 `threading.local()` 每线程一个连接，WAL 的优势在"多个进程同时访问同一个 .db 文件"时最明显。你的场景下开启了也没坏处，是个好习惯。

---

### 步骤 3.3：动手操作 —— 新建 `Agent/db.py`

**背景**：为长期记忆创建 SQLite 数据库，替代 Markdown 文件存储。

#### 第 1 小步：设计表结构

先看当前 Markdown 存了什么数据，再设计对应的 SQL 表：

```
Markdown 结构（每个 session 一个文件）：
├── Stable Profile    → {name, gender, age, height, weight, training_level, goal}
├── Preferences       → {key1: value1, key2: value2, ...}
├── Constraints       → {key1: value1, key2: value2, ...}
├── Active Plan Facts → {key1: value1, ...}
├── Conflict Log      → [时间戳, 字段, 旧值, 新值]
└── Last Updated      → 时间戳
```

转化为两张表：

```sql
-- 表1：用户档案（一个 session 一条记录）
CREATE TABLE user_profile (
    session_id TEXT PRIMARY KEY,
    name TEXT,
    gender TEXT,
    age TEXT,
    height TEXT,
    weight TEXT,
    training_level TEXT,
    goal TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 表2：键值属性（偏好/约束/计划，一个 session 多条记录）
CREATE TABLE user_attributes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    category TEXT NOT NULL,   -- 'preference' | 'constraint' | 'plan_fact'
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES user_profile(session_id)
);
```

**为什么拆成两张表而不是一张大表？**

Profile 的字段是固定的（name、age 等），适合用列存。偏好/约束是动态的 key-value（用户可能提"不喜欢跑步"、"膝盖有伤"等），用 key-value 表更灵活。这就是数据库设计的第一范式思想：**一个字段只存一个值**。

#### 第 2 小步：创建 `Agent/db.py`

创建新文件 `Agent/db.py`，内容：

```python
"""
数据库模块：SQLite 持久化（长期记忆 + 聊天日志）。
使用 Python 标准库 sqlite3，零外部依赖。
"""
import sqlite3
import json
import logging
import threading
from pathlib import Path
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# 数据库文件路径
DB_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DB_DIR / "fit_agent.db"

# 线程本地连接（每个线程拥有自己的连接，避免多线程竞争）
_local = threading.local()

# ---------------------------------------------------------------------------
# 连接管理
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    """获取当前线程的 SQLite 连接（自动创建）。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row      # 查询结果可用 dict 方式访问
        conn.execute("PRAGMA journal_mode=WAL")     # WAL 模式：读写不互斥
        conn.execute("PRAGMA foreign_keys=ON")       # 启用外键约束
        _local.conn = conn
    return conn


def close_connection():
    """关闭当前线程的数据库连接（进程退出时调用）。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


# ---------------------------------------------------------------------------
# 表初始化
# ---------------------------------------------------------------------------

def init_db():
    """创建所有需要的表（幂等操作，已存在则跳过）。"""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS user_profile (
            session_id TEXT PRIMARY KEY,
            name TEXT,
            gender TEXT,
            age TEXT,
            height TEXT,
            weight TEXT,
            training_level TEXT,
            goal TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS user_attributes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            category TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES user_profile(session_id)
        );

        CREATE INDEX IF NOT EXISTS idx_attr_session
            ON user_attributes(session_id, category);

        CREATE TABLE IF NOT EXISTS conflict_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            source TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES user_profile(session_id)
        );

        CREATE INDEX IF NOT EXISTS idx_conflict_session
            ON conflict_log(session_id);

        CREATE TABLE IF NOT EXISTS chat_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            intent TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES user_profile(session_id)
        );

        CREATE INDEX IF NOT EXISTS idx_chat_session
            ON chat_log(session_id, created_at);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# 用户档案 CRUD
# ---------------------------------------------------------------------------

def upsert_profile(session_id: str, fields: dict[str, str]) -> None:
    """插入或更新用户档案。fields = {name, gender, age, height, weight, ...}"""
    if not fields:
        return
    conn = _get_conn()
    # 先确保记录存在
    conn.execute(
        "INSERT OR IGNORE INTO user_profile (session_id) VALUES (?)",
        (session_id,)
    )
    # 构建 SET 子句
    set_clause = ", ".join(f"{k} = ?" for k in fields.keys())
    sql = f"UPDATE user_profile SET {set_clause}, updated_at = datetime('now') WHERE session_id = ?"
    conn.execute(sql, list(fields.values()) + [session_id])
    conn.commit()


def get_profile(session_id: str) -> Optional[dict]:
    """获取用户档案，返回 dict 或 None。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM user_profile WHERE session_id = ?",
        (session_id,)
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# 属性 CRUD（偏好/约束/计划事实）
# ---------------------------------------------------------------------------

def upsert_attribute(session_id: str, category: str, key: str, value: str) -> None:
    """插入或更新一个属性。category: preference | constraint | plan_fact"""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO user_attributes (session_id, category, key, value)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(session_id, category, key) DO UPDATE
           SET value = excluded.value,
               updated_at = datetime('now')""",
        (session_id, category, key, value)
    )
    conn.commit()


def get_attributes(session_id: str, category: Optional[str] = None) -> list[dict]:
    """获取属性列表。不指定 category 则返回全部。"""
    conn = _get_conn()
    if category:
        rows = conn.execute(
            "SELECT * FROM user_attributes WHERE session_id = ? AND category = ?",
            (session_id, category)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM user_attributes WHERE session_id = ?",
            (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 冲突日志
# ---------------------------------------------------------------------------

def append_conflict(session_id: str, field_name: str, old_value: str, new_value: str, source: str) -> None:
    """记录一条冲突日志。"""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO conflict_log (session_id, field_name, old_value, new_value, source) VALUES (?, ?, ?, ?, ?)",
        (session_id, field_name, old_value, new_value, source)
    )
    conn.commit()


def get_conflicts(session_id: str, limit: int = 20) -> list[dict]:
    """获取最近 N 条冲突记录。"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM conflict_log WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
        (session_id, limit)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 聊天日志
# ---------------------------------------------------------------------------

def append_chat_log(session_id: str, role: str, content: str, intent: Optional[str] = None) -> None:
    """追加一条聊天记录。"""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO chat_log (session_id, role, content, intent) VALUES (?, ?, ?, ?)",
        (session_id, role, content, intent)
    )
    conn.commit()


def get_chat_history(session_id: str, limit: int = 50) -> list[dict]:
    """获取最近 N 条聊天记录。"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM chat_log WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
        (session_id, limit)
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


# ---------------------------------------------------------------------------
# 统计查询（SQL 实战练习用）
# ---------------------------------------------------------------------------

def get_stats() -> dict:
    """返回数据库统计信息。"""
    conn = _get_conn()
    profile_count = conn.execute("SELECT COUNT(*) FROM user_profile").fetchone()[0]
    chat_count = conn.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0]
    conflict_count = conn.execute("SELECT COUNT(*) FROM conflict_log").fetchone()[0]
    return {
        "total_sessions": profile_count,
        "total_chat_logs": chat_count,
        "total_conflicts": conflict_count,
        "db_path": str(DB_PATH),
    }


# ---------------------------------------------------------------------------
# 模块初始化：import 时自动建表
# ---------------------------------------------------------------------------

try:
    init_db()
    logger.info(f"Database initialized at {DB_PATH}")
except Exception as e:
    logger.warning(f"Database init failed (will retry on first use): {e}")
```

> **关键设计解释**：
> 1. **`threading.local()`**：每个线程有自己的 sqlite3 连接，避免多线程竞争同一个连接
> 2. **WAL 模式**：Write-Ahead Logging，写操作不阻塞读操作，适合读写并发场景
> 3. **`check_same_thread=False`**：sqlite3 默认只允许创建连接的线程使用，我们手动管理线程安全，关掉这个检查
> 4. **`row_factory = sqlite3.Row`**：查询结果可以 `row['name']` 这样访问，比 `row[0]` 直观
> 5. **`INSERT OR IGNORE`**：确保 session 记录先存在，再用 UPDATE 更新字段
> 6. **`ON CONFLICT DO UPDATE`（UPSERT）**：有则更新，无则插入，一条语句搞定

#### 验证方法

```bash
# 验证 1：import 不报错
python -c "from Agent.db import init_db, get_stats; print('OK:', get_stats())"

# 验证 2：插入和查询
python -c "
from Agent.db import upsert_profile, get_profile, upsert_attribute, get_attributes, append_chat_log, get_chat_history

# 写
upsert_profile('test-001', {'name': '小小', 'age': '24', 'goal': '增肌'})
upsert_attribute('test-001', 'preference', '训练时间', '早上')
upsert_attribute('test-001', 'constraint', '伤病', '膝盖旧伤')
append_chat_log('test-001', 'user', '我想增肌', 'training_plan')
append_chat_log('test-001', 'assistant', '好的，帮你制定训练计划', 'training_plan')

# 读
profile = get_profile('test-001')
print('档案:', profile)

attrs = get_attributes('test-001')
print('属性:', attrs)

history = get_chat_history('test-001')
print('聊天记录:', history)

# 统计
from Agent.db import get_stats
print('数据库统计:', get_stats())
"
```

预期输出：档案返回 name/age/goal，属性返回两条记录，聊天日志返回两条记录。

---

### 步骤 3.4：动手操作 —— 长期记忆迁移到 SQLite

**背景**：将 `long_memory.py` 的存储后端从 Markdown 文件迁移到刚建好的 SQLite。

#### 第 1 小步：理解迁移策略

迁移思路：**不改 `long_memory.py` 的对外接口**，只改内部实现。外部调用方（`workflow.py`）不感知存储变化。

对外接口保持不变：
- `load_long_memory(session_id)` → 返回 dict（来自 SQLite）
- `save_long_memory(session_id, memory_obj)` → 写入 SQLite
- `update_if_needed(...)` → 行为不变

#### 第 2 小步：在 `long_memory.py` 中接入 SQLite

打开 `Agent/memory/long_memory.py`，改造 `save_long_memory()` 和 `load_long_memory()`。

**改 `save_long_memory()`**（当前第 93 行）：

```python
def save_long_memory(session_id: str, memory_obj: dict) -> None:
    """
    保存长期记忆到 SQLite。
    自动拆分为 profile 和 attributes 两部分存储。
    """
    from Agent.db import upsert_profile, upsert_attribute, append_conflict

    try:
        # 1. Stable Profile → user_profile 表
        profile_section = memory_obj.get("Stable Profile", {}).get("items", {})
        profile_fields = {}
        for k in ['name', 'gender', 'age', 'height', 'weight', 'training_level', 'goal']:
            if k in profile_section and profile_section[k]:
                profile_fields[k] = profile_section[k]
        if profile_fields:
            upsert_profile(session_id, profile_fields)

        # 2. Preferences / Constraints / Active Plan Facts → user_attributes 表
        section_to_category = {
            "Preferences": "preference",
            "Constraints": "constraint",
            "Active Plan Facts": "plan_fact",
        }
        for section_name, category in section_to_category.items():
            section = memory_obj.get(section_name, {}).get("items", {})
            for key, value in section.items():
                if value:  # 跳过空值
                    upsert_attribute(session_id, category, key, str(value))

        # 3. Conflict Log → conflict_log 表（新增的冲突条目）
        # 注意：冲突日志在 merge_with_overwrite 中已经追加到 memory_obj，
        # 这里我们只记录新增的行（简化实现：冲突日志按条追加，不重复的才写入）
        conflict_section = memory_obj.get("Conflict Log", {}).get("raw_lines", [])
        # 冲突日志的新增已在 append_conflict_log() 中处理，此处跳过
        # （直接在 merge 时调用 append_conflict 写入 SQLite）

    except Exception as e:
        logger.warning(f"Failed to save long memory to SQLite for {session_id}: {e}", exc_info=True)
```

**改 `load_long_memory()`**（当前第 76 行）：

```python
def load_long_memory(session_id: str) -> dict:
    """
    从 SQLite 加载长期记忆为 dict 对象。
    不存在时返回默认模板。
    """
    from Agent.db import get_profile, get_attributes, get_conflicts

    try:
        profile = get_profile(session_id)
        attrs = get_attributes(session_id)
        conflicts = get_conflicts(session_id)

        # 构建与原来 Markdown 解析相同的 dict 结构（兼容外部调用方）
        memory_obj = {}

        # Stable Profile
        if profile:
            items = {k: v for k, v in profile.items()
                     if k not in ('session_id', 'created_at', 'updated_at') and v}
            raw_lines = [f"- {k}: {v}" for k, v in items.items()]
            memory_obj["Stable Profile"] = {"raw_lines": raw_lines, "items": items}
        else:
            memory_obj["Stable Profile"] = {"raw_lines": ["- 暂无信息"], "items": {}}

        # Attributes 按 category 分组
        for category, section_name in [
            ("preference", "Preferences"),
            ("constraint", "Constraints"),
            ("plan_fact", "Active Plan Facts"),
        ]:
            cat_attrs = [a for a in attrs if a["category"] == category]
            items = {a["key"]: a["value"] for a in cat_attrs}
            raw_lines = [f"- {k}: {v}" for k, v in items.items()]
            if not raw_lines:
                raw_lines = [f"- 暂无{'偏好' if category == 'preference' else '约束' if category == 'constraint' else '计划'}"]
            memory_obj[section_name] = {"raw_lines": raw_lines, "items": items}

        # Conflict Log
        if conflicts:
            raw_lines = [
                f"- [{c['created_at']}] 字段「{c['field_name']}」({c['source']})：{c['old_value']} → {c['new_value']}"
                for c in conflicts
            ]
        else:
            raw_lines = ["- 暂无冲突"]
        memory_obj["Conflict Log"] = {"raw_lines": raw_lines, "items": {}}

        # Last Updated
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        memory_obj["Last Updated"] = {
            "raw_lines": [f"- {now}"],
            "items": {"timestamp": now},
        }

        return memory_obj

    except Exception as e:
        logger.warning(f"Failed to load from SQLite for {session_id}: {e}, using default", exc_info=True)
        return parse_markdown_to_obj(DEFAULT_TEMPLATE)
```

#### 第 3 小步：修改 `append_conflict_log()` 同步写 SQLite

```python
def append_conflict_log(memory_obj: dict, conflicts: list[dict]) -> None:
    """将冲突追加到 Conflict Log（同时写内存对象和 SQLite）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    section = memory_obj.get("Conflict Log", {"raw_lines": [], "items": {}})

    # 获取 session_id（从 memory_obj 的 Stable Profile items 中推断不太可靠，
    # 这里我们额外需要传入。简化方案：从 memory_obj 本身无法直接获取 session_id，
    # 所以改为在 merge_with_overwrite 的调用方（update_long_memory）中同时写 SQLite）

    for conflict in conflicts:
        log_entry = (
            f"- [{now}] 字段「{conflict['field']}」"
            f"({conflict['source']})：{conflict['old_value']} → {conflict['new_value']}"
        )
        section["raw_lines"].append(log_entry)

    memory_obj["Conflict Log"] = section
```

> **注意**：由于 `append_conflict_log` 没有 session_id 参数，我们需要在 `update_long_memory()` 里额外调用 `from Agent.db import append_conflict` 写入 SQLite。这是一处小的接口调整。

#### 第 4 小步：修改 `update_long_memory()` 

在 `update_long_memory()` 中，冲突发生后同步写 SQLite：

```python
# 在步骤 4 追加冲突日志后添加：
if conflicts:
    append_conflict_log(merged, conflicts)
    # 同步写入 SQLite
    from Agent.db import append_conflict as db_append_conflict
    for c in conflicts:
        db_append_conflict(
            session_id=session_id,
            field_name=c['field'],
            old_value=str(c['old_value']),
            new_value=str(c['new_value']),
            source=c.get('source', '')
        )
```

#### 验证方法

```bash
# 重启后端，发几条消息触发长期记忆更新
python -m backend.main

# 另一个终端发多轮对话
curl -X POST http://localhost:8000/chat   -H "Content-Type: application/json"   -d '{"message": "我叫小小，今年24岁，目标是增肌"}'

# 重复对话直到累计超过 10 轮（触发长期记忆更新）

# 然后验证 SQLite 中有数据
python -c "
from Agent.db import get_stats, get_profile
stats = get_stats()
print('数据库统计:', stats)
# 如果能找到一个有数据的 session 就查出来看
"

# 也可以直接用 sqlite3 命令行查看
sqlite3 Agent/data/fit_agent.db ".tables"
sqlite3 Agent/data/fit_agent.db "SELECT * FROM user_profile LIMIT 5;"
```

---

### 步骤 3.5：SQL 实战练习

以下练习使用 `sqlite3` 命令行，直接在项目数据库上操作。

#### 练习 1：基本查询

```bash
# 查看所有表
sqlite3 Agent/data/fit_agent.db ".tables"

# 查看表结构
sqlite3 Agent/data/fit_agent.db ".schema user_profile"
sqlite3 Agent/data/fit_agent.db ".schema user_attributes"

# 查所有用户档案
sqlite3 Agent/data/fit_agent.db "SELECT * FROM user_profile;"

# 统计每个训练目标的用户数
sqlite3 Agent/data/fit_agent.db "SELECT goal, COUNT(*) as cnt FROM user_profile GROUP BY goal;"
```

#### 练习 2：JOIN 查询

```bash
# 关联查询：找出目标=增肌的用户的所有偏好
sqlite3 Agent/data/fit_agent.db "
SELECT p.name, p.goal, a.key, a.value
FROM user_profile p
JOIN user_attributes a ON p.session_id = a.session_id
WHERE p.goal = '增肌' AND a.category = 'preference';
"
```

#### 补充概念：B+ 树索引原理

你建了索引（`CREATE INDEX`），但它为什么能加速查询？数据库不是线性扫描，而是用了 **B+ 树**数据结构。

**为什么不用简单的二分查找？**

数据库数据存在磁盘上，磁盘读写的最小单位是"页"（4KB）。如果数据有 100 万行，二分查找需要随机跳到不同页读取，每次跳页都是一次磁盘 IO（~10ms），100 万行 ≈ 20 次跳转（log₂ 1M ≈ 20），每次 10ms = 200ms。

B+ 树的核心思想：**一个节点存一整个页（4KB），存尽可能多的 key**，减少树的高度。

```
二叉搜索树（每个节点 1 个 key）：       B+ 树（每个节点几百个 key）：
       100                                   [10 | 50 | 120 | 200]
      /   \                                /    /     |     \    \
    50    150                            [1-10][11-50][51-120][121-200][201+]
   /  \   /  \                            ↑ 每个节点一页 4KB，存几百个范围
  ...
高度 = log₂(N) ≈ 20 层                  高度 = log₅₀₀(N) ≈ 3 层
20 次磁盘 IO                             3 次磁盘 IO
```

**B+ 树的三个关键特性**：

| 特性 | 含义 | 好处 |
|------|------|------|
| **矮胖** | 每个节点存几百个 key | 树高 ≤ 3 层，百万行也只需 3 次 IO |
| **叶子链表** | 所有叶子节点用指针串联 | `ORDER BY key BETWEEN x AND y` 顺序读，不跳页 |
| **数据只在叶子** | 非叶子只存路由信息 | 叶子链表可以做范围扫描（`WHERE key > 100`） |

**什么是聚簇索引 vs 非聚簇索引？**

SQLite 中，`INTEGER PRIMARY KEY` 就是**聚簇索引**——数据行本身按主键顺序物理排列，叶子节点直接存整行数据。

非主键索引（`CREATE INDEX`）是**非聚簇索引**——叶子存的是索引列的值 + 主键。查到主键后还要回表查数据：

```
非聚簇索引查找过程：
  [idx_attr_session 索引 B+ 树]
         ↓ 命中 session_id='abc'
  得到: session_id='abc', 主键(id)=42
         ↓ 回到主键索引
  [user_attributes 主键 B+ 树]
         ↓ 按 id=42 查找
  得到: 完整行数据 {id:42, session_id:'abc', category:'preference', ...}
```

**什么时候建索引？**

| 建 | 不建/谨慎 |
|----|----------|
| WHERE 条件列 | 表很小（< 1000 行） |
| JOIN 的关联列（外键） | 频繁写入的列（每次写都要更新索引） |
| ORDER BY / GROUP BY 列 | 区分度低的列（如性别只有男/女） |

> 你的 `db.py` 里 `idx_attr_session` 建在 `(session_id, category)` 上，因为每次加载长期记忆都是 `WHERE session_id = ? AND category = ?`。`idx_chat_session` 建在 `(session_id, created_at)` 上，因为查聊天记录是 `WHERE session_id = ? ORDER BY created_at`。这两个索引都能把 SCAN 变成 SEARCH。

**什么程度的分区认为是区分度低**：一般用"选择性"衡量 = 不同值的数量 / 总行数。低于 0.1（10%）通常不适合建索引。比如性别只有 2 个值，100 万行中每个值约 50 万行，索引查了还要回表，不如直接全表扫描。

#### 练习 3：用 EXPLAIN 看查询计划

```bash
# 没有索引的查询
sqlite3 Agent/data/fit_agent.db "EXPLAIN QUERY PLAN SELECT * FROM chat_log WHERE session_id = 'test-001';"

# 有索引后同样的查询（应该显示 USING INDEX）
sqlite3 Agent/data/fit_agent.db "EXPLAIN QUERY PLAN SELECT * FROM user_attributes WHERE session_id = 'test-001' AND category = 'preference';"
```

预期：第一个显示 `SCAN chat_log`（全表扫描），第二个显示 `USING INDEX`（走索引）。

---

### 步骤 3.6：概念讲解 —— 事务 ACID

你已经在用事务了——`conn.commit()` 就是提交事务。但理解 ACID 能帮你写出更可靠的代码。

**ACID 四个字母拆开**：

| 属性 | 含义 | 大白话 | 反例 |
|------|------|--------|------|
| **A**tomicity 原子性 | 要么全做，要么全不做 | 转账：扣钱和加钱要么都成功，要么都回滚 | 扣了钱但加钱失败了，钱飞了 |
| **C**onsistency 一致性 | 数据符合所有约束 | 外键引用的 session 必须存在 | 聊天记录引用了不存在的 session |
| **I**solation 隔离性 | 并发事务互不干扰 | 两个请求同时改 age，不会互相覆盖 | 丢失更新 |
| **D**urability 持久性 | 事务提交后不丢 | 断电重启后数据还在 | INSERT 后没 commit 就崩了 |

**SQLite 的事务使用**：

```python
conn = _get_conn()
try:
    conn.execute("INSERT INTO user_profile ...")
    conn.execute("INSERT INTO user_attributes ...")
    conn.execute("INSERT INTO conflict_log ...")
    conn.commit()   # 三个操作全部成功才提交
except Exception:
    conn.rollback() # 任一失败则全部回滚
```

你的 `db.py` 中每个函数都独立 commit，简单但不够"事务"。如果 `upsert_profile` 成功但 `upsert_attribute` 失败，会出现不一致。后续可以在调用层用显式事务包装。

---

### 阶段三 理解测试

**问题 1**：三种存储（Redis、Qdrant、SQLite）各解决什么问题？如果"根据 session_id 查用户的训练次数"放在 Qdrant 里行不行？为什么？

<details>
<summary>提问目的</summary>

检验你是否真正理解了三种存储的本质区别，而不是死记硬背。关键在于"访问模式"——精确键查、相似度查、条件组合查，三种模式对应三种存储。选错存储会让简单的事变复杂。

</details>

**问题 2**：你的 `long_memory.py` 从 Markdown 改成 SQLite 后，外部调用方需要改吗？为什么？

<details>
<summary>提问目的</summary>

考察你对"接口 vs 实现"的理解。`load_long_memory()` 的签名和返回值格式不变，调用方就不需要改。这引出了软件工程的核心原则：封装变化。

</details>

**问题 3**：两个请求同时更新同一个 session 的长期记忆，当前代码（Markdown 版本和 SQLite 版本）分别会怎样？SQLite 版本是否天然解决这个问题？

<details>
<summary>提问目的</summary>

考察你对并发写冲突的认识。SQLite 本身有事务和锁，但 `INSERT OR REPLACE` 不能解决"两个请求读到旧值→各自修改→后写覆盖先写"的问题。要彻底解决需要乐观锁（加 version 列）或使用 `SELECT ... FOR UPDATE`。

</details>

**问题 4**：`EXPLAIN QUERY PLAN` 中 `SCAN` 和 `SEARCH` 的区别是什么？你在哪些字段上建了索引？

<details>
<summary>提问目的</summary>

训练你读查询计划的能力。SCAN = 全表扫描（慢），SEARCH = 走索引（快）。你建的 idx_attr_session 和 idx_chat_session 就是为了把 SCAN 变成 SEARCH。

</details>
---
## 阶段四：微服务基础 + 消息队列

> 阶段四的内容将在你完成阶段三并 review 通过后展开教学。

### 覆盖知识点

- 消息队列核心概念：Producer、Consumer、Broker、Topic、Queue
- 三种模式：点对点、发布订阅、请求响应
- 幂等性：为什么需要、如何实现（唯一 ID + 状态机）
- 限流算法：令牌桶、漏桶、滑动窗口
- 服务注册与发现概念

---

## 阶段五：分布式系统进阶

> 阶段五的内容将在你完成阶段四并 review 通过后展开教学。

### 覆盖知识点

- 分布式锁：Redlock 算法、锁续期（watchdog）
- 分布式事务概念：2PC、TCC、Saga
- 链路追踪：Trace、Span、Context Propagation
- 负载均衡：轮询、加权轮询、一致性哈希
- CAP 定理：一致性 vs 可用性 vs 分区容错

---

## 阶段六：性能压测 + 系统稳定性

> 阶段六的内容将在你完成阶段五并 review 通过后展开教学。

### 覆盖知识点

- 性能指标：QPS、延迟（P50/P90/P99）、错误率
- 压测工具：Locust
- 系统瓶颈定位：CPU bound vs IO bound vs Memory bound
- 容量规划
- 优雅关闭、健康检查

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
- [ ] 5.1 分布式锁
- [ ] 5.2 链路追踪
- [ ] 5.3 分库分表设计文档
- [ ] 阶段五理解测试

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
