import asyncio
import json
import re
import shlex
import time
import httpx
import websockets
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from model_catalog import normalize_graphql_models, resolve_model_id

CONFIG_PATH = Path(__file__).parent / "config.json"


def normalize_config(cfg: dict[str, Any]) -> dict[str, Any]:
    gitlab = cfg.setdefault("gitlab", {})
    if not isinstance(gitlab, dict):
        gitlab = {}
        cfg["gitlab"] = gitlab

    cookies = gitlab.get("cookies")
    if not isinstance(cookies, dict):
        cookies = {}
        gitlab["cookies"] = cookies

    legacy_session = str(gitlab.get("session") or "").strip()
    if legacy_session and not str(cookies.get("_gitlab_session") or "").strip():
        cookies["_gitlab_session"] = legacy_session

    legacy_remember = str(gitlab.get("remember_user_token") or "").strip()
    cookies.setdefault("remember_user_token", legacy_remember)

    gitlab.setdefault("host", GITLAB_HOST)
    gitlab.setdefault("model", "claude-sonnet-4.6")
    gitlab.setdefault("user_agent", "Mozilla/5.0")

    server = cfg.get("server")
    if not isinstance(server, dict):
        server = {}
        cfg["server"] = server

    legacy_api_keys = cfg.get("api_keys")
    if isinstance(legacy_api_keys, list) and not server.get("api_keys"):
        server["api_keys"] = legacy_api_keys
    server.setdefault("host", "0.0.0.0")
    server.setdefault("port", 8000)
    server.setdefault("api_keys", [])

    return cfg


def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return normalize_config(json.load(f))


def _gitlab_cfg() -> dict:
    return _load_config()["gitlab"]


def _get_cookies() -> dict:
    return _gitlab_cfg()["cookies"]


def _get_namespace_id() -> str:
    return str(_gitlab_cfg()["namespace_id"])


def _get_model() -> str:
    return _gitlab_cfg().get("model", "claude-sonnet-4.6")


def _get_ua() -> str:
    return _gitlab_cfg().get("user_agent", "Mozilla/5.0")


GITLAB_HOST = "https://gitlab.com"
WSS_HOST = "wss://gitlab.com"
MODEL = "claude-sonnet-4.6"
HTTP_TIMEOUT_SECONDS = 30.0
WS_OPEN_TIMEOUT_SECONDS = 10.0
WS_CLOSE_TIMEOUT_SECONDS = 5.0
RESPONSE_TIMEOUT_SECONDS = 120.0
MODEL_CACHE_TTL_SECONDS = 300.0
AI_CHAT_MODELS_QUERY = """
query getAiChatAvailableModels($rootNamespaceId: GroupID, $namespaceId: GroupID) {
  aiChatAvailableModels(rootNamespaceId: $rootNamespaceId, namespaceId: $namespaceId) {
    selectableModels {
      ref
      name
      modelProvider
      modelDescription
      costIndicator
    }
    defaultModel {
      name
      ref
      modelProvider
    }
    pinnedModel {
      ref
      name
    }
  }
}
"""
_MODEL_CACHE: dict[str, Any] = {"expires_at": 0.0, "models": None}

# id        = user-facing OpenAI-compatible model ID (dashes + dots)
# gitlab_id = internal GitLab identifier sent in the WebSocket URL (snake_case)
ALL_MODELS = [
    {"id": "claude-haiku-4.5",   "gitlab_id": "claude_haiku_4_5",   "name": "Claude Haiku 4.5",   "owned_by": "anthropic"},
    {"id": "claude-sonnet-4.5",  "gitlab_id": "claude_sonnet_4_5",  "name": "Claude Sonnet 4.5",  "owned_by": "anthropic"},
    {"id": "claude-sonnet-4.6",  "gitlab_id": "claude_sonnet_4_6",  "name": "Claude Sonnet 4.6",  "owned_by": "anthropic"},
    {"id": "claude-opus-4.5",    "gitlab_id": "claude_opus_4_5",    "name": "Claude Opus 4.5",    "owned_by": "anthropic"},
    {"id": "claude-opus-4.6",    "gitlab_id": "claude_opus_4_6",    "name": "Claude Opus 4.6",    "owned_by": "anthropic"},
    {"id": "claude-opus-4.7",    "gitlab_id": "claude_opus_4_7",    "name": "Claude Opus 4.7",    "owned_by": "anthropic"},
    {"id": "claude-opus-4.8",    "gitlab_id": "claude_opus_4_8",    "name": "Claude Opus 4.8",    "owned_by": "anthropic"},
    {"id": "gemini-3.5-flash",   "gitlab_id": "gemini_3_5_flash",   "name": "Gemini 3.5 Flash",   "owned_by": "google"},
    {"id": "gpt-5-mini",         "gitlab_id": "gpt_5_mini",         "name": "GPT-5-Mini",         "owned_by": "openai"},
    {"id": "gpt-5.1",            "gitlab_id": "gpt_5_1",            "name": "GPT-5.1",            "owned_by": "openai"},
    {"id": "gpt-5.2",            "gitlab_id": "gpt_5_2",            "name": "GPT-5.2",            "owned_by": "openai"},
    {"id": "gpt-5-codex",        "gitlab_id": "gpt_5_codex",        "name": "GPT-5-Codex",        "owned_by": "openai"},
    {"id": "gpt-5.2-codex",      "gitlab_id": "gpt_5_2_codex",      "name": "GPT-5.2-Codex",      "owned_by": "openai"},
    {"id": "gpt-5.3-codex",      "gitlab_id": "gpt_5_3_codex",      "name": "GPT-5.3-Codex",      "owned_by": "openai"},
    {"id": "gpt-5.4",            "gitlab_id": "gpt_5_4",            "name": "GPT-5.4",            "owned_by": "openai"},
    {"id": "gpt-5.4-mini",       "gitlab_id": "gpt_5_4_mini",       "name": "GPT-5.4-Mini",       "owned_by": "openai"},
    {"id": "gpt-5.4-nano",       "gitlab_id": "gpt_5_4_nano",       "name": "GPT-5.4-Nano",       "owned_by": "openai"},
    {"id": "gpt-5.5",            "gitlab_id": "gpt_5_5",            "name": "GPT-5.5",            "owned_by": "openai"},
]

# Lookup: user-facing ID → GitLab internal ID
_MODEL_ID_MAP: dict[str, str] = {m["id"]: m["gitlab_id"] for m in ALL_MODELS}
# Also accept legacy snake_case IDs directly
_MODEL_ID_MAP.update({m["gitlab_id"]: m["gitlab_id"] for m in ALL_MODELS})


def resolve_gitlab_model_id(model_id: str, models: Sequence[dict[str, Any]] | None = None) -> str:
    """Convert user-facing model ID to GitLab internal ID."""
    if models is not None:
        return resolve_model_id(model_id, models)
    return _MODEL_ID_MAP.get(model_id, model_id)


def cookie_header() -> str:
    cookies = _get_cookies()
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


async def fetch_csrf_token(client: httpx.AsyncClient) -> str:
    ua = _get_ua()
    for path in ["/dashboard", "/users/sign_in"]:
        resp = await client.get(
            f"{GITLAB_HOST}{path}",
            headers={"User-Agent": ua, "Cookie": cookie_header()},
            follow_redirects=True,
        )
        m = re.search(r'<meta\s+name=["\']csrf-token["\']\s+content=["\'](.*?)["\']', resp.text)
        if m:
            return m.group(1)
    raise RuntimeError("Could not find CSRF token — are the cookies still valid?")


def _namespace_gid() -> str:
    return f"gid://gitlab/Group/{_get_namespace_id()}"


async def fetch_available_models(client: httpx.AsyncClient, csrf: str | None = None) -> list[dict[str, Any]]:
    ua = _get_ua()
    headers = {
        "Content-Type": "application/json",
        "Cookie": cookie_header(),
        "User-Agent": ua,
        "X-Requested-With": "XMLHttpRequest",
    }
    if csrf:
        headers["X-CSRF-Token"] = csrf
    namespace_gid = _namespace_gid()
    resp = await client.post(
        f"{GITLAB_HOST}/api/graphql",
        json={
            "query": AI_CHAT_MODELS_QUERY.strip(),
            "variables": {
                "rootNamespaceId": namespace_gid,
                "namespaceId": namespace_gid,
            },
        },
        headers=headers,
    )
    if not resp.is_success:
        raise RuntimeError(f"Failed to fetch GitLab models ({resp.status_code})")
    payload = resp.json()
    result = (payload.get("data") or {}).get("aiChatAvailableModels")
    if not result:
        errors = payload.get("errors") or []
        detail = errors[0].get("message", "empty model list") if errors else "empty model list"
        raise RuntimeError(f"Failed to fetch GitLab models: {detail}")
    models = normalize_graphql_models(result)
    if not models:
        raise RuntimeError("Failed to fetch GitLab models: empty model list")
    return models


def clear_model_cache() -> None:
    _MODEL_CACHE["expires_at"] = 0.0
    _MODEL_CACHE["models"] = None


def model_cache_status() -> dict[str, Any]:
    cached = _MODEL_CACHE.get("models")
    expires_at = float(_MODEL_CACHE.get("expires_at", 0.0))
    expires_in = max(0, int(expires_at - time.monotonic())) if cached is not None else 0
    return {
        "cache_ttl_seconds": int(MODEL_CACHE_TTL_SECONDS),
        "has_cached_models": cached is not None and expires_in > 0,
        "cached_count": len(cached) if isinstance(cached, list) else 0,
        "expires_in_seconds": expires_in,
        "fallback_count": len(ALL_MODELS),
    }


async def get_available_models(*, refresh: bool = False) -> list[dict[str, Any]]:
    now = time.monotonic()
    cached = _MODEL_CACHE.get("models")
    if not refresh and cached is not None and now < float(_MODEL_CACHE.get("expires_at", 0.0)):
        return list(cached)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=HTTP_TIMEOUT_SECONDS) as client:
            csrf = await fetch_csrf_token(client)
            models = await fetch_available_models(client, csrf)
    except Exception:
        return list(ALL_MODELS)
    _MODEL_CACHE["models"] = list(models)
    _MODEL_CACHE["expires_at"] = now + MODEL_CACHE_TTL_SECONDS
    return list(models)


async def create_workflow(client: httpx.AsyncClient, csrf: str) -> str:
    namespace_id = _get_namespace_id()
    ua = _get_ua()
    resp = await client.post(
        f"{GITLAB_HOST}/api/v4/ai/duo_workflows/workflows",
        json={"namespace_id": namespace_id, "workflow_definition": "chat"},
        headers={
            "Content-Type": "application/json",
            "Cookie": cookie_header(),
            "User-Agent": ua,
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-Token": csrf,
        },
    )
    if not resp.is_success:
        raise RuntimeError(f"Failed to create workflow ({resp.status_code}): {resp.text[:200]}")
    return str(resp.json()["id"])


async def probe_gitlab_auth(*, deep: bool = False) -> dict:
    async with httpx.AsyncClient(follow_redirects=True, timeout=HTTP_TIMEOUT_SECONDS) as client:
        csrf = await fetch_csrf_token(client)
        workflow_checked = False
        if deep:
            await create_workflow(client, csrf)
            workflow_checked = True
        return {
            "ok": True,
            "gitlab_authenticated": True,
            "namespace_id": _get_namespace_id(),
            "checks": {
                "csrf_token": bool(csrf),
                "workflow": workflow_checked if deep else None,
            },
            "message": "GitLab authentication is valid.",
        }


def _ws_url(workflow_id: str, model: str) -> str:
    namespace_id = _get_namespace_id()
    return (
        f"{WSS_HOST}/api/v4/ai/duo_workflows/ws"
        f"?root_namespace_id={namespace_id}&namespace_id={namespace_id}"
        f"&user_selected_model_identifier={model}"
        f"&workflow_definition=chat&workflow_id={workflow_id}&client_type=browser"
    )


def _start_msg(workflow_id: str, goal: str, checkpoint: str = "") -> str:
    msg: dict = {
        "startRequest": {
            "workflowID": workflow_id,
            "clientVersion": "1.0",
            "workflowDefinition": "chat",
            "workflowMetadata": json.dumps({
                "extended_logging": False,
                "is_team_member": False,
                "tool_approval_for_session_enabled": True,
            }),
            "clientCapabilities": ["incremental_streaming", "web_search"],
            "goal": goal,
            "approval": {},
            "useOrbit": False,
            "additional_context": [],
        }
    }
    if checkpoint:
        msg["startRequest"]["checkpoint"] = checkpoint
    return json.dumps(msg)


def _parse_checkpoint(checkpoint_json: str) -> dict:
    try:
        return json.loads(checkpoint_json)
    except (json.JSONDecodeError, TypeError):
        return {}


def _extract_new_agent_content(checkpoint_json: str, seen_id: str | None) -> tuple[str | None, str | None]:
    inner = _parse_checkpoint(checkpoint_json)
    log = inner.get("channel_values", {}).get("ui_chat_log", [])
    for entry in reversed(log):
        if entry.get("message_type") == "agent":
            mid = entry.get("message_id")
            content = entry.get("content", "").strip()
            if content and mid != seen_id:
                return content, mid
            break
    return None, None


def _extract_pending_tool_info(checkpoint_json: str) -> dict[str, Any] | None:
    inner = _parse_checkpoint(checkpoint_json)
    log = inner.get("channel_values", {}).get("ui_chat_log", [])
    for entry in reversed(log):
        if entry.get("message_type") != "request":
            continue
        tool_info = entry.get("tool_info")
        if isinstance(tool_info, Mapping):
            return dict(tool_info)
    return None


def _exec_command_call(command: str) -> str:
    return json.dumps({
        "tool_calls": [{
            "name": "exec_command",
            "arguments": {"command": command},
        }]
    }, ensure_ascii=False, separators=(",", ":"))


def _create_file_command(args: Mapping[str, Any]) -> str:
    file_path = str(args.get("file_path") or args.get("path") or "").strip()
    contents = str(args.get("contents") or args.get("content") or "")
    if not file_path:
        return ""
    script = "\n".join([
        "from pathlib import Path",
        f"path = Path({json.dumps(file_path, ensure_ascii=False)})",
        f"contents = {json.dumps(contents, ensure_ascii=False)}",
        "path.parent.mkdir(parents=True, exist_ok=True)",
        "path.write_text(contents, encoding='utf-8')",
    ])
    return "python3 - <<'PY'\n" + script + "\nPY"


def _run_command(args: Mapping[str, Any]) -> str:
    program = str(args.get("program") or args.get("command") or "").strip()
    raw_args = args.get("args")
    if isinstance(raw_args, list):
        arg_text = " ".join(shlex.quote(str(arg)) for arg in raw_args)
    else:
        arg_text = str(raw_args or "").strip()
    if program and arg_text:
        return f"{program} {arg_text}"
    return program or arg_text


def _duo_tool_info_to_tool_call_text(tool_info: Mapping[str, Any] | None) -> str | None:
    if not tool_info:
        return None
    name = str(tool_info.get("name") or "").strip()
    args = tool_info.get("args")
    if not isinstance(args, Mapping):
        args = {}
    if name == "create_file_with_contents":
        command = _create_file_command(args)
    elif name == "run_command":
        command = _run_command(args)
    else:
        arg_keys = sorted(str(key) for key in args.keys())
        return (
            "Unsupported GitLab Duo tool_info received. "
            f"name={name or 'unknown'} args_keys={json.dumps(arg_keys, ensure_ascii=False)}"
        )
    if not command:
        return None
    return _exec_command_call(command)


async def _recv_until_done(ws, workflow_id: str, seen_id: str | None = None) -> tuple[str, str, str | None]:
    last_checkpoint = ""
    final_answer = ""
    current_id = seen_id
    printed_len = 0

    async for raw in ws:
        data = json.loads(raw)

        if "newCheckpoint" not in data:
            if "error" in data:
                raise RuntimeError(f"Server error: {data['error']}")
            continue

        cp = data["newCheckpoint"]
        status = cp.get("status", "")
        last_checkpoint = cp.get("checkpoint", last_checkpoint)

        content, mid = _extract_new_agent_content(last_checkpoint, seen_id)
        if content:
            final_answer = content
            current_id = mid
            if len(content) > printed_len:
                printed_len = len(content)

        if status in ("INPUT_REQUIRED", "COMPLETE"):
            break
        elif status == "TOOL_CALL_APPROVAL_REQUIRED":
            bridged = _duo_tool_info_to_tool_call_text(_extract_pending_tool_info(last_checkpoint))
            if bridged:
                final_answer = bridged
            break
        elif status == "FAILED":
            raise RuntimeError(f"Workflow failed: {cp.get('errors', [])}")

    return final_answer, last_checkpoint, current_id


class DuoChat:
    def __init__(self):
        self.workflow_id: str | None = None
        self.last_checkpoint: str = ""
        self.last_agent_id: str | None = None
        self._csrf: str | None = None
        self._http: httpx.AsyncClient | None = None

    def reset(self):
        self.workflow_id = None
        self.last_checkpoint = ""
        self.last_agent_id = None
        self._csrf = None
        self._http = None

    async def _ensure_init(self):
        if self._http is None:
            self._http = httpx.AsyncClient(follow_redirects=True, timeout=HTTP_TIMEOUT_SECONDS)
        if self._csrf is None:
            self._csrf = await fetch_csrf_token(self._http)
        if self.workflow_id is None:
            self.workflow_id = await create_workflow(self._http, self._csrf)

    async def send(self, message: str, model: str | None = None) -> str:
        await self._ensure_init()
        ua = _get_ua()
        use_model = resolve_gitlab_model_id(model or _get_model())
        ws_headers = {
            "Cookie": cookie_header(),
            "Origin": "https://gitlab.com",
            "User-Agent": ua,
        }

        async with websockets.connect(
            _ws_url(self.workflow_id, use_model),
            additional_headers=ws_headers,
            open_timeout=WS_OPEN_TIMEOUT_SECONDS,
            close_timeout=WS_CLOSE_TIMEOUT_SECONDS,
        ) as ws:
            payload = _start_msg(self.workflow_id, message, checkpoint=self.last_checkpoint)
            await ws.send(payload)
            answer, self.last_checkpoint, self.last_agent_id = await asyncio.wait_for(
                _recv_until_done(ws, workflow_id=self.workflow_id, seen_id=self.last_agent_id),
                timeout=RESPONSE_TIMEOUT_SECONDS,
            )

        return answer

    async def stream(self, message: str, model: str | None = None):
        await self._ensure_init()
        ua = _get_ua()
        use_model = resolve_gitlab_model_id(model or _get_model())
        ws_headers = {
            "Cookie": cookie_header(),
            "Origin": "https://gitlab.com",
            "User-Agent": ua,
        }

        last_checkpoint = self.last_checkpoint
        current_id = self.last_agent_id
        seen_id = self.last_agent_id
        printed_len = 0

        async with websockets.connect(
            _ws_url(self.workflow_id, use_model),
            additional_headers=ws_headers,
            open_timeout=WS_OPEN_TIMEOUT_SECONDS,
            close_timeout=WS_CLOSE_TIMEOUT_SECONDS,
        ) as ws:
            payload = _start_msg(self.workflow_id, message, checkpoint=last_checkpoint)
            await ws.send(payload)

            async with asyncio.timeout(RESPONSE_TIMEOUT_SECONDS):
                async for raw in ws:
                    data = json.loads(raw)
                    if "newCheckpoint" not in data:
                        if "error" in data:
                            raise RuntimeError(f"Server error: {data['error']}")
                        continue

                    cp = data["newCheckpoint"]
                    status = cp.get("status", "")
                    last_checkpoint = cp.get("checkpoint", last_checkpoint)

                    content, mid = _extract_new_agent_content(last_checkpoint, seen_id)
                    if content and len(content) > printed_len:
                        new_chunk = content[printed_len:]
                        printed_len = len(content)
                        current_id = mid
                        yield new_chunk

                    if status in ("INPUT_REQUIRED", "COMPLETE"):
                        break
                    elif status == "FAILED":
                        raise RuntimeError(f"Workflow failed: {cp.get('errors', [])}")

        self.last_checkpoint = last_checkpoint
        self.last_agent_id = current_id

    async def close(self):
        if self._http:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
