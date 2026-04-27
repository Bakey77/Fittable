"""
FastAPI 入口
"""
import sys
from pathlib import Path

# 确保项目根目录在 sys.path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import uuid
import json
import asyncio
import re
import logging
from typing import Optional, AsyncIterator
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Cookie, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from Agent.workflow import run_workflow
from Agent.memory.session_memory import (
    check_compress_status,
    retry_compress,
    discard_compress_backup,
    clear_session_memory,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Fit-Agent API")

# CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 辅助函数：全中文归一化（训练计划专用）
# ---------------------------------------------------------------------------

def _normalize_to_chinese(text: str) -> str:
    """
    将英文动作名/focus 转换为中文。
    - 优先精确映射
    - 其次驼峰/下划线分词后映射已知词片段
    - 最后替换为通用占位符 + 日志警告
    """
    if not text or not isinstance(text, str):
        return text

    # 第一层：精确映射（大小写兼容）
    exercise_map = {
        # 精确映射
        "BarbellSquat": "杠铃深蹲", "barbellsquat": "杠铃深蹲",
        "BenchPress": "杠铃卧推", "benchpress": "杠铃卧推",
        "LatPulldown": "高位下拉", "latpulldown": "高位下拉",
        "RomanianDeadlift": "罗马尼亚硬拉", "romaniandeadlift": "罗马尼亚硬拉",
        "Plank": "平板支撑", "plank": "平板支撑",
        "Deadlift": "硬拉", "deadlift": "硬拉",
        "OverheadPress": "肩上推举", "overheadpress": "肩上推举",
        "InclineDumbbellPress": "上斜哑铃卧推", "inclinedumbbellpress": "上斜哑铃卧推",
        "LegCurl": "腿弯举", "legcurl": "腿弯举",
        "HangingLegRaise": "悬垂举腿", "hanginglegraise": "悬垂举腿",
        "DumbbellRow": "哑铃划船", "dumbbellrow": "哑铃划船",
        "CableFly": "绳索夹胸", "cablefly": "绳索夹胸",
        "BicepCurl": "二头弯举", "bicepcurl": "二头弯举",
        "TricepPushdown": "三头下压", "triceppushdown": "三头下压",
        "LegExtension": "腿屈伸", "legextension": "腿屈伸",
        "CalfRaise": "提踵", "calfraise": "提踵",
        "Lunges": "箭步蹲", "lunges": "箭步蹲",
        "FrontSquat": "前深蹲", "frontsquat": "前深蹲",
        "BarbellRow": "杠铃划船", "barbellrow": "杠铃划船",
        "PullUps": "引体向上", "pullups": "引体向上",
        "PushUps": "俯卧撑", "pushups": "俯卧撑",
        "Crunches": "卷腹", "crunches": "卷腹",
    }

    # 检查精确映射
    if text in exercise_map:
        return exercise_map[text]

    # 第二层：驼峰/下划线分词后的片段匹配
    # 将文本分解为单词段，尝试匹配已知词汇
    word_parts = re.findall(r'[A-Z][a-z]+|[a-z]+', text)
    if len(word_parts) > 1:
        # 多单词组合，尝试片段组合映射
        # 例如 "BarbellRow" -> ["Barbell", "Row"]
        for key, value in exercise_map.items():
            if key.lower() == text.lower():
                return value

    # 第三层：检查是否包含已知英文词根 或 任何英文字母
    has_english = any(c.isalpha() and ord(c) < 128 for c in text)
    if has_english:
        # 检查是否包含已知的英文词根
        known_roots = ["squat", "deadlift", "press", "row", "curl", "fly", "raise", "pull", "push", "bench", "dumbbell", "barbell"]
        if any(eng in text.lower() for eng in known_roots):
            # 包含已知的英文词根，替换为通用占位符并记录
            logger.warning(f"[planning] 未能转换英文动作名: {text}，使用通用占位符")
            return "动作"
        else:
            # 即使不包含已知词根，只要有英文字母就转换为通用占位符
            logger.warning(f"[planning] 检测到未知英文动作名: {text}，使用通用占位符")
            return "动作"

    return text


def _normalize_focus_to_chinese(focus: str) -> str:
    """将 focus 英文转为中文"""
    focus_map = {
        "push": "推", "Push": "推",
        "pull": "拉", "Pull": "拉",
        "legs": "腿", "Legs": "腿",
        "full_body": "全身", "Full_Body": "全身", "fullbody": "全身", "FullBody": "全身",
        "core": "核心", "Core": "核心",
        "cardio": "有氧", "Cardio": "有氧",
    }
    return focus_map.get(focus, focus)


def _normalize_day_to_chinese(day: str) -> str:
    """将 'Day1', 'day1' 等转为中文 '第1天'"""
    if not day or not isinstance(day, str):
        return day
    # 匹配 Day/day + 数字
    match = re.match(r'^[Dd]ay(\d+)$', day.strip())
    if match:
        num = match.group(1)
        return f"第{num}天"
    return day


def _sanitize_plan_to_chinese(plan: dict) -> dict:
    """
    对训练计划进行全中文归一化。
    - 转换 focus 为中文
    - 转换 day 标签为中文（Day1 → 第1天）
    - 转换 exercise name 为中文
    - 检查并兜底处理残留英文
    """
    if not isinstance(plan, dict) or "plan" not in plan:
        return plan

    plan_copy = dict(plan)
    if not isinstance(plan_copy.get("plan"), list):
        return plan_copy

    for day in plan_copy["plan"]:
        if not isinstance(day, dict):
            continue

        # 转换 focus
        if "focus" in day:
            day["focus"] = _normalize_focus_to_chinese(day["focus"])

        # 转换 day 标签
        if "day" in day:
            day["day"] = _normalize_day_to_chinese(day["day"])

        # 转换 exercises name
        if "exercises" in day and isinstance(day["exercises"], list):
            for ex in day["exercises"]:
                if isinstance(ex, dict) and "name" in ex:
                    ex["name"] = _normalize_to_chinese(ex["name"])

    return plan_copy


# CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Schema 定义
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class CompressActionRequest(BaseModel):
    session_id: str


class ClearSessionRequest(BaseModel):
    session_id: str


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def get_or_create_session_id(
    cookie_session_id: Optional[str],
    response: Response,
    explicit_session_id: Optional[str] = None,
) -> str:
    """
    从 Cookie 获取 session_id，若不存在则自动生成并设置 Cookie

    参数:
    - session_id: 从 Cookie 获取的 session_id（可能为 None）
    - response: FastAPI Response 对象，用于设置 Cookie

    返回:
    - 有效的 session_id
    """
    # 优先使用请求体传入的 session_id，其次使用 Cookie，最后自动生成
    session_id = explicit_session_id or cookie_session_id
    if not session_id:
        session_id = str(uuid.uuid4())

    # 如果 Cookie 中没有或与当前 sid 不一致，刷新 Cookie
    if cookie_session_id != session_id:
        response.set_cookie(
            key="session_id",
            value=session_id,
            max_age=30 * 24 * 60 * 60,  # 30天
            httponly=True,
            samesite="lax",
        )
    return session_id


# ---------------------------------------------------------------------------
# API 路由
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """健康检查"""
    return {"status": "ok"}


@app.post("/chat")
def chat(req: ChatRequest, response: Response, session_id: Optional[str] = Cookie(default=None)):
    """
    聊天接口

    - 自动管理 session_id（Cookie）
    - 调用 workflow 执行
    - 返回压缩重试状态（若有）
    """
    sid = get_or_create_session_id(session_id, response, explicit_session_id=req.session_id)

    try:
        result = run_workflow(req.message, session_id=sid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # 构建响应
    resp = {
        "session_id": sid,
        "status": result.get("status"),
        "primary_intent": result.get("primary_intent"),
        "guidance": result.get("guidance"),
        "plan": result.get("plan"),
        "analysis_result": result.get("analysis_result"),
        "general_response": result.get("general_response"),
        "follow_up_questions": result.get("follow_up_questions"),
        "waiting_info": result.get("waiting_info"),
    }

    # 透传长期记忆冲突信息
    if result.get("conflict_notice"):
        resp["conflict_notice"] = result["conflict_notice"]
    if result.get("conflicts"):
        resp["conflicts"] = result["conflicts"]

    # 检查压缩状态
    if result.get("compress_retry_needed"):
        resp["compress_retry_needed"] = True
        resp["compress_backup_info"] = result.get("compress_backup_info")
        resp["_compress_error"] = result.get("_compress_error")
        resp["_message"] = (
            "历史对话压缩失败，是否重试？"
            f"（{result['compress_backup_info']['turns_count']} 条旧对话待压缩）"
            "调用 /compress-retry 重试，或 /compress-discard 丢弃。"
        )

    return resp


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, response: Response, session_id: Optional[str] = Cookie(default=None)):
    """
    SSE 流式聊天接口

    - 自动管理 session_id（Cookie）
    - 调用 workflow 执行
    - 以 SSE 形式流式返回 intent 和文本内容
    """
    sid = get_or_create_session_id(session_id, response, explicit_session_id=req.session_id)

    def run_sync_workflow():
        return run_workflow(req.message, session_id=sid)

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(ThreadPoolExecutor(), run_sync_workflow)

    primary_intent = result.get("primary_intent") or ""

    # 根据意图类型决定输出的文本内容
    text = ""
    if result.get("guidance"):
        text = result["guidance"]
    elif result.get("plan"):
        # 先进行全中文归一化
        plan = _sanitize_plan_to_chinese(result["plan"])

        goal_labels = {"muscle_gain": "增肌", "fat_loss": "减脂", "beginner": "新手入门"}
        focus_labels = {
            "push": "推", "pull": "拉", "legs": "腿", "full_body": "全身", "core": "核心", "cardio": "有氧",
            "Push": "推", "Pull": "拉", "Legs": "腿", "Full_Body": "全身", "Core": "核心", "Cardio": "有氧",
        }

        goal_text = goal_labels.get(plan.get('goal', ''), plan.get('goal', ''))
        frequency = plan.get('frequency', 0)

        # 按 focus 分组
        focus_groups = {}
        for day in plan.get("plan", []):
            focus = day.get('focus', 'unknown')
            focus_zh = focus_labels.get(focus, focus)
            if focus_zh not in focus_groups:
                focus_groups[focus_zh] = []
            focus_groups[focus_zh].append(day)

        # 生成文本
        lines = [f"训练计划：{goal_text}"]
        lines.append(f"每周训练：{frequency}天 | 训练频率：{', '.join(focus_groups.keys())}")
        lines.append("")

        for focus_zh, days in focus_groups.items():
            lines.append(f"【{focus_zh}训练日】")
            for day in days:
                # day 已在归一化时转换为中文
                lines.append(f"  {day.get('day')}")
                for ex in day.get('exercises', []):
                    # exercise name 已在归一化时转换为中文
                    ex_name = ex.get('name', '')
                    lines.append(f"    - {ex_name}: {ex.get('sets')}组 × {ex.get('reps')}次")
            lines.append("")

        text = "\n".join(lines).strip()
    elif result.get("analysis_result"):
        text = result["analysis_result"]
    elif result.get("general_response"):
        text = result["general_response"]
    elif result.get("follow_up_questions"):
        text = "追问提示：\n" + "\n".join(result["follow_up_questions"])

    # ===== 最终检查：planning 输出必须全中文（数字除外） =====
    if result.get("primary_intent") == "training_plan" and text:
        # 检查是否还有残留英文字母（数字、中文、常见符号允许）
        # 正则：匹配任何不是数字、中文、常见标点的ASCII字母
        english_pattern = r'[a-zA-Z]'
        if re.search(english_pattern, text):
            logger.warning(f"[planning] 检测到输出中残留英文字母，正在进行二次处理\n原文本: {text[:100]}")
            # 二次处理：用单个字符替换策略
            processed_lines = []
            for line in text.split('\n'):
                # 对每行进行清理
                cleaned_line = line
                # 查找所有英文单词并尝试替换
                words = re.findall(r'[a-zA-Z]+', line)
                for word in words:
                    # 尝试用中文替换
                    replacement = _normalize_to_chinese(word)
                    cleaned_line = cleaned_line.replace(word, replacement)
                processed_lines.append(cleaned_line)
            text = '\n'.join(processed_lines)
            logger.warning(f"[planning] 二次处理后文本: {text[:100]}")

    async def event_stream() -> AsyncIterator[str]:
        # 先发送 session_id
        yield f"data: {json.dumps({'session_id': sid})}\n\n"

        # 发送 intent 标识
        yield f"data: {json.dumps({'intent': primary_intent})}\n\n"

        # 逐字符 yield 文本内容
        for ch in text:
            yield f"data: {ch}\n\n"
            await asyncio.sleep(0)  # 让出控制权，允许其他协程执行

        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/compress-retry")
def compress_retry(req: CompressActionRequest, response: Response):
    """
    重试 LLM 压缩

    - 读取备份的 older_turns
    - 调用 LLM 重新压缩
    - 成功则更新 memory_summary，清除备份
    - 失败则保留备份，抛出异常
    """
    sid = req.session_id

    # 检查是否有备份
    status = check_compress_status(sid)
    if not status["needs_retry"]:
        raise HTTPException(status_code=400, detail="No compress backup found, nothing to retry")

    try:
        result = retry_compress(sid)
        if result["success"]:
            return {
                "session_id": sid,
                "success": True,
                "summary": result["summary"],
            }
        else:
            raise HTTPException(status_code=500, detail=result["error"])
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/compress-discard")
def compress_discard(req: CompressActionRequest):
    """
    丢弃压缩备份

    - 清除 older_turns 备份
    - 清除 compress_failed 标记
    - 注意：这意味着那些旧对话将不会被压缩，可能丢失上下文
    """
    sid = req.session_id

    discard_compress_backup(sid)

    return {
        "session_id": sid,
        "success": True,
        "message": "压缩备份已丢弃，历史对话将保留在 recent_turns 中（可能累积变长）",
    }


@app.post("/clear-session")
def clear_session(req: ClearSessionRequest):
    """
    清除指定 session 的所有记忆

    - 清除主记忆数据
    - 清除压缩备份
    - 清除压缩失败标记
    """
    sid = req.session_id

    clear_session_memory(sid)

    return {
        "session_id": sid,
        "success": True,
        "message": "Session memory cleared",
    }


@app.get("/compress-status")
def compress_status(session_id: str):
    """
    查询压缩状态（用于前端检查是否需要提示用户）

    返回:
    - needs_retry: bool
    - backup_info: dict | None
    """
    status = check_compress_status(session_id)
    return {
        "session_id": session_id,
        **status,
    }


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
