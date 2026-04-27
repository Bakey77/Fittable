# Fix Log: `detect_conflicts` 类型误报修复

## 问题描述

长期记忆冲突检测在 `frequency` 字段出现误报：

```
conflicts: [{
  "field": "frequency",
  "old_value": "2",    # 来自 Markdown 解析 → 字符串
  "new_value": 2,      # 来自 LLM JSON → 整数
}]
```

实际为同一值 `"2"` == `2`，却触发了冲突通知。

## 根本原因

1. **类型不一致**：`old_value` 从 Markdown 解析（`parse_markdown_to_obj`），读出的是字符串；`new_value` 从 LLM 返回的 JSON（`pending_entities`），是 Python `int/float/bool`。

2. **直接比较**：`old_values[key] != new_val` 使用 Python 的 `!=`，字符串 `"2"` 与整数 `2` 不等。

3. **占位符集合未小写化**：之前用 `{"N/A"}` 大写形式配合 `s.lower()` 查询，大小写不一致导致 `"N/A"` 无法被识别为占位符。

## 修复方案

在 `detect_conflicts` 前插入 `_semantic_eq` 函数，实现类型自适应的语义比较：

```python
def _semantic_eq(old_val: Any, new_val: Any) -> bool:
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
```

比较策略（优先级递减）：
1. **占位符归一**：None / "暂无信息" / "暂无" / "n/a" / "" 等均归一为空字符串，两者都为空 → 相等
2. **数值比较**：`float(old) == float(new)`（兼容 "2" == 2、"28.0" == 28.0 等）
3. **字符串比较**：大小写不敏感

调用处修改（`detect_conflicts` 第 215 行）：

```diff
- if key in old_values and old_values[key] != new_val:
+ if key in old_values and not _semantic_eq(old_values[key], new_val):
```

同步将 `detect_conflicts` 内部占位符检查从一串 `and !=` 改用集合成员判断，提升可维护性。

## 修复的文件

- `Agent/memory/long_memory.py`

## 影响范围

- 冲突检测（`detect_conflicts`）
- 后续任何调用 `_semantic_eq` 的地方（若未来有需要）

## 测试建议

| 场景 | 旧值 | 新值 | 期望结果 |
|------|------|------|----------|
| 数值等价 | `"2"` | `2` | 不报冲突 |
| 数值等价 | `"28.0"` | `28` | 不报冲突 |
| 占位符等价 | `"暂无信息"` | `None` | 不报冲突 |
| 真实变化 | `"增肌"` | `"减脂"` | 报冲突 |
| 大小写敏感字符串 | `"Shanghai"` | `"shanghai"` | 不报冲突 |
| 真实冲突字符串 | `"减脂"` | `"增肌"` | 报冲突 |
