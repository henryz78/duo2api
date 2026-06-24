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
- ✅ 配置文件热加载（修改 config.json 无需重启）
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
src/
  gitlab_duo_client.py   # WebSocket 客户端 + 模型映射
  gitlab2api_server.py   # FastAPI 路由
config.json              # 凭据配置（见 config.example.json）
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

这是有意为之——修改 config.json 后无需重启服务即可生效。但高并发时有轻微 I/O 开销，生产环境可考虑加缓存。

### 坑 5：WebSocket 每次对话都需要先创建 workflow，再开 session

GitLab Duo 的 WebSocket 握手流程：
1. `POST /ai/duo_chat/create_workflow` → 获取 `workflow_id`
2. `WSS /cable?namespace_id=...&model=...` → 升级连接
3. 发送订阅消息 + 用户消息
4. 解析 `ChunkAck` / `FinalAck` 收流

每个 `DuoChat` 实例只初始化一次 workflow_id，之后复用。

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
- [ ] **Web 配置界面**：浏览器里直接修改 config.json（更新 Cookie、切换模型等）
- [ ] **多 session 并发**：每个请求用独立 DuoChat 实例，支持并行对话
- [ ] **对话历史**：把 messages 数组拼成多轮对话传给 Duo（目前只传最后一条）
- [ ] **Cookie 自动刷新**：检测 session 过期并提示用户更新
- [ ] **Docker 部署**：提供 Dockerfile，方便自托管

---

## 文件结构

```
.
├── src/
│   ├── gitlab_duo_client.py   # WebSocket 客户端核心逻辑
│   └── gitlab2api_server.py   # FastAPI 入口
├── config.json                # 运行时凭据（不提交到 git）
├── config.example.json        # 配置模板
├── requirements.txt           # Python 依赖
└── PROGRESS.md                # 本文件
```
