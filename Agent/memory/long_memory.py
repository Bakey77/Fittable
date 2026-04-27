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
    加载长期记忆为 dict 对象。
    文件不存在时返回默认模板解析结果。
    """
    path = get_long_memory_path(session_id)
    if not path.exists():
        return parse_markdown_to_obj(DEFAULT_TEMPLATE)

    try:
        text = path.read_text(encoding="utf-8")
        return parse_markdown_to_obj(text)
    except Exception as e:
        logger.warning(f"Failed to load long memory for {session_id}: {e}")
        return parse_markdown_to_obj(DEFAULT_TEMPLATE)


def save_long_memory(session_id: str, memory_obj: dict) -> None:
    """
    将 memory_obj 渲染为 Markdown 并写入文件。
    使用 tmp + replace 实现原子写入。
    """
    path = get_long_memory_path(session_id)
    md_text = render_markdown(memory_obj)

    tmp_path = path.with_suffix(".tmp")
    try:
        tmp_path.write_text(md_text, encoding="utf-8")
        tmp_path.replace(path)
    except Exception as e:
        logger.warning(f"Failed to save long memory for {session_id}: {e}")
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


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
        return False

    if total_turns % BATCH_SIZE != 0:
        return False

    current_batch = total_turns // BATCH_SIZE
    processed_batch = metadata.get("long_memory_batch_processed", 0)

    if current_batch <= processed_batch:
        return False

    return True


def mark_batch_processed(session_id: str) -> None:
    """标记当前批次已处理（更新批次游标）。"""
    from . import session_memory

    full_memory = session_memory.get_session_memory(session_id)
    metadata = full_memory.get("metadata", {})
    total_turns = metadata.get("total_turns", 0)
    current_batch = total_turns // BATCH_SIZE

    if "metadata" not in full_memory:
        full_memory["metadata"] = {}
    full_memory["metadata"]["long_memory_batch_processed"] = current_batch

    session_memory._save_memory(session_id, full_memory)


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
            if val and val not in _PLACEHOLDERS:
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
    """将冲突追加到 Conflict Log。"""
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
  "updated_sections": {{
    "Stable Profile": {{
      "name": "用户姓名或称呼（如有）",
      "gender": "用户性别（如有）",
      "age": "用户年龄（如有，保留数字或数字+岁）",
      "height": "用户身高（如有，保留原单位）",
      "weight": "用户体重（如有，保留原单位）",
      "training_level": "训练水平（如有）",
      "goal": "长期目标（如有）"
    }},
    "Preferences": {{"key": "value", ...}},
    "Constraints": {{"key": "value", ...}},
    "Active Plan Facts": {{"key": "value", ...}}
  }},
  "summary": "一句话概括本轮记忆更新内容"
}}

要求：
1. 必须优先提取并更新用户档案字段：name、gender、age、height、weight。
2. 如果最近对话或当前长期记忆中有这些字段，请在 Stable Profile 中显式输出；没有则输出 null。
3. 严禁编造用户信息；只能基于“最近10轮对话 + 当前长期记忆 + 本轮实体”更新。
4. 冲突时以用户最新明确表达为准（例如年龄、身高、体重更新）。
5. Preferences 存放用户偏好（如训练偏好、饮食偏好）。
6. Constraints 存放约束条件（如时间限制、伤病、禁忌）。
7. Active Plan Facts 存放当前计划事实（如每周训练频次、计划类型）。
8. 只输出 JSON，不要有 markdown 代码块标记。
"""
    return prompt


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
        "new_markdown": str,  # 新 markdown 内容（已渲染）
        "conflict_notice": str | None,  # 提示用户的文案
      }
    """
    from backend.services.llm import get_llm

    # 1. 加载当前长期记忆
    current_obj = load_long_memory(session_id)
    current_md = render_markdown(current_obj)

    # 2. 调用 LLM 分析更新
    try:
        llm = get_llm()
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
        applied_updates = []
        for section, fields in updated_sections.items():
            if isinstance(fields, dict):
                applied_updates.extend([f"{section}.{k}" for k in fields.keys()])

    except Exception as e:
        logger.warning(f"LLM update failed for {session_id}: {e}")
        return {
            "success": False,
            "conflicts": [],
            "applied_updates": [],
            "new_markdown": current_md,
            "conflict_notice": None,
        }

    # 3. 检测冲突
    merged, conflicts = merge_with_overwrite(current_obj, updated_sections)

    # 4. 追加冲突日志
    if conflicts:
        append_conflict_log(merged, conflicts)

    # 5. 更新时间戳
    update_last_updated(merged)

    # 6. 渲染新 Markdown
    new_md = render_markdown(merged)

    # 7. 原子写入
    try:
        save_long_memory(session_id, merged)
    except Exception as e:
        logger.warning(f"Save failed for {session_id}: {e}")
        return {
            "success": False,
            "conflicts": conflicts,
            "applied_updates": applied_updates,
            "new_markdown": new_md,
            "conflict_notice": None,
        }

    # 8. 生成 conflict_notice
    conflict_notice = None
    if conflicts:
        conflict_notice = "检测到你的偏好发生变化（如训练偏好/约束），我已按你最新信息更新长期记忆。"

    return {
        "success": True,
        "conflicts": conflicts,
        "applied_updates": applied_updates,
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
        logger.warning(f"Long memory update failed for {session_id}: {e}")
        return {
            "triggered": True,
            "success": False,
            "warning": str(e),
            "conflicts": [],
            "conflict_notice": None,
        }
