# duo2api — 项目进展记录

> GitLab Duo Chat → OpenAI-compatible API 代理

---

## 项目目标

把 GitLab Duo Chat 的 WebSocket 接口包装成 OpenAI `/v1/chat/completions` + `/v1/models` 格式，让 Cursor、ChatBox、Open WebUI 等工具可以直接接入。

原始参考项目：https://github.com/1icc0/gitlab2api

---

## 当前运行状态（截至 2026-06-24）

- ✅ FastAPI 服务运行在 Replit，端口 8000
- ✅ 18 个模型全部列出，ID 使用人类友好格式（破折号+点号）
- ✅ 流式输出（SSE）和非流式均可用
- ✅ API Key 鉴权（`Authorization: Bearer <key>`）
- ✅ 管理接口鉴权：`/v1/config` 读写必须使用已配置的 API Key
- ✅ Web 配置页不在 HTML 源码中渲染 Cookie / API Key
- ✅ 配置文件热加载（修改 config.json 无需重启）
- ✅ OpenAI `messages` 完整历史拼接
- ✅ 每请求独立 GitLab Duo workflow，避免跨窗口/并发串台
- ✅ 未知模型 ID 返回 `model_not_found`，不再透传上游
- ✅ 上游 HTTP / WebSocket / 响应等待超时保护
- ✅ `/healthz` 健康检查
- ✅ 上游错误对客户端脱敏，详细信息写服务端日志
- 🚧 工具调用（function calling）尚未实现
- 🚧 联网搜索内置提示词尚未实现

---

## 如何在 Replit 上创建此项目

### 1. 创建新 Repl

1. 打开 https://replit.com，点击 **Create Repl**
2. 选择模板：**Blank** 或 **Node.js**（后者方便配 pnpm 工作区）
3. 项目名随意，如 `gitlab-duo-proxy`

### 2. 安装 Python 3.12 + 依赖

Replit 用 Nix 管理系统包，在 Shell 里：

```bash
# 安装 Python 3.12（Replit Nix 环境）
# 通过 replit.nix 添加 python312 或直接在包管理面板搜索 python3

pip install fastapi uvicorn httpx websockets
```

> **坑 1**：Replit 默认 Python 可能是 3.10，需要手动在 Nix 配置里声明 `pkgs.python312`。

### 3. 上传 / 创建核心文件

```
gitlab_duo_client.py   # WebSocket 客户端 + 模型映射
server.py              # FastAPI 路由
context.py             # OpenAI messages 转 prompt
security.py            # 鉴权、脱敏、配置保护 helper
config.example.json    # 配置模板
config.json            # 本地凭据配置（已被 gitignore）
```

### 4. 配置 Workflow

在 `.replit-artifact/artifact.toml` 里配置服务：

```toml
[[services]]
localPort = 8000
name = "GitLab Duo Proxy"
paths = ["/"]
```

然后添加 Workflow（Replit 左侧工具栏 → Workflows）：

- **名称**：GitLab Duo Proxy
- **命令**：`cd artifacts/api-server && python3 src/gitlab2api_server.py`
- **端口**：8000

### 5. 填写 config.json

复制 `config.example.json` 为 `config.json`，填入：

```bash
cp config.example.json config.json
```

| 字段 | 说明 |
|------|------|
| `namespace_id` | GitLab **群组**的 namespace ID（见下方如何获取） |
| `_gitlab_session` | 浏览器 Cookie，F12 → Application → Cookies |
| `remember_user_token` | 同上，可为空 |
| `api_key` | 自定义 API Key，供客户端鉴权 |
| `model` | 默认模型，如 `claude-sonnet-4.5` |

---

## 关键踩坑记录

### 坑 1：必须用群组命名空间，不能用个人命名空间

GitLab Duo Chat 需要 **Ultimate** 计划才能使用。个人命名空间（Free 计划）的 namespace_id 请求会返回 403 或无法创建 workflow。

- **个人 namespace_id**（Free）：`135817729` → ❌ 不可用
- **群组 namespace_id**（Ultimate Trial）：`135817766`，群组名 `sx-group3` → ✅ 可用

获取群组 namespace_id 方法：
```
https://gitlab.com/api/v4/namespaces?search=<你的群组名>
```
找 `"kind": "group"` 的那条，取其 `id`。

### 坑 2：WebSocket URL 里的模型 ID 是下划线格式

GitLab 内部 API 用 `claude_sonnet_4_5`，但对外 OpenAI 接口我们改成了 `claude-sonnet-4.5`。
两套 ID 在 `gitlab_duo_client.py` 里用 `_MODEL_ID_MAP` 双向映射，`resolve_gitlab_model_id()` 做转换。

### 坑 3：config.json 路径在 Replit pnpm 工作区里比较深

服务的工作目录是 `artifacts/api-server/`，用 `Path(__file__).parent.parent / "config.json"` 定位。

### 坑 4：`_load_config()` 每次调用重新读文件

这是有意为之——修改 config.json 后无需重启服务即可生效。鉴权用的 `server.api_keys` 已加 5 秒 TTL 缓存，Web 配置保存后会主动清空缓存。

### 坑 5：WebSocket 每次对话都需要先创建 workflow，再开 session

GitLab Duo 的 WebSocket 握手流程：
1. `POST /ai/duo_chat/create_workflow` → 获取 `workflow_id`
2. `WSS /cable?namespace_id=...&model=...` → 升级连接
3. 发送订阅消息 + 用户消息
4. 解析 `ChunkAck` / `FinalAck` 收流

当前服务为每个 OpenAI 请求创建独立 `DuoChat` workflow。上下文来自客户端传入的完整 `messages`，避免服务端 checkpoint 在不同客户端窗口之间串台。

---

## 模型列表（18 个）

| 用户友好 ID | GitLab 内部 ID | 提供商 |
|-------------|----------------|--------|
| claude-haiku-4.5 | claude_haiku_4_5 | Anthropic |
| claude-sonnet-4.5 | claude_sonnet_4_5 | Anthropic |
| claude-sonnet-4.6 | claude_sonnet_4_6 | Anthropic |
| claude-opus-4.5 | claude_opus_4_5 | Anthropic |
| claude-opus-4.6 | claude_opus_4_6 | Anthropic |
| claude-opus-4.7 | claude_opus_4_7 | Anthropic |
| claude-opus-4.8 | claude_opus_4_8 | Anthropic |
| gemini-3.5-flash | gemini_3_5_flash | Google |
| gpt-5-mini | gpt_5_mini | OpenAI |
| gpt-5.1 | gpt_5_1 | OpenAI |
| gpt-5.2 | gpt_5_2 | OpenAI |
| gpt-5-codex | gpt_5_codex | OpenAI |
| gpt-5.2-codex | gpt_5_2_codex | OpenAI |
| gpt-5.3-codex | gpt_5_3_codex | OpenAI |
| gpt-5.4 | gpt_5_4 | OpenAI |
| gpt-5.4-mini | gpt_5_4_mini | OpenAI |
| gpt-5.4-nano | gpt_5_4_nano | OpenAI |
| gpt-5.5 | gpt_5_5 | OpenAI |

---

## API 使用方法

### 获取模型列表
```bash
curl https://<your-replit-domain>/v1/models \
  -H "Authorization: Bearer <api_key>"
```

### 普通对话
```bash
curl https://<your-replit-domain>/v1/chat/completions \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4.5",
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

### 流式对话
```bash
curl https://<your-replit-domain>/v1/chat/completions \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4.5",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'
```

### 在 Cursor / ChatBox 里配置
- Base URL：`https://<your-replit-domain>/v1`
- API Key：config.json 里的 `api_key`
- 模型：从列表里选任意一个

---

## 下一步计划

- [ ] **工具调用（function calling）**：解析 OpenAI tools 格式，转换为 system prompt 注入到 Duo Chat
- [ ] **联网搜索内置提示词**：system prompt 里注入搜索指令，让模型知道何时触发搜索
- [x] **Web 配置界面**：浏览器里直接修改 config.json（更新 Cookie、切换模型等）
- [x] **多 session 并发**：每个请求用独立 DuoChat 实例，支持并行对话
- [x] **对话历史**：把 messages 数组拼成多轮对话传给 Duo
- [x] **模型校验**：未知模型 ID 返回 OpenAI 风格 400 错误
- [ ] **Cookie 自动刷新**：检测 session 过期并提示用户更新
- [ ] **Docker 部署**：提供 Dockerfile，方便自托管

---

## 临时测试说明：上下文修复验证

本节用于云端 agent 验证 2026-06-24 的上下文修复。验证目标：服务端不再共享 GitLab checkpoint 作为聊天历史；每个请求使用客户端传来的完整 OpenAI `messages`；同一个客户端窗口切换模型后，只要客户端继续发送历史 messages，模型能看到前文；不同窗口或并发请求不会通过服务端 session 串台。

### 1. 本地自动测试

在仓库根目录运行：

```bash
python -m unittest test_context.py
python -m unittest test_security.py
python -m py_compile context.py security.py server.py gitlab_duo_client.py test_context.py test_security.py
```

期望结果：

```text
Ran 5 tests
OK
```

`py_compile` 命令应以退出码 `0` 结束，无语法错误输出。

`test_security.py` 期望结果：

```text
Ran 9 tests
OK
```

它覆盖：

- 配置状态只返回是否已配置和数量，不返回 Cookie / API Key 明文
- API key TTL 缓存可清理
- Web 表单里的遮罩值不会被写回配置
- 空 API Keys 保持原配置
- 中文 token 粗估按 UTF-8 字节计算
- 上游错误对客户端脱敏

### 2. 启动服务

确认 `config.json` 已填入有效字段：

- `gitlab.namespace_id`
- `gitlab.cookies._gitlab_session`
- `gitlab.cookies.remember_user_token`
- `server.api_keys`

启动：

```bash
python server.py
```

或：

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

### 3. 验证完整 messages 历史

发送一段显式多轮历史：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-custom-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4.5",
    "messages": [
      {"role": "user", "content": "记住关键词 blue-mango，请确认。"},
      {"role": "assistant", "content": "已确认，关键词是 blue-mango。"},
      {"role": "user", "content": "刚才的关键词？只回答关键词本身。"}
    ]
  }'
```

期望肉眼结果：返回内容包含 `blue-mango`。这说明代理把前面的 user/assistant 历史一起传给了 GitLab Duo。

### 4. 验证同窗口切模型继续上下文

用同一段历史，把 `model` 换成另一个可用模型，例如：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-custom-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5-codex",
    "messages": [
      {"role": "user", "content": "记住关键词 blue-mango，请确认。"},
      {"role": "assistant", "content": "已确认，关键词是 blue-mango。"},
      {"role": "user", "content": "刚才的关键词？只回答关键词本身。"}
    ]
  }'
```

期望肉眼结果：如果该模型在账号权限内可用，返回内容仍然包含 `blue-mango`。这说明上下文来自客户端 `messages`，不是来自某个模型自己的服务端 checkpoint。

### 5. 验证不同窗口不串台

模拟窗口 A：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-custom-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4.5",
    "messages": [
      {"role": "user", "content": "窗口 A 的关键词是 alpha-ctx，请确认。"},
      {"role": "assistant", "content": "已确认，窗口 A 的关键词是 alpha-ctx。"},
      {"role": "user", "content": "刚才窗口 A 的关键词？只回答关键词本身。"}
    ]
  }'
```

模拟窗口 B：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-custom-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4.5",
    "messages": [
      {"role": "user", "content": "窗口 B 的关键词是 beta-ctx，请确认。"},
      {"role": "assistant", "content": "已确认，窗口 B 的关键词是 beta-ctx。"},
      {"role": "user", "content": "刚才窗口 B 的关键词？只回答关键词本身。"}
    ]
  }'
```

期望肉眼结果：窗口 A 返回 `alpha-ctx`，窗口 B 返回 `beta-ctx`。窗口 B 的回答不应出现 `alpha-ctx`。

### 6. 验证服务重启后上下文恢复范围

重启服务后，重新发送第 3 步的完整 `messages` 请求。

期望肉眼结果：返回内容仍然能依据请求里的历史回答 `blue-mango`。这说明服务端重启不会影响客户端本次发来的可见历史。

注意：如果客户端本身只发送最近几条 messages，代理只能传递这几条。客户端已经裁剪掉的旧消息，代理无法恢复。

### 7. 验证管理接口安全

确认 `config.json` 里 `server.api_keys` 至少有一个 key，例如 `sk-your-custom-key`。

未带鉴权访问配置接口：

```bash
curl -i http://localhost:8000/v1/config
```

期望肉眼结果：HTTP 401，返回 OpenAI 风格 `invalid_api_key`。

带鉴权访问：

```bash
curl http://localhost:8000/v1/config \
  -H "Authorization: Bearer sk-your-custom-key"
```

期望肉眼结果：只看到 `namespace_id`、`model`、`has_session_cookie`、`has_remember_token`、`api_keys_count`、`available_models`。响应里没有 Cookie 明文和 API Key 明文。

检查管理页 HTML 源码：

```bash
curl http://localhost:8000/ | grep -E "_gitlab_session|remember_user_token|sk-your-custom-key|cell-"
```

期望肉眼结果：只可能看到字段名或说明文字，看不到真实 Cookie 值和真实 API Key 值。

验证健康检查：

```bash
curl http://localhost:8000/healthz
```

期望肉眼结果：

```json
{"ok":true,"service":"duo2api"}
```

验证遮罩值保护：

```bash
curl -i http://localhost:8000/v1/config \
  -H "Authorization: Bearer sk-your-custom-key" \
  -H "Content-Type: application/json" \
  -d '{
    "namespace_id": "135817766",
    "model": "claude-sonnet-4.5",
    "api_keys": ["sk-a...1234"]
  }'
```

期望肉眼结果：HTTP 400，提示提交完整 API Key 或留空。随后再次 `GET /v1/config`，`api_keys_count` 保持原数量。

---

## 文件结构

```
.
├── context.py                 # OpenAI messages 转 prompt
├── gitlab_duo_client.py       # WebSocket 客户端核心逻辑
├── security.py                # 鉴权、脱敏、配置保护 helper
├── server.py                  # FastAPI 入口
├── config.example.json        # 配置模板
├── config.json                # 本地运行凭据（gitignore）
├── requirements.txt           # Python 依赖
├── test_context.py            # 上下文测试
├── test_security.py           # 安全测试
└── PROGRESS.md                # 本文件
```
