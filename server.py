"""OpenAI-compatible API wrapper for GitLab Duo Chat."""

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from context import (
    build_prompt,
    build_tool_retry_prompt,
    extract_tool_calls,
    is_known_model,
    should_retry_auto_tool_choice,
    validate_tools,
)
from gitlab_duo_client import (
    ALL_MODELS,
    DuoChat,
    _load_config,
    clear_model_cache,
    get_available_models,
    probe_gitlab_auth,
    resolve_gitlab_model_id,
)
from security import (
    apply_config_update,
    auth_keys_from_config,
    clear_auth_cache,
    estimate_tokens,
    public_config_status,
    public_upstream_error_message,
)
from responses_api import (
    build_responses_prompt,
    response_completed_sse,
    response_created_sse,
    response_function_call_sse,
    responses_named_tools,
    responses_input_to_messages,
    sse_event,
    text_output_items,
)

CONFIG_PATH = Path(__file__).parent / "config.json"

_cfg = _load_config()
_srv = _cfg["server"]
SERVER_HOST: str = _srv.get("host", "0.0.0.0")
SERVER_PORT: int = int(os.environ.get("PORT", _srv.get("port", 8000)))

app = FastAPI(title="GitLab Duo OpenAI Proxy", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
logger = logging.getLogger("duo2api")


def _openai_error(status: int, code: str, message: str, param=None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message,
                           "type": "invalid_request_error" if status < 500 else "server_error",
                           "param": param, "code": code}},
    )


def _check_auth(request: Request, *, require_configured_keys: bool = False) -> JSONResponse | None:
    keys = auth_keys_from_config(CONFIG_PATH)
    if require_configured_keys and not keys:
        return _openai_error(
            403,
            "config_auth_required",
            "Configure server.api_keys in config.json before using config management endpoints.",
        )
    if not keys:
        return None
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return _openai_error(401, "invalid_api_key", "Missing or invalid Authorization header.")
    if auth[len("Bearer "):] not in keys:
        return _openai_error(401, "invalid_api_key", "Incorrect API key provided.")
    return None


class Message(BaseModel):
    role: str
    content: str | list[dict[str, Any]] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ChatRequest(BaseModel):
    model: str = "claude-sonnet-4.5"
    messages: list[Message]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    stop: list[str] | str | None = None
    user: str | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None


class ResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "claude-sonnet-4.5"
    input: Any
    stream: bool = True
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    instructions: str | None = None


def _tools_allowed(tools: list[dict[str, Any]] | None, tool_choice: str | dict[str, Any] | None) -> bool:
    return bool(tools) and tool_choice != "none"


def _build_prompt(body: ChatRequest) -> str:
    return build_prompt(
        [m.model_dump(exclude_none=True) for m in body.messages],
        tools=body.tools if _tools_allowed(body.tools, body.tool_choice) else None,
        tool_choice=body.tool_choice,
    )


def _estimate_tokens(text: str) -> int:
    return estimate_tokens(text)


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _chunk(req_id: str, model: str, content: str = "", finish_reason: str | None = None) -> str:
    delta = {"content": content} if content else {}
    return _chunk_delta(req_id, model, delta, finish_reason=finish_reason)


def _chunk_delta(req_id: str, model: str, delta: dict, finish_reason: str | None = None) -> str:
    return _sse({"id": req_id, "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]})


def _tool_call_deltas(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"index": index, **tool_call} for index, tool_call in enumerate(tool_calls)]


def _chat_messages(body: ChatRequest) -> list[dict[str, Any]]:
    return [m.model_dump(exclude_none=True) for m in body.messages]


def _usage(prompt_tokens: int, completion_tokens: int) -> dict[str, int]:
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


async def _send_with_optional_tool_retry(
    session: DuoChat,
    prompt: str,
    upstream_model: str,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
) -> tuple[str, list[dict[str, Any]], int]:
    full = await session.send(prompt, model=upstream_model)
    completion_tokens = _estimate_tokens(full)
    tool_calls = extract_tool_calls(full)
    if tool_calls:
        return full, tool_calls, completion_tokens
    if not should_retry_auto_tool_choice(messages, tools, tool_choice, full):
        return full, [], completion_tokens

    retry_full = await session.send(build_tool_retry_prompt(prompt), model=upstream_model)
    completion_tokens += _estimate_tokens(retry_full)
    retry_tool_calls = extract_tool_calls(retry_full)
    if retry_tool_calls:
        return retry_full, retry_tool_calls, completion_tokens
    return full, [], completion_tokens


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    model_options = "\n".join(
        f'<option value="{m["id"]}" {"selected" if m["id"] == "claude-sonnet-4.5" else ""}>{m["name"]} ({m["owned_by"]})</option>'
        for m in ALL_MODELS
    )

    model_table_rows = "\n".join(
        f'<tr><td><code>{m["id"]}</code></td><td>{m["name"]}</td><td>{m["owned_by"].title()}</td></tr>'
        for m in ALL_MODELS
    )

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GitLab Duo OpenAI Proxy</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; padding: 2rem 1rem; }}
  .container {{ max-width: 760px; margin: 0 auto; }}
  h1 {{ font-size: 1.6rem; font-weight: 700; margin-bottom: 0.25rem; color: #f8fafc; }}
  .subtitle {{ color: #94a3b8; font-size: 0.9rem; margin-bottom: 2rem; }}
  .status {{ background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 1rem 1.25rem; margin-bottom: 2rem; display: flex; align-items: center; gap: 0.75rem; }}
  .status-dot {{ width: 10px; height: 10px; border-radius: 50%; background: #f59e0b; flex-shrink: 0; }}
  .status-text {{ font-size: 0.95rem; color: #f59e0b; }}
  .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem; }}
  .card h2 {{ font-size: 1rem; font-weight: 600; margin-bottom: 1rem; color: #cbd5e1; }}
  label {{ display: block; font-size: 0.82rem; color: #94a3b8; margin-bottom: 0.4rem; margin-top: 1rem; }}
  label:first-of-type {{ margin-top: 0; }}
  input, textarea, select {{ width: 100%; background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 0.6rem 0.8rem; color: #e2e8f0; font-size: 0.88rem; font-family: 'Courier New', monospace; outline: none; transition: border-color 0.2s; }}
  select {{ font-family: inherit; cursor: pointer; }}
  input:focus, textarea:focus, select:focus {{ border-color: #6366f1; }}
  textarea {{ resize: vertical; min-height: 60px; }}
  .btn {{ display: inline-block; background: #6366f1; color: #fff; border: none; border-radius: 8px; padding: 0.65rem 1.5rem; font-size: 0.9rem; font-weight: 600; cursor: pointer; transition: background 0.2s; margin-top: 1.25rem; }}
  .btn:hover {{ background: #4f46e5; }}
  .btn.secondary {{ background: #334155; margin-left: 0.5rem; }}
  .btn.secondary:hover {{ background: #475569; }}
  .hint {{ background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 1rem; font-size: 0.82rem; color: #94a3b8; line-height: 1.7; }}
  .hint code {{ background: #1e293b; padding: 0.1em 0.4em; border-radius: 4px; color: #a5b4fc; font-size: 0.85em; }}
  .api-info {{ background: #0f172a; border-radius: 8px; padding: 1rem; font-size: 0.82rem; color: #94a3b8; }}
  .api-info .row {{ display: flex; justify-content: space-between; padding: 0.4rem 0; border-bottom: 1px solid #1e293b; }}
  .api-info .row:last-child {{ border-bottom: none; }}
  .api-info .val {{ color: #a5b4fc; font-family: monospace; font-size: 0.85em; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  th {{ text-align: left; color: #64748b; font-weight: 600; padding: 0.4rem 0.6rem; border-bottom: 1px solid #334155; }}
  td {{ padding: 0.45rem 0.6rem; border-bottom: 1px solid #1e293b; color: #94a3b8; }}
  td code {{ color: #a5b4fc; font-size: 0.85em; }}
  tr:last-child td {{ border-bottom: none; }}
  #msg {{ margin-top: 1rem; padding: 0.75rem 1rem; border-radius: 8px; font-size: 0.88rem; display: none; }}
  .success {{ background: #14532d; color: #86efac; }}
  .error {{ background: #7f1d1d; color: #fca5a5; }}
</style>
</head>
<body>
<div class="container">
  <h1>🦊 GitLab Duo OpenAI Proxy</h1>
  <p class="subtitle">将 GitLab Duo Chat 包装成 OpenAI 兼容 API</p>

  <div class="status">
    <div class="status-dot"></div>
    <span class="status-text">请输入管理 API Key 读取配置状态</span>
  </div>

  <div class="card">
    <h2>接口信息</h2>
    <div class="api-info">
      <div class="row"><span>Base URL</span><span class="val">/v1</span></div>
      <div class="row"><span>Chat Completions</span><span class="val">POST /v1/chat/completions</span></div>
      <div class="row"><span>Models</span><span class="val">GET /v1/models</span></div>
      <div class="row"><span>默认模型</span><span class="val" id="default_model_status">待读取</span></div>
      <div class="row"><span>可用模型数</span><span class="val" id="available_model_count">{len(ALL_MODELS)} 个</span></div>
    </div>
  </div>

  <div class="card">
    <h2>GitLab 配置</h2>
    <form id="configForm">
      <label>管理 API Key</label>
      <input type="password" id="admin_key" value="" placeholder="用于读取和保存配置；不会写入页面源码">

      <label>_gitlab_session Cookie</label>
      <input type="password" id="gitlab_session" value="" placeholder="留空则保持不变">

      <label>remember_user_token Cookie（可选，有效期更长）</label>
      <input type="password" id="remember_token" value="" placeholder="留空则保持不变">

      <label>Namespace ID（需使用有 Duo 权限的 Group namespace）</label>
      <input type="text" id="namespace_id" value="" placeholder="例：135817766">

      <label>默认模型（请求未指定时使用）</label>
      <select id="model_input">
        {model_options}
      </select>

      <label>API Keys（每行一个，留空则保持不变）</label>
      <textarea id="api_keys" placeholder="留空则保持不变；输入新 key 列表会覆盖当前配置"></textarea>

      <button type="submit" class="btn">保存配置</button>
      <button type="button" id="checkGitLabBtn" class="btn secondary">检查 GitLab Cookie</button>
    </form>
    <div id="msg"></div>
  </div>

  <div class="card">
    <h2>支持的全部模型（<span id="model_table_count">{len(ALL_MODELS)}</span> 个）</h2>
    <table>
      <thead><tr><th>Model ID</th><th>名称</th><th>提供商</th></tr></thead>
      <tbody id="models_table_body">{model_table_rows}</tbody>
    </table>
  </div>

  <div class="card">
    <h2>如何获取 GitLab Cookie</h2>
    <div class="hint">
      1. 浏览器登录 <code>gitlab.com</code>（登录时勾选 "Remember me"）<br>
      2. 按 <code>F12</code> → Application → Cookies → gitlab.com<br>
      3. 复制 <code>_gitlab_session</code> 和 <code>remember_user_token</code><br><br>
      <strong>Namespace ID：</strong> 访问
      <code>https://gitlab.com/api/v4/namespaces?search=你的用户名</code>，
      找 <code>kind=group</code> 且有 Ultimate/Pro 的那个 <code>id</code>
    </div>
  </div>

  <div class="card">
    <h2>第三方客户端接入</h2>
    <div class="hint">
      在 <strong>ChatBox / Open WebUI / Cursor / Continue</strong> 设置中填写：<br><br>
      API Base URL：<code>https://你的域名/v1</code><br>
      API Key：配置的 key<br>
      Model：任意上方表格中的 Model ID
    </div>
  </div>
</div>

<script>
let configLoaded = false;

function escapeHtml(value) {{
  return String(value || '').replace(/[&<>"']/g, (ch) => ({{
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }}[ch]));
}}

function authHeaders(includeJson = true) {{
  const key = document.getElementById('admin_key').value.trim();
  const headers = includeJson ? {{'Content-Type': 'application/json'}} : {{}};
  if (key) headers['Authorization'] = 'Bearer ' + key;
  return headers;
}}

async function loadModels(selectedModel) {{
  const res = await fetch('/v1/models', {{headers: authHeaders(false)}});
  if (!res.ok) return;
  const json = await res.json();
  const models = json.data || [];
  const select = document.getElementById('model_input');
  select.innerHTML = models.map((m) => {{
    const label = (m.name || m.id) + (m.model_provider ? ' - ' + m.model_provider : '') + (m.cost_indicator ? ' (' + m.cost_indicator + ')' : '');
    return '<option value="' + escapeHtml(m.id) + '">' + escapeHtml(label) + '</option>';
  }}).join('');
  const selected = selectedModel ? models.find((m) => (
    m.id === selectedModel ||
    m.gitlab_id === selectedModel ||
    (m.aliases || []).includes(selectedModel)
  )) : null;
  if (selected) {{
    select.value = selected.id;
  }}
  document.getElementById('available_model_count').textContent = models.length + ' 个';
  document.getElementById('model_table_count').textContent = models.length;
  document.getElementById('models_table_body').innerHTML = models.map((m) => {{
    const provider = m.model_provider || m.owned_by || '';
    return '<tr><td><code>' + escapeHtml(m.id) + '</code></td><td>' + escapeHtml(m.name || m.id) + '</td><td>' + escapeHtml(provider) + '</td></tr>';
  }}).join('');
}}

async function loadConfigStatus() {{
  const msg = document.getElementById('msg');
  const res = await fetch('/v1/config', {{headers: authHeaders(false)}});
  if (!res.ok) {{
    configLoaded = false;
    msg.style.display = 'block';
    msg.className = 'error';
    msg.textContent = '请输入有效管理 API Key 后保存或刷新配置状态';
    return false;
  }}
  const cfg = await res.json();
  document.getElementById('namespace_id').value = cfg.namespace_id || '';
  await loadModels(cfg.model || 'claude-sonnet-4.5');
  document.getElementById('model_input').value = cfg.model || 'claude-sonnet-4.5';
  document.getElementById('default_model_status').textContent = cfg.model || '未配置';
  document.getElementById('gitlab_session').placeholder = cfg.has_session_cookie ? '已配置；留空则保持不变' : '未配置';
  document.getElementById('remember_token').placeholder = cfg.has_remember_token ? '已配置；留空则保持不变' : '未配置';
  document.getElementById('api_keys').placeholder = '已配置 ' + cfg.api_keys_count + ' 个；留空则保持不变；输入新 key 列表会覆盖当前配置';
  msg.style.display = 'block';
  msg.className = 'success';
  msg.textContent = '配置状态已读取';
  configLoaded = true;
  return true;
}}

document.getElementById('admin_key').addEventListener('change', () => {{
  configLoaded = false;
  loadConfigStatus().catch(() => {{}});
}});

document.getElementById('checkGitLabBtn').addEventListener('click', async () => {{
  const msg = document.getElementById('msg');
  msg.style.display = 'block';
  msg.className = 'success';
  msg.textContent = '正在检查 GitLab Cookie...';
  try {{
    const res = await fetch('/v1/gitlab/health?deep=true', {{headers: authHeaders(false)}});
    const json = await res.json();
    if (res.ok && json.ok) {{
      msg.className = 'success';
      msg.textContent = '✅ GitLab Cookie 有效，Duo workflow 可创建';
    }} else {{
      msg.className = 'error';
      msg.textContent = '❌ ' + (json.message || json.error?.message || 'GitLab Cookie 检查失败');
    }}
  }} catch(err) {{
    msg.className = 'error';
    msg.textContent = '❌ 网络错误：' + err.message;
  }}
}});

document.getElementById('configForm').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const msg = document.getElementById('msg');
  if (!configLoaded) {{
    const loaded = await loadConfigStatus().catch(() => false);
    if (!loaded) return;
  }}
  const data = {{
    gitlab_session: document.getElementById('gitlab_session').value,
    remember_token: document.getElementById('remember_token').value,
    namespace_id: document.getElementById('namespace_id').value,
    model: document.getElementById('model_input').value,
    api_keys: document.getElementById('api_keys').value.split('\\n').map(k => k.trim()).filter(Boolean)
  }};
  try {{
    const res = await fetch('/v1/config', {{
      method: 'POST',
      headers: authHeaders(true),
      body: JSON.stringify(data)
    }});
    const json = await res.json();
    msg.style.display = 'block';
    if (res.ok) {{
      msg.className = 'success';
      msg.textContent = '✅ 配置已保存';
      await loadConfigStatus();
    }} else {{
      msg.className = 'error';
      msg.textContent = '❌ ' + (json.error?.message || json.detail || '保存失败');
    }}
  }} catch(err) {{
    msg.style.display = 'block';
    msg.className = 'error';
    msg.textContent = '❌ 网络错误：' + err.message;
  }}
}});

loadConfigStatus().catch(() => {{}});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.get("/v1/models")
async def list_models(request: Request):
    if err := _check_auth(request):
        return err
    models = await get_available_models()
    ts = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": m["id"],
                "object": "model",
                "created": ts,
                "owned_by": m.get("owned_by", "gitlab"),
                "permission": [],
                "root": m["id"],
                "parent": None,
                "name": m.get("name", m["id"]),
                "gitlab_id": m.get("gitlab_id", m["id"]),
                "model_provider": m.get("model_provider", m.get("owned_by", "gitlab")),
                "cost_indicator": m.get("cost_indicator", ""),
                "aliases": m.get("aliases", []),
            }
            for m in models
        ],
    }


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "duo2api"}


@app.get("/v1/gitlab/health")
async def gitlab_health(request: Request, deep: bool = False):
    if err := _check_auth(request, require_configured_keys=True):
        return err
    try:
        return await probe_gitlab_auth(deep=deep)
    except Exception as e:
        logger.warning("GitLab Duo health check failed: %s", e)
        return {
            "ok": False,
            "gitlab_authenticated": False,
            "namespace_id": _load_config().get("gitlab", {}).get("namespace_id", ""),
            "checks": {
                "csrf_token": False,
                "workflow": False if deep else None,
            },
            "message": public_upstream_error_message(e),
        }


@app.get("/v1/config")
async def get_config(request: Request):
    if err := _check_auth(request, require_configured_keys=True):
        return err
    cfg = _load_config()
    models = await get_available_models()
    return public_config_status(cfg, len(models))


class ConfigUpdate(BaseModel):
    gitlab_session: str = ""
    remember_token: str = ""
    namespace_id: str | None = None
    model: str | None = None
    api_keys: list[str] = Field(default_factory=list)


@app.post("/v1/config")
async def update_config(request: Request, body: ConfigUpdate):
    if err := _check_auth(request, require_configured_keys=True):
        return err
    cfg = _load_config()
    models = await get_available_models()
    if body.model and not is_known_model(body.model, models):
        return _openai_error(
            400,
            "model_not_found",
            f"Model '{body.model}' not found. Call /v1/models for available models.",
            param="model",
        )
    try:
        apply_config_update(
            cfg,
            gitlab_session=body.gitlab_session,
            remember_token=body.remember_token,
            namespace_id=body.namespace_id,
            model=body.model or None,
            api_keys=body.api_keys,
        )
    except ValueError as e:
        return _openai_error(400, "invalid_request_error", str(e), param="config")

    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    clear_auth_cache()
    clear_model_cache()
    return {"ok": True, "message": "Config saved. Stateless mode uses new GitLab workflow per request."}


@app.post("/v1/responses")
async def responses(request: Request, body: ResponsesRequest):
    if err := _check_auth(request):
        return err

    named_tools = responses_named_tools(body.tools)
    try:
        validate_tools(named_tools)
    except ValueError as e:
        return _openai_error(400, "invalid_request_error", str(e), param="tools")

    cfg = _load_config()
    model = body.model if body.model else cfg["gitlab"].get("model", "claude-sonnet-4.5")
    models = await get_available_models()
    if not is_known_model(model, models):
        return _openai_error(
            400,
            "model_not_found",
            f"Model '{model}' not found. Call /v1/models for available models.",
            param="model",
        )
    upstream_model = resolve_gitlab_model_id(model, models)
    body_data = body.model_dump(exclude_none=True)
    if named_tools is None:
        body_data.pop("tools", None)
    else:
        body_data["tools"] = named_tools
    try:
        prompt = build_responses_prompt(body_data)
    except ValueError as e:
        return _openai_error(400, "invalid_request_error", str(e), param="input")

    prompt_tokens = _estimate_tokens(prompt)
    resp_id = f"resp_{uuid.uuid4().hex}"
    tools_allowed = _tools_allowed(named_tools, body.tool_choice)

    return StreamingResponse(
        _do_responses_stream(
            prompt,
            resp_id,
            prompt_tokens,
            model,
            upstream_model,
            tools_enabled=tools_allowed,
            messages=responses_input_to_messages(body.input),
            tools=named_tools,
            tool_choice=body.tool_choice,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatRequest):
    if err := _check_auth(request):
        return err

    try:
        validate_tools(body.tools)
    except ValueError as e:
        return _openai_error(400, "invalid_request_error", str(e), param="tools")

    try:
        prompt = _build_prompt(body)
    except ValueError as e:
        return _openai_error(400, "invalid_request_error", str(e), param="messages")

    cfg = _load_config()
    model = body.model if body.model else cfg["gitlab"].get("model", "claude-sonnet-4.5")
    models = await get_available_models()
    if not is_known_model(model, models):
        return _openai_error(
            400,
            "model_not_found",
            f"Model '{model}' not found. Call /v1/models for available models.",
            param="model",
        )
    upstream_model = resolve_gitlab_model_id(model, models)
    prompt_tokens = _estimate_tokens(prompt)
    req_id = f"chatcmpl-{uuid.uuid4().hex}"
    tools_allowed = _tools_allowed(body.tools, body.tool_choice)
    messages = _chat_messages(body)

    if body.stream:
        return StreamingResponse(
            _do_stream(
                prompt,
                req_id,
                prompt_tokens,
                model,
                upstream_model,
                tools_enabled=tools_allowed,
                messages=messages,
                tools=body.tools,
                tool_choice=body.tool_choice,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return await _do_complete(
        prompt,
        req_id,
        prompt_tokens,
        model,
        upstream_model,
        tools_enabled=tools_allowed,
        messages=messages,
        tools=body.tools,
        tool_choice=body.tool_choice,
    )


async def _do_complete(
    prompt: str,
    req_id: str,
    prompt_tokens: int,
    model: str,
    upstream_model: str,
    *,
    tools_enabled: bool = False,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
):
    session = DuoChat()
    try:
        if tools_enabled:
            full, tool_calls, completion_tokens = await _send_with_optional_tool_retry(
                session,
                prompt,
                upstream_model,
                messages=messages or [],
                tools=tools,
                tool_choice=tool_choice,
            )
        else:
            full = await session.send(prompt, model=upstream_model)
            completion_tokens = _estimate_tokens(full)
            tool_calls = []
    except Exception as e:
        logger.warning("GitLab Duo upstream error: %s", e)
        return _openai_error(502, "upstream_error", public_upstream_error_message(e))
    finally:
        await session.close()

    if tool_calls:
        return JSONResponse(content={
            "id": req_id, "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                                                 "tool_calls": tool_calls},
                         "finish_reason": "tool_calls", "logprobs": None}],
            "usage": _usage(prompt_tokens, completion_tokens),
            "system_fingerprint": None,
        })
    return JSONResponse(content={
        "id": req_id, "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": full},
                     "finish_reason": "stop", "logprobs": None}],
        "usage": _usage(prompt_tokens, completion_tokens),
        "system_fingerprint": None,
    })


def _responses_usage(input_tokens: int, output_tokens: int) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


async def _do_responses_stream(
    prompt: str,
    resp_id: str,
    prompt_tokens: int,
    model: str,
    upstream_model: str,
    *,
    tools_enabled: bool = False,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
):
    created_at = int(time.time())
    yield response_created_sse(resp_id, model, created_at)

    session = DuoChat()
    if tools_enabled:
        try:
            full, tool_calls, completion_tokens = await _send_with_optional_tool_retry(
                session,
                prompt,
                upstream_model,
                messages=messages or [],
                tools=tools,
                tool_choice=tool_choice,
            )
        except Exception as e:
            logger.warning("GitLab Duo upstream responses error: %s", e)
            yield sse_event("response.failed", {
                "type": "response.failed",
                "response": {
                    "id": resp_id,
                    "object": "response",
                    "created_at": created_at,
                    "status": "failed",
                    "model": model,
                    "error": {
                        "code": "upstream_error",
                        "message": public_upstream_error_message(e),
                    },
                },
            })
            return
        finally:
            await session.close()

        usage = _responses_usage(prompt_tokens, completion_tokens)
        if tool_calls:
            yield response_function_call_sse(resp_id, model, created_at, tool_calls[0], usage)
            return

        message_id = f"msg_{uuid.uuid4().hex[:16]}"
        added_item, done_item = text_output_items(message_id, full)
        yield sse_event("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": added_item,
        })
        yield sse_event("response.output_text.delta", {
            "type": "response.output_text.delta",
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "delta": full,
        })
        yield sse_event("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": done_item,
        })
        yield response_completed_sse(resp_id, model, created_at, [done_item], usage)
        return

    message_id = f"msg_{uuid.uuid4().hex[:16]}"
    added_item, _ = text_output_items(message_id, "")
    yield sse_event("response.output_item.added", {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": added_item,
    })
    yield sse_event("response.content_part.added", {
        "type": "response.content_part.added",
        "item_id": message_id,
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": ""},
    })

    full = ""
    completion_tokens = 0
    try:
        async for chunk in session.stream(prompt, model=upstream_model):
            full += chunk
            completion_tokens += _estimate_tokens(chunk)
            yield sse_event("response.output_text.delta", {
                "type": "response.output_text.delta",
                "item_id": message_id,
                "output_index": 0,
                "content_index": 0,
                "delta": chunk,
            })
    except Exception as e:
        logger.warning("GitLab Duo upstream responses stream error: %s", e)
        yield sse_event("response.failed", {
            "type": "response.failed",
            "response": {
                "id": resp_id,
                "object": "response",
                "created_at": created_at,
                "status": "failed",
                "model": model,
                "error": {
                    "code": "upstream_error",
                    "message": public_upstream_error_message(e),
                },
            },
        })
        return
    finally:
        await session.close()

    _, done_item = text_output_items(message_id, full)
    yield sse_event("response.output_text.done", {
        "type": "response.output_text.done",
        "item_id": message_id,
        "output_index": 0,
        "content_index": 0,
        "text": full,
    })
    yield sse_event("response.content_part.done", {
        "type": "response.content_part.done",
        "item_id": message_id,
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": full},
    })
    yield sse_event("response.output_item.done", {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": done_item,
    })
    yield response_completed_sse(
        resp_id,
        model,
        created_at,
        [done_item],
        _responses_usage(prompt_tokens, completion_tokens),
    )


async def _do_stream(
    prompt: str,
    req_id: str,
    prompt_tokens: int,
    model: str,
    upstream_model: str,
    *,
    tools_enabled: bool = False,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
):
    session = DuoChat()

    yield _sse({"id": req_id, "object": "chat.completion.chunk", "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})

    completion_tokens = 0
    if tools_enabled:
        try:
            full, tool_calls, completion_tokens = await _send_with_optional_tool_retry(
                session,
                prompt,
                upstream_model,
                messages=messages or [],
                tools=tools,
                tool_choice=tool_choice,
            )
        except Exception as e:
            logger.warning("GitLab Duo upstream stream error: %s", e)
            yield _sse({"error": {"message": public_upstream_error_message(e), "type": "server_error", "code": "upstream_error"}})
            yield "data: [DONE]\n\n"
            return
        finally:
            await session.close()

        if tool_calls:
            yield _chunk_delta(req_id, model, {"tool_calls": _tool_call_deltas(tool_calls)})
            yield _sse({"id": req_id, "object": "chat.completion.chunk", "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                        "usage": _usage(prompt_tokens, completion_tokens)})
            yield "data: [DONE]\n\n"
            return
        if full:
            yield _chunk(req_id, model, content=full)
        yield _sse({"id": req_id, "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": _usage(prompt_tokens, completion_tokens)})
        yield "data: [DONE]\n\n"
        return

    try:
        async for chunk in session.stream(prompt, model=upstream_model):
            completion_tokens += _estimate_tokens(chunk)
            yield _chunk(req_id, model, content=chunk)
    except Exception as e:
        logger.warning("GitLab Duo upstream stream error: %s", e)
        yield _sse({"error": {"message": public_upstream_error_message(e), "type": "server_error", "code": "upstream_error"}})
        yield "data: [DONE]\n\n"
        return
    finally:
        await session.close()

    yield _sse({"id": req_id, "object": "chat.completion.chunk", "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": _usage(prompt_tokens, completion_tokens)})
    yield "data: [DONE]\n\n"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=SERVER_HOST, port=SERVER_PORT, reload=False)
