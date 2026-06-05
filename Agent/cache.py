"""
缓存模块：检索结果缓存+食物查询缓存
复用一阶段改造好的Redis连接池
"""
import json
import hashlib
import time
import logging
import random
from typing import Optional

from Agent.memory.session_memory import _get_redis_client

logger = logging.getLogger(__name__)

#缓存默认 TTL设置（秒）
RETRIEVAL_CACHE_TTL = 600 #检索结果缓存10分钟
RETRIEVAL_CACHE_JITTER= 30 #缓存过期时间随机偏移30秒
EMPTY_CACHE_TTL= 60 #空缓存过期1分钟（防止穿透）

def _normalize_query(query:str)->str:
    """简单的查询规范化，去除多余空格，转换为小写"""
    return ' '.join(query.strip().lower().split())

class RetrievalCache:
    """
    检索结果缓存 Cache-Aside模式
    """
    def __init__(self,ttl:int=RETRIEVAL_CACHE_TTL):
        self.ttl = ttl
        self._hits = 0
        self._misses = 0
    
    def _cache_key(self,query:str)->str:
        """生成缓存键，使用查询的规范化文本的MD5哈希"""
        q = _normalize_query(query)
        digest = hashlib.md5(q.encode()).hexdigest() #转字节-转哈希-转32位16进制字符串
        return f"fit_agent:cache:retrieval:{digest}"
    
    def get(self,query:str,use_lock:bool=True)->Optional[list[dict]]:
        """"
        查缓存，命中返回结果列表，未命中返回None
        use_lock=True 时启动分布式锁防击穿
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
        
        #缓存未命中 -- 如果启用了锁防护，尝试获取回源锁
        if use_lock:
            from Agent.lock import get_cache_lock

            lock = get_cache_lock(query)
            if lock.acquire():
                #拿到锁 -- 我负责回源 （调用方负责回源后调set()）
                #不在这里释放锁。调用方回源+set后再释放
                #把锁对象存进实例变量，让调用方通过 release_lock() 方法释放
                self._pending_lock = lock
                self._misses += 1 #确实是miss了
                return None
            
            #没拿到锁 - 等一会再查缓存
            for _ in range(5):
                time.sleep(0.1)
                data = client.get(key)
                if data is not None:
                    self._hits += 1
                    return json.loads(data)
        
        #最终miss 没拿到锁，等了也没等到
        self._misses += 1
        return None

    def release_after_backfill(self,query:str)->None:
        """
        回源完成并写入缓存后，释放分布式锁（调用方在set()后调用）
        """
        if hasattr(self,'_pending_lock') and self._pending_lock:
            self._pending_lock.release()
            self._pending_lock = None
    
    def set(self,query:str,results:list[dict],is_empty:bool=False) -> None:
        """"
        写入缓存
        is_empty=True 表示查询无结果（空值缓存，短ttl防穿透）
        """
        client = _get_redis_client()
        if client is None:
            return
        
        key = self._cache_key(query)
        #ttl加随机抖动，避免缓存雪崩
        ttl = EMPTY_CACHE_TTL if is_empty else self.ttl
        ttl += random.randint(-RETRIEVAL_CACHE_JITTER, RETRIEVAL_CACHE_JITTER)
        ttl = max(ttl,10)

        try:
            client.setex(key,ttl,json.dumps(results,ensure_ascii=False))
        except Exception :
            logger.warning(f"Failed to write retrieval cache for key={key}",exc_info=True)

    def get_stats(self) -> dict:
        """获取缓存命中率统计"""
        total = self._hits + self._misses
        hit_rate = self._hits / total if total > 0 else 0.0
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(hit_rate,3),
        }

#全局单例
_retrieval_cache = RetrievalCache()

def get_retrieval_cache() -> RetrievalCache:
    """获取全局检索缓存实例"""
    return _retrieval_cache


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


