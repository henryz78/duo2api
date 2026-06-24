import asyncio
import json
import re
import httpx
import websockets
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"


def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _gitlab_cfg() -> dict:
    return _load_config()["gitlab"]


def _get_cookies() -> dict:
    return _gitlab_cfg()["cookies"]


def _get_namespace_id() -> str:
    return str(_gitlab_cfg()["namespace_id"])


def _get_model() -> str:
    return _gitlab_cfg().get("model", "claude-sonnet-4.5")


def _get_ua() -> str:
    return _gitlab_cfg().get("user_agent", "Mozilla/5.0")


GITLAB_HOST = "https://gitlab.com"
WSS_HOST = "wss://gitlab.com"
MODEL = "claude-sonnet-4.5"
HTTP_TIMEOUT_SECONDS = 30.0
WS_OPEN_TIMEOUT_SECONDS = 10.0
WS_CLOSE_TIMEOUT_SECONDS = 5.0
RESPONSE_TIMEOUT_SECONDS = 120.0

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


def resolve_gitlab_model_id(model_id: str) -> str:
    """Convert user-facing model ID to GitLab internal ID."""
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
