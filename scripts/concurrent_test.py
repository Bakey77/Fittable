"""
并发测试脚本：同时发 15 个请求，趁它们还在跑时观察 /health 的 queue_size。
用法：先启动后端（python -m backend.main），再开另一个终端跑这个脚本。
"""
import time
import json
import threading
import urllib.request

BASE = "http://localhost:8000"
CONCURRENT = 15
results = []


def send_one(i):
    """发一个 /chat 请求，记录开始和结束时间"""
    start = time.monotonic()
    try:
        data = json.dumps({"message": f"并发测试第{i}条：你好"}).encode()
        req = urllib.request.Request(
            f"{BASE}/chat",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=60)
        body = json.loads(resp.read())
        elapsed = time.monotonic() - start
        results.append((i, "OK", elapsed, body.get("primary_intent", "")))
        print(f"[请求{i:2d}] 完成, 耗时 {elapsed:.1f}s, intent={body.get('primary_intent', '')}")
    except Exception as e:
        elapsed = time.monotonic() - start
        results.append((i, "FAIL", elapsed, str(e)[:50]))
        print(f"[请求{i:2d}] 失败, 耗时 {elapsed:.1f}s, 错误={e}")


def poll_health():
    """持续查询 /health，直到被主线程结束"""
    while not stop_flag.is_set():
        try:
            resp = urllib.request.urlopen(f"{BASE}/health", timeout=3)
            h = json.loads(resp.read())
            tp = h.get("thread_pool", {})
            print(f"  [health] active={tp.get('active_threads')}, "
                  f"queue={tp.get('queue_size')}, "
                  f"done={tp.get('tasks_completed')}")
        except Exception:
            pass
        time.sleep(0.3)


# 启动 health 轮询线程
stop_flag = threading.Event()
health_thread = threading.Thread(target=poll_health, daemon=True)

print(f"发送 {CONCURRENT} 个并发请求（线程池上限=10）...\n")
start_time = time.monotonic()
health_thread.start()

# 并发发送：每个请求占一个线程
threads = []
for i in range(CONCURRENT):
    t = threading.Thread(target=send_one, args=(i,))
    t.start()
    threads.append(t)
    time.sleep(0.05)  # 错开 50ms，避免同时建立连接

# 等所有请求完成
for t in threads:
    t.join()

stop_flag.set()
total = time.monotonic() - start_time

# 汇总
print(f"\n{'='*50}")
print(f"全部完成，总耗时 {total:.1f}s")
ok = [r for r in results if r[1] == "OK"]
fail = [r for r in results if r[1] != "OK"]
print(f"成功: {len(ok)}, 失败: {len(fail)}")
if ok:
    times = [r[2] for r in ok]
    print(f"单个最快: {min(times):.1f}s, 最慢: {max(times):.1f}s, 平均: {sum(times)/len(times):.1f}s")
