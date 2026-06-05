"""
长期记忆模块（Long-Term Memory）
Markdown 持久化，按 session_id 存储。

存储目录: Agent/memory/long_term_store/
文件名: {session_id}.md

冲突策略：
- 不二次确认
- 新值覆盖旧值
- 冲突写入 Conflict Log
"""
import json
import re
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
STORE_DIR = Path(__file__).parent / "long_term_store"
MAX_CHARS = 4000
BATCH_SIZE = 10

# 优先级 section（越靠前越重要，裁剪时优先保留）
PRIORITY_SECTIONS = [
    "Stable Profile",
    "Constraints",
    "Preferences",
    "Active Plan Facts",
    "Conflict Log",
    "Last Updated",
]

# 默认模板
DEFAULT_TEMPLATE = """# Long-Term Memory

## Stable Profile
- 暂无信息

## Preferences
- 暂无偏好

## Constraints
- 暂无约束

## Active Plan Facts
- 暂无计划

## Conflict Log
- 暂无冲突

## Last Updated
- 暂无更新
"""

# ---------------------------------------------------------------------------
# 食物同义词映射（读取时去重用，与 workflow.py 中 _FOOD_SYNONYM_CANONICAL_MAP 保持一致）
# ---------------------------------------------------------------------------
_FOOD_CANONICAL: dict[str, str] = {}
for _canonical, *_synonyms in [
    ["米饭", "白米饭", "白饭", "大米饭"],
    ["鸡肉", "鸡胸肉", "鸡腿肉", "鸡翅", "鸡"],
    ["猪肉", "猪瘦肉", "瘦肉", "五花肉", "猪"],
    ["牛肉", "牛腩", "肥牛", "牛排", "牛"],
    ["羊肉", "羊排", "羊腿", "羊"],
    ["鸡蛋", "蛋", "鸡蛋白", "蛋清"],
    ["牛奶", "奶", "牛乳"],
    ["面条", "面", "拉面", "挂面", "白面"],
    ["面包", "吐司", "全麦面包"],
    ["鱼", "鱼肉", "鱼类"],
]:
    for _name in [_canonical] + _synonyms:
        _FOOD_CANONICAL[_name] = _canonical


def _dedupe_preference_items(items: dict[str, str]) -> dict[str, str]:
    """对偏好 items 做同义词去重，如 米饭/白米饭 → 米饭。"""
    if not items:
        return items
    merged: dict[str, str] = {}
    for key, val in items.items():
        canonical = _FOOD_CANONICAL.get(key, key)
        if canonical not in merged:
            merged[canonical] = val
    if len(merged) != len(items):
        logger.info(f"[LONG_MEMORY] deduped preferences: {len(items)} → {len(merged)}")
    return merged


# ---------------------------------------------------------------------------
# 路径管理
# ---------------------------------------------------------------------------

def get_long_memory_path(session_id: str) -> Path:
    """获取指定 session 的长期记忆文件路径。"""
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    return STORE_DIR / f"{session_id}.md"


# ---------------------------------------------------------------------------
# 核心读写
# ---------------------------------------------------------------------------

def load_long_memory(session_id: str) -> dict:
    """
    从SQLite加载长期记忆为dict对象
    不存在时返回默认模版
    """
    from Agent.db import get_attributes, get_conflicts, get_profile, get_session_last_updated

    try:
        profile = get_profile(session_id)
        attrs = get_attributes(session_id)
        conflicts = get_conflicts(session_id)
        last_updated = get_session_last_updated(session_id)

        memory_obj = {}

        #构建与原来markdown解析相同的dict结构（兼容外部调用方）
        #Stable Profile
        if profile:
            items = {k:v for k,v in profile.items()
                     if k not in ('session_id','created_at','updated_at') and v}
            raw_lines = [f"- {k}:{v}" for k,v in items.items()]
            memory_obj["Stable Profile"] = {"raw_lines": raw_lines, "items": items}
        else:
            memory_obj["Stable Profile"] = {"raw_lines": ["- 暂无信息"], "items": {}}
        #Attributes 按 category分组
        for category,section_name in [
            ("preferences","Preferences"),
            ("constraints","Constraints"),
            ("active_plan_facts","Active Plan Facts"),
        ]:
            cat_attrs = [a for a in attrs if a['category'] == category]
            items = {a["key"]: a["value"] for a in cat_attrs}
            # 对偏好类做同义词去重（如 米饭/白米饭 → 米饭）
            if category == "preferences":
                items = _dedupe_preference_items(items)
            raw_lines = [f"- {k}: {v}" for k, v in items.items()]
            if not raw_lines:
                raw_lines = [f"- 暂无{'偏好' if category == 'preferences' else '约束' if category == 'constraints' else '计划'}"]
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

        # Last Updated 使用真实持久化时间；无持久化记录时回退到默认占位。
        memory_obj["Last Updated"] = {
            "raw_lines": [f"- {last_updated}"] if last_updated else ["- 暂无更新"],
            "items": {"timestamp": last_updated} if last_updated else {},
        }

        return memory_obj

    except Exception as e:
        logger.warning(f"Failed to load from SQLite for {session_id}: {e}, using default", exc_info=True)
        return parse_markdown_to_obj(DEFAULT_TEMPLATE)


    # path = get_long_memory_path(session_id)
    # if not path.exists():
    #     return parse_markdown_to_obj(DEFAULT_TEMPLATE)

    # try:
    #     text = path.read_text(encoding="utf-8")
    #     return parse_markdown_to_obj(text)
    # except Exception as e:
    #     logger.warning(f"Failed to load long memory for {session_id}: {e}")
    #     return parse_markdown_to_obj(DEFAULT_TEMPLATE)


def save_long_memory(session_id: str, memory_obj: dict) -> None:
    """
    保存长期记忆到SQLite
    自动拆分为 profile 和 attributes 两张表
    """
    from Agent.db import upsert_profile, upsert_attribute,append_conflict

    try:
        # 1. Stable Profile → user_profile 表
        profile_section = memory_obj.get("Stable Profile", {}).get("items", {})
        profile_fields = {}
        for k in ["name", "gender", "age", "height", "weight", "training_level", "goal"]:
            if k in profile_section and profile_section[k]:
                profile_fields[k] = profile_section[k]
        if profile_fields:
            upsert_profile(session_id, profile_fields)
        # 2. Preferences/Constraints/Active Plan Facts → user_attributes 表
        section_to_category = {
            "Preferences" : "preferences",
            "Constraints" : "constraints",
            "Active Plan Facts" : "active_plan_facts",
        }
        for section_name, category in section_to_category.items():
            section = memory_obj.get(section_name,{}).get("items",{})
            for key, value in section.items():
                if value:
                    upsert_attribute(session_id, category, key, str(value))
        # 3. Conflict Log → conflict_log 表
        # 注意：冲突日志在 merge_with_overwrite 中已经追加到 memory_obj，
        # 这里我们只记录新增的行（简化实现：冲突日志按条追加，不重复的才写入）
        conflict_section = memory_obj.get("Conflict Log",{}).get("raw_lines",[])
        # 冲突日志的新增已在 append_conflict_log() 中处理，此处跳过
        # （直接在 merge 时调用 append_conflict 写入 SQLite）

    except Exception as e:
        logger.warning(f"Failed to save long memory to SQLite for {session_id}: {e}",exc_info=True)
    
    # path = get_long_memory_path(session_id)
    # md_text = render_markdown(memory_obj)

    # tmp_path = path.with_suffix(".tmp")
    # try:
    #     tmp_path.write_text(md_text, encoding="utf-8")
    #     tmp_path.replace(path)
    # except Exception as e:
    #     logger.warning(f"Failed to save long memory for {session_id}: {e}")
    #     if tmp_path.exists():
    #         try:
    #             tmp_path.unlink()
    #         except OSError:
    #             pass
    #     raise


# ---------------------------------------------------------------------------
# 批次判断
# ---------------------------------------------------------------------------

def should_update_long_memory(session_id: str, recent_turns: list[dict]) -> bool:
    """
    判断是否应触发长期记忆更新。

    触发条件：累计对话轮数（total_turns）达到 BATCH_SIZE 的整数倍（10/20/30...）
    且尚未处理过该批次。

    total_turns 存储在 session memory 的 metadata 中:
    - metadata.total_turns: int (累计对话轮数，不随 trim 变化)
    - metadata.long_memory_batch_processed: int (已处理的批次基数)
    """
    from . import session_memory

    full_memory = session_memory.get_session_memory(session_id)
    metadata = full_memory.get("metadata", {})
    total_turns = metadata.get("total_turns", 0)

    if total_turns < BATCH_SIZE:
        print(f"[LONG_MEMORY] skip: total_turns={total_turns} < BATCH_SIZE={BATCH_SIZE}")
        return False

    if total_turns % BATCH_SIZE != 0:
        return False

    current_batch = total_turns // BATCH_SIZE
    processed_batch = metadata.get("long_memory_batch_processed", 0)

    if current_batch <= processed_batch:
        print(f"[LONG_MEMORY] skip: batch {current_batch} already processed (processed_batch={processed_batch})")
        return False

    print(f"[LONG_MEMORY] TRIGGER: total_turns={total_turns} batch={current_batch} processed_batch={processed_batch}")
    return True


def mark_batch_processed(session_id: str) -> None:
    """标记当前批次已处理（原子操作，更新批次游标）。"""
    from . import session_memory

    session_memory.update_metadata(session_id, "mark_batch_processed", BATCH_SIZE)


def build_recent_batch(recent_turns: list[dict], batch_size: int = BATCH_SIZE) -> list[dict]:
    """
    从 recent_turns 尾部提取最近 batch_size 轮对话。
    返回格式: [{"role": "user"|"assistant", "text": str}, ...]
    """
    if len(recent_turns) < batch_size:
        return []
    return recent_turns[-batch_size:]


# ---------------------------------------------------------------------------
# 冲突检测与合并
# ---------------------------------------------------------------------------

def _semantic_eq(old_val: Any, new_val: Any) -> bool:
    """
    判断两个值是否语义相等（类型自适应）。

    比较策略：
    1. 先统一标准化：None / 占位符均归一为空字符串
    2. 两者都为空 → 相等
    3. 尝试数值比较（"2" == 2, "28.0" == 28.0）
    4. 兜底：大小写不敏感字符串比较
    """
    _PLACEHOLDERS = {
        "暂无信息", "暂无偏好", "暂无约束", "暂无计划",
        "n/a", "无", "暂无", "",
    }

    def _normalize(v: Any) -> str:
        if v is None:
            return ""
        s = str(v).strip().lower()
        return "" if s in _PLACEHOLDERS else s

    return _normalize(old_val) == _normalize(new_val)


def detect_conflicts(old_memory: dict, new_facts: dict) -> list[dict]:
    """
    检测冲突：同一字段旧值存在且新值不同。

    返回冲突列表，每项包含:
    - field: str
    - old_value: Any
    - new_value: Any
    - source: str
    """
    conflicts = []
    tracked_sections = ["Preferences", "Constraints", "Stable Profile", "Active Plan Facts"]
    _PLACEHOLDERS = {"暂无信息", "暂无偏好", "暂无约束", "暂无计划"}

    for section in tracked_sections:
        old_section = old_memory.get(section, {})
        new_section = new_facts.get(section, {})

        # 收集旧值
        old_values = {}
        for line in old_section.get("raw_lines", []):
            if ":" in line:
                key = line.split(":", 1)[0].strip().lstrip("- ").strip()
                val = line.split(":", 1)[1].strip()
                if val not in _PLACEHOLDERS:
                    old_values[key] = val

        # 收集新值
        new_values = {}
        for key, val in new_section.items():
            if val:
                # _PLACEHOLDERS 全是字符串，非字符串（如 list）不可能是占位符，直接通过
                if isinstance(val, str) and val in _PLACEHOLDERS:
                    continue
                new_values[key] = val

        # 检测冲突（使用语义相等判断）
        for key, new_val in new_values.items():
            if key in old_values and not _semantic_eq(old_values[key], new_val):
                conflicts.append({
                    "field": key,
                    "old_value": old_values[key],
                    "new_value": new_val,
                    "source": section,
                })

    return conflicts


def merge_with_overwrite(old_memory: dict, new_facts: dict) -> tuple[dict, list[dict]]:
    """
    将 new_facts 合并到旧记忆中，新值覆盖旧值。
    返回 (合并后的 memory_obj, 冲突列表)。
    """
    conflicts = detect_conflicts(old_memory, new_facts)

    # 深拷贝避免修改原对象
    merged = json.loads(json.dumps(old_memory))

    for section, fields in new_facts.items():
        if section not in merged:
            merged[section] = {"raw_lines": [], "items": {}}
        if isinstance(fields, dict):
            merged[section]["items"].update(fields)
            for key, val in fields.items():
                found = False
                for i, line in enumerate(merged[section]["raw_lines"]):
                    if line.startswith(f"- {key}:"):
                        merged[section]["raw_lines"][i] = f"- {key}: {val}"
                        found = True
                        break
                if not found and val:
                    merged[section]["raw_lines"].append(f"- {key}: {val}")

    return merged, conflicts


def append_conflict_log(memory_obj: dict, conflicts: list[dict]) -> None:
    """将冲突追加到 Conflict Log。同时写内存对象和SQLite"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    section = memory_obj.get("Conflict Log", {"raw_lines": [], "items": {}})

    for conflict in conflicts:
        log_entry = (
            f"- [{now}] 字段「{conflict['field']}」"
            f"({conflict['source']})：{conflict['old_value']} → {conflict['new_value']}"
        )
        section["raw_lines"].append(log_entry)

    memory_obj["Conflict Log"] = section


def update_last_updated(memory_obj: dict) -> None:
    """更新 Last Updated 时间戳。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    memory_obj["Last Updated"] = {
        "raw_lines": [f"- {now}"],
        "items": {"timestamp": now}
    }


# ---------------------------------------------------------------------------
# Markdown 渲染与解析
# ---------------------------------------------------------------------------

def render_markdown(memory_obj: dict, max_chars: int = MAX_CHARS) -> str:
    """
    将 memory_obj 渲染为 Markdown 字符串。
    超长时按 section 级裁剪，优先级: Stable Profile > Constraints >
    Preferences > Active Plan Facts > Conflict Log > Last Updated。
    """
    # 按优先级收集每个 section 的行
    section_blocks: list[tuple[str, list[str]]] = []

    for section in PRIORITY_SECTIONS:
        data = memory_obj.get(section, {"raw_lines": []})
        raw_lines = data.get("raw_lines", []) if isinstance(data, dict) else []
        if raw_lines:
            lines = [f"## {section}"] + raw_lines
            section_blocks.append((section, lines))

    # 从低优先级开始尝试丢弃，直到满足长度限制
    # section_blocks 按优先级从高到低排列，丢弃时从后往前丢弃
    result_lines = [line for _, lines in section_blocks for line in lines]
    result_text = "\n".join(result_lines).strip()

    if len(result_text) <= max_chars:
        return result_text

    # 需要裁剪：先构建"必须保留"的部分（标题行 + 所有 section header）
    mandatory_lines: list[str] = ["# Long-Term Memory", ""]
    mandatory_chars = len("\n".join(mandatory_lines).strip()) + 1

    # 计算每个 section 的总字符量（用于判断是否可以放下）
    section_sizes: dict[str, int] = {}
    for section, lines in section_blocks:
        section_sizes[section] = len("\n".join(lines)) + 1

    # 分配：先放高优先级 section，尽量填满 max_chars
    allocated_lines: list[str] = list(mandatory_lines)
    allocated_chars = mandatory_chars

    for section, lines in section_blocks:
        section_text = "\n".join(lines) + "\n"
        if allocated_chars + len(section_text) <= max_chars:
            allocated_lines.append(section_text.rstrip())
            allocated_chars += len(section_text)
        else:
            # 放不下了，看是否能放下至少标题行
            header_text = f"## {section}"
            if allocated_chars + len(header_text) + 1 <= max_chars:
                allocated_lines.append(header_text)
                allocated_lines.append("_(该 section 已截断)_")
                allocated_chars = len("\n".join(allocated_lines))

    result_text = "\n".join(allocated_lines).strip()

    # 如果还是超长（理论上不应该发生），暴力截断
    if len(result_text) > max_chars:
        result_text = result_text[:max_chars]
        last_h2 = result_text.rfind("\n## ")
        last_nl = result_text.rfind("\n")
        cutoff = last_h2 if last_h2 > max_chars * 0.7 else last_nl
        result_text = result_text[:cutoff].strip() + "\n\n_(已截断)_"

    return result_text


def parse_markdown_to_obj(md_text: str) -> dict:
    """
    将 Markdown 文本解析为 memory_obj dict。
    结构: { section_name: { "raw_lines": [...], "items": {...} } }
    """
    result = {}
    current_section = None
    current_lines = []

    for line in md_text.split("\n"):
        h2_match = re.match(r"^## (.+)$", line.strip())
        if h2_match:
            if current_section:
                result[current_section] = _build_section_obj(current_section, current_lines)
            current_section = h2_match.group(1).strip()
            current_lines = []
        elif current_section and line.strip():
            current_lines.append(line.rstrip())

    if current_section:
        result[current_section] = _build_section_obj(current_section, current_lines)

    return result


def _build_section_obj(section: str, raw_lines: list[str]) -> dict:
    """从 raw_lines 构建 section 对象。"""
    items = {}
    for line in raw_lines:
        line = line.strip()
        if line.startswith("- "):
            line = line[2:]
        if ":" in line:
            key = line.split(":", 1)[0].strip()
            val = line.split(":", 1)[1].strip()
            items[key] = val
    return {"raw_lines": raw_lines, "items": items}


def _sanitize_profile_updates(updated_sections: dict[str, Any]) -> dict[str, Any]:
    """
    对 LLM 产出的 Stable Profile 做最小安全清洗，避免问句词误写入姓名。
    """
    if not isinstance(updated_sections, dict):
        return updated_sections
    stable = updated_sections.get("Stable Profile")
    if not isinstance(stable, dict):
        return updated_sections
    name_val = stable.get("name")
    if name_val is None:
        return updated_sections

    invalid_name_tokens = {"什么", "啥", "谁", "名字", "姓名"}
    name = str(name_val).strip()
    if (not name) or (name in invalid_name_tokens) or ("?" in name) or ("？" in name):
        stable["name"] = None
    return updated_sections


# ---------------------------------------------------------------------------
# LLM 更新逻辑
# ---------------------------------------------------------------------------

def _build_update_prompt(
    recent_batch: list[dict],
    current_md: str,
    latest_entities: dict | None,
    latest_intent: str | None,
) -> str:
    """构建 LLM 更新提示词。"""
    turns_text = ""
    for turn in recent_batch:
        role = "用户" if turn.get("role") == "user" else "助手"
        turns_text += f"{role}：{turn.get('text', '')}\n"

    entities_text = json.dumps(latest_entities or {}, ensure_ascii=False)
    intent_text = latest_intent or "未知"

    prompt = f"""你是一个健身助手的长期记忆管理模块。

## 任务
根据以下信息，更新用户的长期记忆。

## 最近对话（最近10轮）
```
{turns_text}
```

## 当前长期记忆
```
{current_md}
```

## 本轮提取的实体（如有）
```json
{entities_text}
```

## 本轮意图
{intent_text}

## 输出要求
请只输出 JSON 格式的更新结果，不要输出其他内容：

{{
  “updated_sections”: {{
    “Stable Profile”: {{
      “name”: “用户姓名或称呼（如有）”,
      “gender”: “用户性别（如有）”,
      “age”: “用户年龄（如有，保留数字或数字+岁）”,
      “height”: “用户身高（如有，保留原单位）”,
      “weight”: “用户体重（如有，保留原单位）”,
      “training_level”: “训练水平（如有）”,
      “goal”: “长期目标（如有）”
    }},
    “Preferences”: {{“key”: “value”, ...}},
    “Constraints”: {{“key”: “value”, ...}},
    “Active Plan Facts”: {{“key”: “value”, ...}}
  }},
  “summary”: “一句话概括本轮记忆更新内容”,
  “historical_events”: [
    {{
      “content”: “用1-2句话描述本轮对话中的重要事件，含因果链条。例如：俯卧撑手腕不适 → 建议改用推胸机 → 用户表示尝试”,
      “source_turns”: [1, 2, 3]
    }}
  ]
}}

要求：
1. 必须优先提取并更新用户档案字段：name、gender、age、height、weight。
2. 如果最近对话或当前长期记忆中有这些字段，请在 Stable Profile 中显式输出；没有则输出 null。
3. 严禁编造用户信息；只能基于”最近10轮对话 + 当前长期记忆 + 本轮实体”更新。
4. 冲突时以用户最新明确表达为准（例如年龄、身高、体重更新）。
5. Preferences 存放用户偏好（如训练偏好、饮食偏好）。
6. Constraints 存放约束条件（如时间限制、伤病、禁忌）。
7. Active Plan Facts 存放当前计划事实（如每周训练频次、计划类型）。
8. historical_events 提取本轮对话的重要事件摘要：
   - 每条1-2句话，包含动作→结果的因果链条
   - source_turns 是这个事件涉及的对话轮次索引（1-based，相对本批次的第几轮）
   - 如果本轮没有值得记录的事件，返回空数组 []
   - 适合记录的事件示例：训练反馈、计划调整、饮食偏好变更、伤病症状变化、用户表达新意向
   - 不适合记录：简单的问候、纯事实查询、无上下文的无意义对话
9. 只输出 JSON，不要有 markdown 代码块标记。
"""
    return prompt


def _write_historical_events(
    session_id: str,
    historical_events: list[dict],
) -> tuple[list[int], list[int]]:
    """
    将 historical_events 写入 SQLite + Qdrant。

    Args:
        session_id: 会话标识
        historical_events: LLM 产出的历史事件列表

    Returns:
        (success_event_ids, failed_event_ids)
    """
    from Agent.db import (
        insert_historical_event,
        prune_old_events,
        create_sync_job,
    )
    from tools.retriever1 import ensure_historical_event_in_qdrant, delete_historical_event_from_qdrant

    success_ids = []
    failed_ids = []

    for event in historical_events:
        content = event.get("content", "")
        source_turns = event.get("source_turns", [])
        if not content:
            continue

        # 1. 先写入 SQLite（事实源）
        try:
            event_id = insert_historical_event(session_id, content, source_turns)
        except Exception as e:
            logger.error(f"[LONG_MEMORY] SQLite insert event failed: {e}")
            continue

        # 2. 再写入 Qdrant
        try:
            ensure_historical_event_in_qdrant(
                event_id=event_id,
                session_id=session_id,
                content=content,
                source_turns=source_turns,
            )
            success_ids.append(event_id)
        except Exception as e:
            logger.error(f"[LONG_MEMORY] Qdrant upsert failed for event={event_id}: {e}")
            # 创建补偿任务
            try:
                create_sync_job(event_id, session_id, str(e))
            except Exception:
                pass
            failed_ids.append(event_id)

    # 3. 裁剪：超出 50 条时删除最旧事件
    try:
        deleted_ids = prune_old_events(session_id, max_events=50)
        for did in deleted_ids:
            try:
                delete_historical_event_from_qdrant(did)
            except Exception as e:
                logger.warning(f"[LONG_MEMORY] Failed to delete Qdrant point for pruned event={did}: {e}")
    except Exception as e:
        logger.error(f"[LONG_MEMORY] Prune failed: {e}")

    # 4. 如果有失败的 event，入队补偿重试
    if failed_ids:
        from Agent.worker import enqueue_retry_sync_jobs
        enqueue_retry_sync_jobs()

    return success_ids, failed_ids


def update_long_memory(
    session_id: str,
    recent_turns_batch: list[dict],
    latest_entities: dict | None = None,
    latest_intent: str | None = None,
) -> dict:
    """
    执行长期记忆更新。

    参数:
    - session_id: 会话标识
    - recent_turns_batch: 最近10轮对话
    - latest_entities: 本轮提取的结构化实体
    - latest_intent: 本轮意图

    返回:
    - dict: {
        "success": bool,
        "conflicts": list[dict],  # 冲突列表
        "applied_updates": list[str],  # 更新的字段列表
        "historical_events_count": int,  # 新增历史事件数
        "new_markdown": str,  # 新 markdown 内容（已渲染）
        "conflict_notice": str | None,  # 提示用户的文案
      }
    """
    from backend.services.llm import get_longcat_llm

    # 1. 加载当前长期记忆
    current_obj = load_long_memory(session_id)
    current_md = render_markdown(current_obj)

    # 2. 调用 LLM 分析更新
    try:
        llm = get_longcat_llm()
        prompt = _build_update_prompt(recent_turns_batch, current_md, latest_entities, latest_intent)
        response = llm.invoke([{"role": "user", "content": prompt}])
        raw_output = response.content if hasattr(response, "content") else str(response)

        # 解析 JSON 输出
        raw_output = raw_output.strip()
        if raw_output.startswith("```json"):
            raw_output = raw_output[7:]
        if raw_output.startswith("```"):
            raw_output = raw_output[3:]
        if raw_output.endswith("```"):
            raw_output = raw_output[:-3]
        raw_output = raw_output.strip()

        llm_result = json.loads(raw_output)
        updated_sections = llm_result.get("updated_sections", {})
        updated_sections = _sanitize_profile_updates(updated_sections)
        applied_updates = []
        for section, fields in updated_sections.items():
            if isinstance(fields, dict):
                applied_updates.extend([f"{section}.{k}" for k in fields.keys()])

        # 提取 historical_events
        historical_events = llm_result.get("historical_events", [])
        if not isinstance(historical_events, list):
            historical_events = []

    except Exception as e:
        logger.error(f"[LONG_MEMORY] LLM update FAILED for session={session_id}: {e}", exc_info=True)
        return {
            "success": False,
            "conflicts": [],
            "applied_updates": [],
            "historical_events_count": 0,
            "new_markdown": current_md,
            "conflict_notice": None,
        }

    # 3. 检测冲突
    merged, conflicts = merge_with_overwrite(current_obj, updated_sections)

    # 4. 追加冲突日志
    if conflicts:
        append_conflict_log(merged, conflicts)
        from Agent.db import append_conflict as db_append_conflict
        #同步写入SQLite
        for c in conflicts:
            db_append_conflict(
                session_id=session_id,
                field_name=c['field'],
                old_value=c['old_value'],
                new_value=c['new_value'],
                source=c['source']
            )

    # 5. 更新时间戳
    update_last_updated(merged)

    # 6. 渲染新 Markdown
    new_md = render_markdown(merged)

    # 7. 原子写入 profile
    try:
        save_long_memory(session_id, merged)
    except Exception as e:
        logger.error(f"[LONG_MEMORY] Save FAILED for session={session_id}: {e}", exc_info=True)
        return {
            "success": False,
            "conflicts": conflicts,
            "applied_updates": applied_updates,
            "historical_events_count": 0,
            "new_markdown": new_md,
            "conflict_notice": None,
        }

    # 8. 写入 historical_events
    successful_events, failed_events = 0, 0
    if historical_events:
        success_ids, failed_ids = _write_historical_events(session_id, historical_events)
        successful_events = len(success_ids)
        failed_events = len(failed_ids)
        logger.info(
            f"[LONG_MEMORY] historical_events: {successful_events} success, {failed_events} failed "
            f"for session={session_id}"
        )

    # 9. 生成 conflict_notice
    conflict_notice = None
    if conflicts:
        conflict_notice = "检测到你的偏好发生变化（如训练偏好/约束），我已按你最新信息更新长期记忆。"

    return {
        "success": True,
        "conflicts": conflicts,
        "applied_updates": applied_updates,
        "historical_events_count": successful_events,
        "new_markdown": new_md,
        "conflict_notice": conflict_notice,
    }


# ---------------------------------------------------------------------------
# 外部调用入口
# ---------------------------------------------------------------------------

def update_if_needed(
    session_id: str,
    recent_turns: list[dict],
    latest_entities: dict | None,
    latest_intent: str | None,
) -> dict:
    """
    检查并执行长期记忆更新（若达到触发条件）。

    返回结构化结果（始终非 None）：
    {
        "triggered": bool,      # 是否达到触发条件
        "success": bool,         # 更新是否成功
        "warning": str | None,   # 警告信息（若有）
        "conflicts": list,       # 冲突列表
        "conflict_notice": str | None,  # 用户提示
    }
    """
    triggered = should_update_long_memory(session_id, recent_turns)

    if not triggered:
        return {
            "triggered": False,
            "success": False,
            "warning": None,
            "conflicts": [],
            "conflict_notice": None,
        }

    recent_batch = build_recent_batch(recent_turns, BATCH_SIZE)
    if not recent_batch:
        return {
            "triggered": True,
            "success": False,
            "warning": "No recent batch available",
            "conflicts": [],
            "conflict_notice": None,
        }

    try:
        result = update_long_memory(session_id, recent_batch, latest_entities, latest_intent)
        if result.get("success"):
            mark_batch_processed(session_id)
        return {
            "triggered": True,
            "success": result.get("success", False),
            "warning": None,
            "conflicts": result.get("conflicts", []),
            "conflict_notice": result.get("conflict_notice"),
        }
    except Exception as e:
        logger.error(f"[LONG_MEMORY] update_if_needed FAILED for session={session_id}: {e}", exc_info=True)
        return {
            "triggered": True,
            "success": False,
            "warning": str(e),
            "conflicts": [],
            "conflict_notice": None,
        }
