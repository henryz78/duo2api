# GitLab Duo OpenAI Proxy

将 GitLab Duo Chat 包装成 OpenAI 兼容 API，让 ChatBox、Open WebUI、Cursor、Continue、Python openai SDK、Codex CLI 等客户端直接接入 GitLab Duo。

---

## 原理

GitLab Duo Chat 的底层通信协议是 WebSocket，流程如下：

```
客户端
  │
  ├─ POST /api/v4/ai/duo_workflows/workflows   创建 workflow，获得 workflow_id
  │
  └─ WSS  /api/v4/ai/duo_workflows/ws          建立 WebSocket 连接
       │
       ├─→ startRequest { goal, ... }               发送本次完整 prompt
       │
       └─← newCheckpoint { status, checkpoint }     流式接收回复
              │
              ├─ status=CREATED        内容还在生成中（checkpoint 里携带累积内容）
              ├─ status=INPUT_REQUIRED 本轮生成完毕，等待下一条用户消息
              └─ status=FAILED        出错
```

本项目在此协议之上加了一层 FastAPI 服务，对外暴露标准 OpenAI REST 接口，做了以下映射：

| OpenAI 概念 | GitLab Duo 实现 |
|---|---|
| 对话历史 | 每次请求完整拼接 OpenAI `messages` |
| system/user/assistant/tool message | 拼接为带角色标签的 prompt |
| stream=true | 逐 chunk 转发 SSE |
| stream=false | 等待 `INPUT_REQUIRED` 后一次性返回 |
| Responses API | `/v1/responses` 最小 SSE 兼容层 |
| Codex CLI tool call | GitLab Duo 原生工具桥接为本地 `exec_command` |
| Bearer API Key | 本地校验，不透传给 GitLab |

---

## 文件说明

```
config.example.json    配置模板
config.json            本地运行配置（cookies、api_keys、服务地址，已被 gitignore）
context.py             OpenAI messages / tools 转 prompt
gitlab_duo_client.py   WebSocket 客户端核心逻辑
model_catalog.py       GitLab GraphQL 模型列表归一化
responses_api.py       OpenAI Responses API / Codex CLI 兼容层
security.py            鉴权、脱敏、配置保护 helper
server.py              FastAPI 服务，暴露 OpenAI 兼容接口
```

---

## 快速开始

### 1. 安装依赖

```bash
python -m pip install -r requirements.txt
```

### 2. 获取 GitLab Cookie

用浏览器登录 [gitlab.com](https://gitlab.com)，打开开发者工具 → Application → Cookies，复制：

- `_gitlab_session`
- `remember_user_token`（可为空，当前验证中仅 `_gitlab_session` 也能通过）

### 3. 获取 namespace_id

建议使用 GitLab Ultimate Trial 群组 namespace。可以通过 API 查询：

```bash
curl -s "https://gitlab.com/api/v4/namespaces?search=你的群组名" \
  -H "Cookie: _gitlab_session=..." | python3 -m json.tool
```

选择 `"kind": "group"` 的记录，取它的 `id` 作为 `namespace_id`。

### 4. 创建并编辑 config.json

```bash
cp config.example.json config.json
```

```json
{
  "gitlab": {
    "host": "https://gitlab.com",
    "namespace_id": "你的 namespace_id",
    "model": "claude-sonnet-4.6",
    "cookies": {
      "_gitlab_session": "粘贴 cookie 值",
      "remember_user_token": ""
    },
    "user_agent": "Mozilla/5.0 ..."
  },
  "server": {
    "host": "0.0.0.0",
    "port": 8000,
    "api_keys": [
      "sk-your-custom-key"
    ]
  }
}
```

`config.json` 保存真实 Cookie 和 API Key，本仓库只提交 `config.example.json` 模板。`api_keys` 留空数组 `[]` 表示聊天接口不鉴权（仅建议本地使用），管理配置接口需要至少一个 API Key。

### 5. 启动服务

```bash
python3 server.py
# 或
uvicorn server:app --host 0.0.0.0 --port 8000
```

启动后用下面两条命令验证服务和 GitLab 鉴权：

```bash
curl http://localhost:8000/healthz
curl "http://localhost:8000/v1/gitlab/health?deep=true" \
  -H "Authorization: Bearer sk-your-custom-key"
```

---

## API 接口

### GET /v1/models

```bash
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer sk-your-custom-key"
```

`/v1/models` 会优先通过 GitLab GraphQL `aiChatAvailableModels` 读取当前账号实际可用模型，结果缓存 5 分钟；GraphQL 请求失败时使用内置 fallback 列表。

### GET /v1/gitlab/health

```bash
curl "http://localhost:8000/v1/gitlab/health?deep=true" \
  -H "Authorization: Bearer sk-your-custom-key"
```

`deep=true` 会验证 GitLab Cookie 能获取 CSRF，并创建一次 Duo workflow 来确认 namespace 权限。

### POST /v1/responses

`/v1/responses` 提供 OpenAI Responses API 的最小 SSE 兼容层，主要用于 Codex CLI 这类 agent 客户端。当前已支持：

- `response.created` / `response.output_item.added` / `response.output_text.delta` / `response.completed`
- `response.function_call_arguments.delta` / `response.function_call_arguments.done`
- Responses 风格 `function_call` SSE
- Codex CLI 风格 `exec_command(cmd=...)`
- GitLab Duo 原生 `create_file_with_contents` / `run_command` 到 `exec_command` 的桥接
- 重复成功命令拦截，避免同一条本地命令反复执行
- 未识别 GitLab Duo 原生工具的脱敏诊断：只返回 `name` 和 `args_keys`

Codex CLI 推荐模型：

| 等级 | 模型 | 说明 |
|---|---|---|
| A | `claude-sonnet-4.6` | 实测最快、POST 次数少、token 消耗低 |
| A | `gpt-5.5` | 基准模型，真实编程任务稳定 |
| A | `gpt-5.4-mini` | 成本较低，轻量任务表现好 |
| B | `gpt-5.4` | 可用，曾出现 shell 转义错误并自动恢复 |
| B | `claude-sonnet-4.6-vertex` | 可用，偶发 WebSocket reconnect |
| C | `gemini-3.5-flash` | 可用，轮次和 token 消耗较高 |
| D | `gpt-5-codex` | Codex CLI metadata 不匹配，当前不推荐 |

### POST /v1/chat/completions

**非流式：**

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-custom-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4.6",
    "messages": [
      {"role": "system", "content": "你是一个代码专家"},
      {"role": "user", "content": "用 Python 写一个快速排序"}
    ]
}'
```

**工具调用（prompt 模拟）：**

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-custom-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4.6",
    "messages": [{"role": "user", "content": "调用 get_time 工具"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_time",
        "description": "Get current server time",
        "parameters": {"type": "object", "properties": {}}
      }
    }],
    "tool_choice": "auto"
  }'
```

GitLab Duo 未暴露外部自定义工具 schema。本项目会把 OpenAI `tools` / `tool_choice` 序列化进 prompt，引导模型输出 JSON tool_calls，再包装成 OpenAI `tool_calls` 响应。

**流式：**

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-custom-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4.6",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'
```

---

## 接入第三方客户端

所有客户端填写以下参数：

| 参数 | 值 |
|---|---|
| API Base URL | `http://localhost:8000/v1` |
| API Key | config.json 里设置的 key |
| 模型 | 从 `/v1/models` 返回列表中选择 |

兼容范围：

| 客户端类型 | 当前状态 |
|---|---|
| 普通 OpenAI Chat Completions 客户端 | 支持非流式、流式、模型列表 |
| Chat Completions tools 客户端 | 支持 `tools` / `tool_choice` prompt 模拟 |
| Codex CLI | 支持 `/v1/responses` SSE、`exec_command(cmd=...)` 和真实编程任务 |
| 其他 Responses API 客户端 | 支持文本 SSE 与 function_call SSE；工具 schema 差异较大时按客户端补 normalization |

### Python openai SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="sk-your-custom-key",
)

# 非流式
resp = client.chat.completions.create(
    model="claude-sonnet-4.6",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)

# 流式
for chunk in client.chat.completions.create(
    model="claude-sonnet-4.6",
    messages=[{"role": "user", "content": "你好"}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

### ChatBox / Open WebUI / Cursor

在设置中将 OpenAI API 地址改为 `http://localhost:8000/v1`，填入 API Key 即可，其余使用方式与官方 OpenAI 完全一致。

### Codex CLI

Codex CLI 需要使用 `/v1/responses`。配置自定义 provider 时，将 base URL 指向：

```text
http://localhost:8000/v1
```

配置要点：

- provider 使用 OpenAI-compatible Responses API。
- API Key 使用 `config.json` 里的 `server.api_keys`。
- 模型优先选 `claude-sonnet-4.6`、`gpt-5.5`、`gpt-5.4-mini`。
- Codex CLI 的 `exec_command` schema 使用 `cmd` 字段，本项目会按客户端 schema 自动输出正确字段名。

推荐模型：

```text
claude-sonnet-4.6
gpt-5.5
gpt-5.4-mini
```

已验证任务类型：

- 创建文件并运行 Python
- 多文件 `add.py` + `test_add.py` + pytest
- 读取失败测试、修复代码、重新运行测试
- 目录检查与小改动
- 多步 shell 命令

当前稳定验证点：

```text
4b42fb1 fix: report unsupported duo tool info
```

---

## 注意事项

- **Cookie 有效期**：`remember_user_token` 通常有效期约 2 周，过期后需重新获取。
- **Session Cookie**：当前验证中 `_gitlab_session` 单独可用，Cookie 失效时用 `/v1/gitlab/health?deep=true` 检查。
- **对话历史**：服务每次使用客户端发来的完整 `messages` 作为上下文，不在服务端共享聊天历史。
- **并发隔离**：每个请求创建独立 GitLab Duo workflow，不同客户端窗口不会通过服务端 session 串台。
- **System prompt 限制**：GitLab Duo 可能拒绝执行自定义 system-like 指令；普通 user/assistant 历史会正常作为上下文传递。
- **模型选择**：GitLab Duo 支持的模型取决于账户订阅等级，`/v1/models` 会按当前账号动态返回可用列表。
- **Codex CLI 模型选择**：优先使用 `claude-sonnet-4.6`、`gpt-5.5`、`gpt-5.4-mini`。`gpt-5-codex` 当前不推荐。
- **未知 Duo 工具**：当前桥接覆盖 `create_file_with_contents` 和 `run_command`。未来 GitLab Duo 新增 raw `tool_info` 时，服务会返回脱敏诊断 `Unsupported GitLab Duo tool_info received. name=... args_keys=[...]`，再按工具名补映射。

---

## duo2api 修改说明（fork from [1icc0/gitlab2api](https://github.com/1icc0/gitlab2api)）

本 fork 在原项目基础上做了以下改动：

### 1. 模型 ID 格式：破折号 + 点号

原项目使用下划线 `claude_sonnet_4_5`，本 fork 改为更通用的格式 `claude-sonnet-4.5`（兼容 OpenAI 官方命名风格）。
旧格式仍然被接受，会自动映射到对应的 GitLab 内部 ID。

### 2. 动态模型列表

`GET /v1/models` 会查询 GitLab 网页端同源 GraphQL 字段 `aiChatAvailableModels`，把 `selectableModels` 转成 OpenAI-compatible 模型列表。模型列表缓存 5 分钟，GitLab 请求失败时使用内置 `ALL_MODELS` 作为 fallback。旧的破折号 ID、GitLab 下划线 ID、GraphQL 完整 `ref` 都会被识别并解析到 GitLab 可用的 `user_selected_model_identifier`。

### 3. Web 配置界面

访问服务根路径 `/` 可看到一个暗色主题的配置页面。页面不会把 Cookie 或 API Key 渲染进 HTML 源码；需要先输入已配置的 API Key，页面才会通过 `/v1/config` 读取配置状态并保存修改。`/v1/config` 读写接口强制要求 `server.api_keys` 已配置并通过 Bearer 鉴权。

### 4. 动态配置热加载

每次请求时重新读取 `config.json`，修改配置文件后立即生效，无需重启进程。
鉴权热路径会缓存 `server.api_keys` 5 秒，Web 配置保存后会主动清空缓存。

### 5. OpenAI messages 主历史

请求会把 `system`、`user`、`assistant`、`tool` 等 OpenAI messages 完整拼成 prompt。上下文由客户端会话历史决定，同一窗口换模型也能继续使用客户端传来的历史。

### 6. 每请求独立 GitLab Duo workflow

服务端不复用 GitLab checkpoint 作为主历史，避免不同客户端窗口或并发请求共享上下文。

### 7. 生产安全与可靠性

- `/healthz` 提供健康检查。
- `/v1/gitlab/health?deep=true` 提供带鉴权的 GitLab Cookie 与 Duo workflow 主动检测。
- GitLab HTTP 请求、WebSocket 握手和回复等待都设置了超时，避免上游卡住时长期占用连接。
- 上游详细错误写入服务端日志，客户端只收到脱敏后的 OpenAI 风格错误。
- token 粗估改为按 UTF-8 字节计算，对中文上下文更接近真实消耗。

### 8. 工具调用兼容层

GitLab Duo WebSocket 当前只暴露内部工具字段，外部自定义工具 schema 通过 prompt 模拟兼容 OpenAI `tools`。非流式请求会返回标准 `message.tool_calls`；流式请求在工具场景下会先等待完整上游回复，再发送 `delta.tool_calls` 和 `finish_reason=tool_calls`。

### 9. Codex CLI Responses 兼容层

`/v1/responses` 已实现 Codex CLI 所需的最小 SSE 协议，并针对 Codex CLI 做了以下兼容：

- 过滤 Codex CLI 内置的无名工具，只保留有函数名的 tools。
- 按客户端 tool schema 输出参数名，例如 `exec_command(cmd=...)`。
- 捕获 GitLab Duo 的 `TOOL_CALL_APPROVAL_REQUIRED`，把 `create_file_with_contents` 和 `run_command` 桥接为本地 `exec_command`。
- 当模型在第二轮重复写已经创建的 `.py` 文件时，按历史剩余任务改写为 `python3 <file>.py`。
- 当同一条命令已经成功执行并返回有效输出时，直接返回最终文本，避免重复执行和 token 浪费。
- 当 GitLab Duo 返回新的未知 `tool_info` 时，返回脱敏诊断，只暴露工具名和参数 key。

真实 Codex CLI 验收结果：

| 任务 | 结果 |
|---|---|
| `hello.py` 创建并运行 | 通过，输出 `CODEX_GPT55_OK` |
| `add.py` + `test_add.py` + pytest | 通过，3 tests passed |
| 修复失败测试 | 通过，`//` 修为 `/` 后测试通过 |
| 多步文件写入与读取 | 通过 |

当前推荐稳定点：`4b42fb1 fix: report unsupported duo tool info`。该提交在 Codex CLI 稳定链路上增加了未知 Duo 工具诊断，已验证的 `create_file_with_contents` / `run_command` 桥接路径保持通过。
