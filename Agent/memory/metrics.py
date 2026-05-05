"""
Redis 操作指标收集模块。
记录每次Redis操作的耗时，慢查询告警，并且提供全局统计
"""
import time
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

SLOW_QUERY_THRESHOLD_MS = 50

# 全局计数器（进程内存，非线程安全，仅用于大致统计）
_global_stats = {
    "total_ops": 0,
    "slow_queries": 0,
    "recent_slow":[],
}

def record_redis_op(
        session_id:str,
        op_name:str,
        elapsed_ms:float,
        client:Optional[object]=None,
):
    # 更新全局统计
    """
    记录一次Redis操作的指标，包括：
    - session_id：当前会话ID
    - op_name：Redis操作名称（如"append_turn"等）
    - elapsed_ms：操作耗时（毫秒）
    - client：Redis客户端对象（可选）
    用于统计全局Redis操作指标，包括总操作数、慢查询数、最近慢查询记录等。
    """
    global _global_stats
    _global_stats["total_ops"] += 1

    if elapsed_ms > SLOW_QUERY_THRESHOLD_MS:
        _global_stats["slow_queries"] += 1
        _global_stats["recent_slow"].append({
            "session_id": session_id,
            "op_name": op_name,
            "elapsed_ms": elapsed_ms,
        })
        # 限制recent_slow列表的大小
        if len(_global_stats["recent_slow"]) > 20:
            _global_stats["recent_slow"] = _global_stats["recent_slow"][-20:]
        logger.warning(
            f"Slow Redis:{op_name} took {elapsed_ms:.1f}ms (session={session_id [:8]}...)"
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
def get_global_stats()->dict:
    """返回全局统计信息"""
    return{
        "total_redis_ops": _global_stats["total_ops"],
        "slow_queries": _global_stats["slow_queries"],
        "recent_slow_queries": _global_stats["recent_slow"][-10:],
    }