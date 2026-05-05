# TODO: 多租户隔离修复

> 每次开始新任务前，先读此文件确认进度。

---

## 背景：原始安全状态

一个面向市场的健身助手 Agent，6 个 API 端点全部无鉴权：

| 端点 | 风险 |
|------|------|
| `POST /chat` | body 可任意指定 `session_id`，读写他人会话 |
| `POST /chat/stream` | 同上 |
| `POST /clear-session` | 可清空任意会话 |
| `POST /compress-retry` | 可操作任意会话 |
| `POST /compress-discard` | 可丢弃任意会话备份 |
| `GET /compress-status` | query param 可查任意会话状态 |
| CORS | `allow_origins=["*"]` |

---

## 问题 1: 客户端可直接注入 session_id

**问题是什么：**
`POST /chat` 和 `/chat/stream` 的请求体 `ChatRequest` 允许客户端传入 `session_id`，后端优先使用请求体的值。任何人通过请求体指定他人的 session_id 就能以他人身份对话，读写其短期记忆。

**实际场景：**
> 用户 A 正在跟健身助手聊训练计划，已经提供了自己的身高体重和偏好。攻击者 B 从某个渠道（如分享链接、浏览器日志）得知了 A 的 session_id，然后发了一个 POST 请求：
> ```json
> {"message": "你好，我是用户 A，计划改为增肌", "session_id": "a-real-session-id"}
> ```
> 服务器接受 B 指定的 session_id，以 A 的身份追加对话到 A 的短期记忆，污染 A 的上下文。后续 A 继续聊天时，模型会读到被污染的假对话。

**如何修复：**
- 第 3 项：`ChatRequest.session_id` 字段删除，session_id 只能从 httponly Cookie 获取
- 第 4 项：所有管理端点（clear-session 等）的 session_id 同样从 Cookie 读取

---

## 问题 2: 会话归属无校验

**问题是什么：**
即使 session_id 只从 Cookie 来，Cookie 的值仍然可以被客户端修改。需要一个"只有服务端知道的秘密"来证明你是这个 session 的主人。第一轮用 `X-User-ID` 请求头做归属校验，但这个头是客户端自填的——攻击者可以随便写 `X-User-ID: userA` 伪装成任何人。

**实际场景：**
> 攻击者 B 手动在浏览器里改了 Cookie，把 session_id 设成了 A 的会话 ID。然后请求时加上 `X-User-ID: userA`。服务器校验：请求头的 userA == Redis 里存的 owner userA，通过。B 成功以 A 的身份操作 A 的会话。

**第一轮尝试（保留记录）：**
- 第 1 项：在 Redis 中存储 `owner_user_id` 字段
- 第 2 项：新增 `validate_session_ownership()` 校验归属
- **残留问题**：X-User-ID 是不可信输入，攻击者伪造相同值即可绕过

**最终修复（第二轮）：**
- 第 A 项：用服务端生成的随机 `session_token` 替代客户端自报的 `X-User-ID`。token 是 `secrets.token_urlsafe(32)` 生成的 43 字符随机串，攻击者无法猜测
- 第 B/C 项：所有端点统一调用 `resolve_session()` 校验 token，token 通过 httponly Cookie 下发，JavaScript 无法读取，即使 XSS 也无法窃取

---

## 问题 3: CORS 全开放

**问题是什么：**
`allow_origins=["*"]` 允许任意域名的网页跨域请求 API。任何恶意网站都可以偷偷向你的服务器发请求，如果用户浏览器中存了有效 Cookie，这些请求会被自动带上。

**实际场景：**
> 用户 A 在某恶意网站浏览，该网站的 JS 偷偷向 `http://your-api.com/chat` 发了一个 POST 请求。由于 Cookie 是 samesite=lax，POST 请求不会自动带上 Cookie，但 `/clear-session` 这类操作在某些条件下仍可能被利用。

**如何修复：**
- 第 5 项：`allow_origins` 改为具体的前端域名 `["http://localhost:5173"]`（部署时替换为生产域名）

---

## 问题 4: 旧会话迁移竞态窗口

**问题是什么：**
在实现 session_token 方案后，历史创建的会话（token 字段为空）存在"谁先请求谁绑定"的竞态窗口。`validate_session_token` 检测到 `stored_token is None` 时自动生成新 token 并写回 Cookie。攻击者只要知道一个历史 session_id，就可以抢先发请求完成 token 绑定，劫持该会话。

**实际场景：**
> 代码升级前，用户 A 创建了一个会话，session_id=`abc123`，此时 Redis 中没有 session_token 字段。代码升级后，攻击者 B 设法获得了 `abc123`，发了一个请求。服务器发现该会话没有 token，自动生成了一个 `xyz789`，通过 Set-Cookie 返回给 B。B 现在拥有了这个会话的"密码"，后续正常访问。而 A 的发来的请求（也没有 token）就会收到 403 被拒绝。

**如何修复：**
- 第 E 项：关闭自动迁移。旧会话没有 token → 直接返回 401 "Session expired"，不生成 token 不写 Cookie。旧会话数据变成孤岛（无法访问），用户需创建新会话。杜绝"先到先得"的抢占窗口。

---

## 修复总览

| 编号 | 问题 | 场景概述 | 修复方式 |
|------|------|---------|---------|
| 3 | session_id 可注入 | B 在请求体指定 A 的会话 ID 冒充 A | session_id 仅从 Cookie 读取 |
| 4 | 管理端点可越权 | B 直接 POST /clear-session 清空 A 的数据 | 管理端点统一从 Cookie 取 session_id |
| 1+2→A+B+C | X-User-ID 不可信 | B 伪造请求头 `X-User-ID: userA` 绕过校验 | 替换为服务端生成 session_token |
| 5 | CORS 全开放 | 恶意网站可跨域请求 API | 限制为具体前端域名 |
| E | 旧会话迁移竞态 | B 抢先请求旧会话完成 token 绑定 | 关闭自动迁移，旧会话直接拒绝 |

---

## 待完成

- [ ] **D. 端到端验证**（启动服务 + 运行 test_auth.sh）
