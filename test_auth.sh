#!/bin/bash
# ============================================================
# 多租户隔离测试脚本 (session_token 版)
# 用法:
#   1. 启动服务: cd backend && python main.py
#   2. 运行本脚本: bash test_auth.sh
# ============================================================
BASE="http://localhost:8000"

TMP_COOKIE_A=/tmp/cookie_userA.txt
TMP_COOKIE_B=/tmp/cookie_userB.txt
rm -f $TMP_COOKIE_A $TMP_COOKIE_B

echo "=============================================="
echo "测试 1: 首次请求（无 Cookie）→ 期望 200"
echo "        服务器自动创建 session_id + session_token"
echo "=============================================="
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"你好"}' \
  -c $TMP_COOKIE_A
echo ""
echo "Cookie 内容:"
cat $TMP_COOKIE_A

echo ""
echo "=============================================="
echo "测试 2: 带正确 Cookie 再次请求 → 期望 200"
echo "=============================================="
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"你是谁"}' \
  -b $TMP_COOKIE_A

echo ""
echo "=============================================="
echo "测试 3: 只有 session_id 没有 session_token → 期望 403"
echo "=============================================="
# 手动构造：只传 session_id Cookie，不传 session_token
SID=$(grep session_id $TMP_COOKIE_A | awk '{print $NF}')
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/chat" \
  -H "Content-Type: application/json" \
  -H "Cookie: session_id=$SID" \
  -d '{"message":"越权?"}'

echo ""
echo "=============================================="
echo "测试 4: 伪造 session_token → 期望 403"
echo "=============================================="
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/chat" \
  -H "Content-Type: application/json" \
  -H "Cookie: session_id=$SID; session_token=fake-token-12345" \
  -d '{"message":"越权?"}'

echo ""
echo "=============================================="
echo "测试 5: 另一个用户（新的 session）→ 期望 200"
echo "        两个 session 的 token 不同，互相隔离"
echo "=============================================="
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"你好"}' \
  -c $TMP_COOKIE_B
echo ""
echo "用户A token: $(grep session_token $TMP_COOKIE_A | awk '{print $NF}')"
echo "用户B token: $(grep session_token $TMP_COOKIE_B | awk '{print $NF}')"
echo "（两个 token 应该不同）"

echo ""
echo "=============================================="
echo "测试 6: 管理端点 /clear-session 带正确 Cookie → 期望 200"
echo "=============================================="
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/clear-session" \
  -H "Content-Type: application/json" \
  -d '{}' \
  -b $TMP_COOKIE_B

echo ""
echo "=============================================="
echo "测试 7: /health 无需鉴权 → 期望 200"
echo "=============================================="
curl -s -w "\nHTTP %{http_code}\n" "$BASE/health"

echo ""
echo "=============================================="
echo "测试 8: /compress-status 带正确 Cookie → 期望 200"
echo "=============================================="
curl -s -w "\nHTTP %{http_code}\n" "$BASE/compress-status" \
  -b $TMP_COOKIE_A

# 清理
rm -f $TMP_COOKIE_A $TMP_COOKIE_B
