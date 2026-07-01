# duo2api — 项目进展记录

> GitLab Duo Chat → OpenAI-compatible API 代理

---

## 项目目标

把 GitLab Duo Chat 的 WebSocket 接口包装成 OpenAI `/v1/chat/completions` + `/v1/models` 格式，让 Cursor、ChatBox、Open WebUI 等工具可以直接接入。

原始参考项目：https://github.com/1icc0/gitlab2api

---

## 当前运行状态（截至 2026-06-25）

- ✅ FastAPI 服务运行在 Replit，端口 8000
- ✅ 模型列表通过 GitLab GraphQL 动态获取，当前账号探测到 31 个 selectable models
- ✅ 模型 ID 兼容破折号 ID、GitLab 下划线 ID、GraphQL 完整 ref
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
- ✅ `/v1/gitlab/health?deep=true` 主动检测 GitLab Cookie 与 Duo workflow
- ✅ 上游错误对客户端脱敏，详细信息写服务端日志
- ✅ `/v1/status` 本地诊断端点，支持 `deep=true` GitLab 鉴权检查
- ✅ `/v1/models/{model}` 单模型查询，支持 friendly ID、GitLab ID 与 alias
- ✅ 工具调用兼容层：OpenAI `tools/tool_choice` 通过 prompt 模拟，响应包装为 `tool_calls`
- ✅ `/v1/responses` 最小 SSE / JSON 兼容层，Codex CLI 可连接并执行本地命令
- ✅ Responses SSE 已补齐 `response.in_progress`、`content_part.added/done`、`output_text.done`
- ✅ Chat Completions 已兼容 `stream_options.include_usage`、`max_completion_tokens`、`response_format`、`parallel_tool_calls`
- ✅ Chat Completions 已兼容旧版 `functions/function_call` 工具参数和旧版 `message.function_call` 响应形状
- ✅ Chat/Responses 会把 `response_format` / `text.format` / token limit 转成 prompt 约束
- ✅ Chat Completions 非流式文本响应会应用 `stop` 截断
- ✅ Codex CLI `exec_command(cmd=...)` 参数兼容，`missing field 'cmd'` 已修复
- ✅ GitLab Duo 原生 `create_file_with_contents` / `run_command` 已桥接到 Codex CLI `exec_command`
- ✅ 重复写文件保护：已写入 `.py` 后，下一步会推进到运行文件
- ✅ 重复成功命令拦截：同一条成功命令不会反复执行
- ✅ Codex CLI 真实编程任务验收通过：创建文件、运行命令、pytest、修代码、多步任务
- ✅ 未知 GitLab Duo 原生 `tool_info` 会返回脱敏诊断，只暴露工具名和参数 key
- ✅ Dockerfile + docker-compose.yml 已提供，支持挂载本地 `config.json` 一键启动
- ✅ `scripts/openai_compat_smoke.py` 可快速验收模型、Chat Completions 与 Responses API 兼容性
- ✅ GitHub Actions CI 已加入，自动跑依赖安装、py_compile 与单元测试
- 🚧 联网搜索内置提示词尚未实现

---

## 如何在 Replit 上创建此项目

### 1. 创建新 Repl

1. 打开 https://replit.com，点击 **Create Repl**
2. 选择模板：**Blank** 或 **Node.js**（后者方便配 pnpm 工作区）
3. 项目名随意，如 `gitlab-duo-proxy`

### 2. 安装 Python 3.12

Replit 用 Nix 管理系统包，在 Shell 里：

通过 replit.nix 添加 `python312`，或直接在包管理面板搜索 `python3`。进入 Shell 后确认版本：

```bash
python --version
```

> **坑 1**：Replit 默认 Python 可能是 3.10，需要手动在 Nix 配置里声明 `pkgs.python312`。

### 3. 拉取项目

```bash
git clone https://github.com/henryz78/duo2api.git
cd duo2api
python -m pip install -r requirements.txt
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
- **命令**：`python3 server.py`
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
| `model` | 默认模型，如 `claude-sonnet-4.6` |

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

### 坑 2：网页端模型列表来自 GraphQL

GitLab 网页端通过 `/api/graphql` 的 `aiChatAvailableModels` 获取当前账号可用模型：

```graphql
aiChatAvailableModels(rootNamespaceId, namespaceId) {
  selectableModels { ref name modelProvider modelDescription costIndicator }
  defaultModel { name ref modelProvider }
  pinnedModel { ref name }
}
```

`ref` 可直接作为 WebSocket URL 里的 `user_selected_model_identifier`。服务对外保留 `claude-sonnet-4.5` 这类 OpenAI-friendly ID，同时接受 GitLab 下划线 ID 和 GraphQL 完整 ref。

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

## 模型列表

`/v1/models` 会优先查询 GitLab GraphQL `aiChatAvailableModels`，把当前账号返回的 `selectableModels` 转成 OpenAI-compatible 列表。结果缓存 5 分钟，GraphQL 失败时使用内置 `ALL_MODELS` fallback。

当前探测账号返回 31 个模型，包含 Anthropic、Vertex、Bedrock、OpenAI 等 provider 变体。典型返回：

| 用户友好 ID | GitLab ref | Provider |
|-------------|------------|----------|
| claude-haiku-4.5 | claude_haiku_4_5_20251001 | Anthropic |
| claude-haiku-4.5-vertex | claude_haiku_4_5_20251001_vertex | Vertex |
| claude-haiku-4.5-bedrock | claude_haiku_4_5_20251001_bedrock | Bedrock |
| claude-sonnet-4.6-vertex | claude_sonnet_4_6_vertex | Vertex |
| gpt-5.1 | gpt_5 | OpenAI |
| gpt-5.5 | gpt_5_5 | OpenAI |

---

## Codex CLI 验收状态（稳定点：main 最新提交）

当前稳定提交：

```text
main 最新提交
```

Codex CLI 版本：`0.142.2`

主测模型：`gpt-5.5`

核心验收：

| 项目 | 结果 |
|------|------|
| `/v1/responses` SSE | ✅ Codex CLI 可解析 |
| `exec_command(cmd=...)` | ✅ 字段正确，`missing field 'cmd'` 为 0 |
| `hello.py` 创建并运行 | ✅ 输出 `CODEX_GPT55_OK` |
| 多文件 pytest | ✅ `add.py` + `test_add.py`，3 tests passed |
| 修复失败测试 | ✅ 先看到失败，再修复并通过 |
| 多步文件任务 | ✅ 不同命令正常执行 |
| 重复成功命令 | ✅ 从 7 次降到 1 次 |
| 未知 Duo 工具脱敏诊断 | ✅ 只返回 `name` 和 `args_keys` |
| 4xx / 5xx | ✅ 0 |
| 敏感信息泄露 | ✅ 无 |

多模型轻量矩阵：

| 模型 | 等级 | 结果 | 备注 |
|------|------|------|------|
| `claude-sonnet-4.6` | A | ✅ PASS | 最快，POST 次数少，token 消耗低 |
| `gpt-5.5` | A | ✅ PASS | 基准稳定 |
| `gpt-5.4-mini` | A | ✅ PASS | 成本较低，轻量任务表现好 |
| `gpt-5.4` | B | ✅ PASS | 出现过 `printf` 转义问题，模型自动恢复 |
| `claude-sonnet-4.6-vertex` | B | ✅ PASS | 出现过 WebSocket reconnect，最终成功 |
| `gemini-3.5-flash` | C | ✅ PASS | 轮次和 token 消耗偏高 |
| `gpt-5-codex` | D | ❌ FAIL | Codex CLI metadata 不匹配，当前不推荐 |

推荐：

- 普通聊天：`claude-sonnet-4.6`、`gpt-5.5`、`gpt-5.4-mini`
- Codex CLI：`claude-sonnet-4.6`、`gpt-5.5`、`gpt-5.4-mini`
- 暂时排除：`gpt-5-codex`

残余风险：

- GitLab `_gitlab_session` 仍会过期，需要手动更新 Cookie。
- 当前 raw Duo tool_info 覆盖 `create_file_with_contents` 和 `run_command`；未来 GitLab Duo 新增原生工具名时，服务会返回脱敏诊断 `Unsupported GitLab Duo tool_info received. name=... args_keys=[...]`，再按工具名补映射。
- 多模型矩阵只做轻量任务，复杂大型代码库任务仍需按使用场景继续观察。

---

## API 使用方法

### OpenAI 兼容性 smoke test

启动服务后，可用仓库内置脚本快速跑一遍主要 OpenAI 兼容路径：

```bash
DUO2API_BASE_URL=http://127.0.0.1:8000/v1 \
DUO2API_API_KEY=sk-your-custom-key \
python scripts/openai_compat_smoke.py --model gpt-5.5
```

脚本覆盖：

- `/v1/models`
- `/v1/models/{model}`
- `/v1/chat/completions` 非流式
- `/v1/chat/completions` 流式 + `stream_options.include_usage`
- `/v1/responses` 非流式
- `/v1/responses` 流式 SSE

### 获取模型列表
```bash
curl https://<your-replit-domain>/v1/models \
  -H "Authorization: Bearer <api_key>"
```

### 获取单个模型
```bash
curl https://<your-replit-domain>/v1/models/gpt-5.5 \
  -H "Authorization: Bearer <api_key>"
```

### 普通对话
```bash
curl https://<your-replit-domain>/v1/chat/completions \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4.6",
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

### 流式对话
```bash
curl https://<your-replit-domain>/v1/chat/completions \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4.6",
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

- [x] **工具调用（function calling）**：解析 OpenAI tools 格式，转换为 prompt 指令并包装 OpenAI `tool_calls`
- [ ] **联网搜索内置提示词**：system prompt 里注入搜索指令，让模型知道何时触发搜索
- [x] **Web 配置界面**：浏览器里直接修改 config.json（更新 Cookie、切换模型等）
- [x] **多 session 并发**：每个请求用独立 DuoChat 实例，支持并行对话
- [x] **对话历史**：把 messages 数组拼成多轮对话传给 Duo
- [x] **模型校验**：未知模型 ID 返回 OpenAI 风格 400 错误
- [x] **动态模型列表**：通过 GitLab GraphQL 获取当前账号可用模型，失败时使用 fallback
- [x] **状态诊断端点**：`/v1/status` 返回版本、功能、配置、模型缓存与 GitLab deep health
- [x] **OpenAI 兼容性补齐**：支持 `/v1/models/{model}`、Responses JSON、Responses 完整文本 SSE、Chat `stream_options.include_usage`
- [x] **旧版工具参数兼容**：Chat Completions 支持 `functions/function_call` 自动转换为 `tools/tool_choice`，并在旧参数请求下返回旧版 `function_call`
- [x] **格式与长度提示约束**：Chat `response_format` / Responses `text.format` / token limit 会注入 prompt 约束
- [x] **Stop 序列**：Chat Completions 非流式文本响应支持 `stop` 截断
- [ ] **Cookie 自动刷新**：检测 session 过期并提示用户更新
- [x] **Docker 部署**：提供 Dockerfile 和 docker-compose.yml，方便自托管
- [x] **CI 验证**：GitHub Actions 自动跑 Python 3.11/3.12 编译与单元测试

---

## 临时测试说明：上下文修复验证

本节用于云端 agent 验证 2026-06-24 的上下文修复。验证目标：服务端不再共享 GitLab checkpoint 作为聊天历史；每个请求使用客户端传来的完整 OpenAI `messages`；同一个客户端窗口切换模型后，只要客户端继续发送历史 messages，模型能看到前文；不同窗口或并发请求不会通过服务端 session 串台。

### 1. 本地自动测试

在仓库根目录运行：

```bash
python -m unittest discover -s tests
python -m py_compile context.py model_catalog.py security.py server.py gitlab_duo_client.py responses_api.py tests/*.py
```

期望结果：

```text
OK
```

`py_compile` 命令应以退出码 `0` 结束，无语法错误输出。

`tests/test_security.py` 期望结果：

```text
Ran 9 tests
OK
```

它覆盖：

- OpenAI `tools/tool_choice` prompt 注入
- 模型 JSON `tool_calls` 解析与 OpenAI 格式规范化
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
    "model": "claude-sonnet-4.6",
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
    "model": "gpt-5.5",
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
    "model": "claude-sonnet-4.6",
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
    "model": "claude-sonnet-4.6",
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

验证 GitLab Cookie 主动检测：

```bash
curl "http://localhost:8000/v1/gitlab/health?deep=true" \
  -H "Authorization: Bearer sk-your-custom-key"
```

期望肉眼结果：

```json
{
  "ok": true,
  "gitlab_authenticated": true,
  "checks": {
    "csrf_token": true,
    "workflow": true
  }
}
```

如果 Cookie 失效，期望 `ok=false`，`message` 是脱敏后的提示，不包含 Cookie、GitLab 原始 HTML 或内部 API 响应全文。

验证遮罩值保护：

```bash
curl -i http://localhost:8000/v1/config \
  -H "Authorization: Bearer sk-your-custom-key" \
  -H "Content-Type: application/json" \
  -d '{
    "namespace_id": "135817766",
    "model": "claude-sonnet-4.6",
    "api_keys": ["sk-a...1234"]
  }'
```

期望肉眼结果：HTTP 400，提示提交完整 API Key 或留空。随后再次 `GET /v1/config`，`api_keys_count` 保持原数量。

### 8. 验证工具调用兼容层

发送一个最小 tools 请求：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-custom-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4.6",
    "messages": [{"role": "user", "content": "调用 get_time 工具，只输出工具调用 JSON。"}],
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

期望肉眼结果：响应 `choices[0].message.tool_calls` 存在，`finish_reason` 为 `tool_calls`，函数名为 `get_time`。

流式验证：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-custom-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4.6",
    "messages": [{"role": "user", "content": "调用 get_time 工具，只输出工具调用 JSON。"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_time",
        "description": "Get current server time",
        "parameters": {"type": "object", "properties": {}}
      }
    }],
    "tool_choice": "auto",
    "stream": true
  }'
```

期望肉眼结果：SSE 里出现 `delta.tool_calls`，结束帧 `finish_reason` 为 `tool_calls`，最后是 `data: [DONE]`。

### 9. 验证动态模型列表

获取模型列表：

```bash
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer sk-your-custom-key"
```

期望肉眼结果：
- `data` 数量优先接近 GitLab GraphQL 返回的账号可用模型数，当前探测账号为 31 个
- 返回项包含 `id`、`gitlab_id`、`name`、`model_provider`、`cost_indicator`
- `claude-sonnet-4.6-vertex` 这类 provider 变体会出现
- `gpt-5.1` 的 `gitlab_id` 应为 `gpt_5`

用动态模型发起聊天：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-custom-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4.6-vertex",
    "messages": [{"role": "user", "content": "只回答 OK"}]
  }'
```

期望肉眼结果：HTTP 200，响应 `model` 回显 `claude-sonnet-4.6-vertex`，内容能正常返回。

验证 alias 兼容：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-custom-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude_sonnet_4_6_vertex",
    "messages": [{"role": "user", "content": "只回答 OK"}]
  }'
```

期望肉眼结果：HTTP 200，说明 GraphQL 完整 ref / GitLab 下划线 ID 可以被解析。

---

## 文件结构

```
.
├── .github/workflows/ci.yml   # GitHub Actions CI
├── .dockerignore              # Docker build 排除规则
├── context.py                 # OpenAI messages 转 prompt
├── Dockerfile                 # Docker 镜像构建
├── docker-compose.yml         # 本地容器一键启动
├── gitlab_duo_client.py       # WebSocket 客户端核心逻辑
├── model_catalog.py           # 动态模型列表归一化与 alias 解析
├── responses_api.py           # Responses API / Codex CLI 兼容层
├── security.py                # 鉴权、脱敏、配置保护 helper
├── server.py                  # FastAPI 入口
├── scripts/
│   └── openai_compat_smoke.py # OpenAI 兼容性 smoke test
├── config.example.json        # 配置模板
├── config.json                # 本地运行凭据（gitignore）
├── requirements.txt           # Python 依赖
├── tests/                     # 单元测试
│   ├── test_context.py
│   ├── test_gitlab_duo_client.py
│   ├── test_models.py
│   ├── test_responses_api.py
│   ├── test_server.py
│   └── test_security.py
└── docs/
    └── PROGRESS.md            # 本文件
```
