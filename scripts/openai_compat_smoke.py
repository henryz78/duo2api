#!/usr/bin/env python3
"""Small OpenAI-compatibility smoke test for a running duo2api server.

The script uses only the Python standard library. It never prints API keys or
GitLab cookies. Configure it with environment variables or CLI arguments:

    DUO2API_BASE_URL=http://127.0.0.1:8000/v1 \
    DUO2API_API_KEY=sk-your-key \
    python scripts/openai_compat_smoke.py --model gpt-5.5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"


class SmokeError(RuntimeError):
    pass


SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9._-]+"),
    re.compile(r"glpat-[A-Za-z0-9._-]+"),
    re.compile(r"_gitlab_session=[^\"'\\s;,}]+"),
    re.compile(r"remember_user_token=[^\"'\\s;,}]+"),
    re.compile(r'("(?:api_key|token|cookie|authorization)"\s*:\s*")[^"]+(")', re.IGNORECASE),
)


def _safe_body_preview(body: bytes, *, limit: int = 300) -> str:
    text = body[:limit].decode("utf-8", errors="replace")
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(r"\1[REDACTED]\2", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return repr(text)


def _url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def _headers(api_key: str | None, *, json_body: bool = False) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _request(
    base_url: str,
    path: str,
    *,
    api_key: str | None,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[int, str, bytes]:
    data = None
    headers = _headers(api_key, json_body=payload is not None)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(_url(base_url, path), data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        raise SmokeError(f"{method} {path} returned HTTP {exc.code}: {_safe_body_preview(body)}") from exc
    except urllib.error.URLError as exc:
        raise SmokeError(f"{method} {path} failed: {exc}") from exc


def _json_request(
    base_url: str,
    path: str,
    *,
    api_key: str | None,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status, content_type, body = _request(base_url, path, api_key=api_key, method=method, payload=payload)
    if status < 200 or status >= 300:
        raise SmokeError(f"{method} {path} returned HTTP {status}")
    if "json" not in content_type:
        raise SmokeError(f"{method} {path} returned non-JSON content type: {content_type}")
    try:
        parsed = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SmokeError(f"{method} {path} returned invalid JSON") from exc
    if "error" in parsed:
        raise SmokeError(f"{method} {path} returned error: {parsed['error']}")
    return parsed


def _sse_events(body: bytes) -> list[tuple[str, dict[str, Any] | str]]:
    text = body.decode("utf-8", errors="replace")
    events: list[tuple[str, dict[str, Any] | str]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_name = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if not data_lines:
            continue
        raw_data = "\n".join(data_lines)
        if raw_data == "[DONE]":
            events.append((event_name, raw_data))
            continue
        try:
            events.append((event_name, json.loads(raw_data)))
        except json.JSONDecodeError:
            events.append((event_name, raw_data))
    return events


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def check_models(base_url: str, api_key: str | None, model: str) -> None:
    models = _json_request(base_url, "models", api_key=api_key)
    data = models.get("data")
    _assert(isinstance(data, list) and data, "/v1/models returned no model data")
    ids = {item.get("id") for item in data if isinstance(item, dict)}
    _assert(model in ids, f"/v1/models does not include requested model {model!r}")

    single = _json_request(base_url, f"models/{model}", api_key=api_key)
    _assert(single.get("object") == "model", "/v1/models/{model} did not return a model object")
    _assert(single.get("id") == model, "/v1/models/{model} returned a different model id")
    print(f"✓ models: count={len(data)} model={model}")


def check_chat(base_url: str, api_key: str | None, model: str) -> None:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: CHAT_OK"}],
        "stream": False,
        "max_completion_tokens": 16,
        "response_format": {"type": "text"},
        "parallel_tool_calls": False,
        "functions": [{
            "name": "noop",
            "description": "No-op compatibility probe",
            "parameters": {"type": "object", "properties": {}},
        }],
        "function_call": "none",
    }
    response = _json_request(base_url, "chat/completions", api_key=api_key, method="POST", payload=payload)
    _assert(response.get("object") == "chat.completion", "chat completion object mismatch")
    _assert(response.get("choices"), "chat completion missing choices")
    print("✓ chat.completions: non-stream JSON")


def check_chat_stream(base_url: str, api_key: str | None, model: str) -> None:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: STREAM_OK"}],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    status, content_type, body = _request(
        base_url,
        "chat/completions",
        api_key=api_key,
        method="POST",
        payload=payload,
    )
    _assert(status == 200, "chat stream returned non-200")
    _assert("text/event-stream" in content_type, f"chat stream content type mismatch: {content_type}")
    events = _sse_events(body)
    _assert(any(event == "message" and data == "[DONE]" for event, data in events), "chat stream missing [DONE]")
    json_events = [data for _, data in events if isinstance(data, dict)]
    _assert(
        any(event.get("choices") == [] and isinstance(event.get("usage"), dict) for event in json_events),
        "chat stream missing final usage chunk with include_usage=true",
    )
    print("✓ chat.completions: stream SSE include_usage")


def check_responses(base_url: str, api_key: str | None, model: str) -> None:
    payload = {
        "model": model,
        "input": "Reply with exactly: RESPONSES_OK",
        "stream": False,
        "metadata": {"smoke": "true"},
        "parallel_tool_calls": False,
        "reasoning": {"effort": "low"},
        "store": False,
        "truncation": "auto",
        "max_output_tokens": 32,
        "text": {"format": {"type": "text"}},
    }
    response = _json_request(base_url, "responses", api_key=api_key, method="POST", payload=payload)
    _assert(response.get("object") == "response", "responses JSON object mismatch")
    _assert(response.get("status") == "completed", "responses JSON status mismatch")
    _assert(response.get("output"), "responses JSON missing output")
    print("✓ responses: non-stream JSON")


def check_responses_stream(base_url: str, api_key: str | None, model: str) -> None:
    payload = {
        "model": model,
        "input": "Reply with exactly: RESPONSES_STREAM_OK",
        "stream": True,
    }
    status, content_type, body = _request(
        base_url,
        "responses",
        api_key=api_key,
        method="POST",
        payload=payload,
    )
    _assert(status == 200, "responses stream returned non-200")
    _assert("text/event-stream" in content_type, f"responses stream content type mismatch: {content_type}")
    event_names = [name for name, _ in _sse_events(body)]
    for required in (
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.completed",
    ):
        _assert(required in event_names, f"responses stream missing {required}")
    print("✓ responses: stream SSE events")


def main() -> int:
    parser = argparse.ArgumentParser(description="duo2api OpenAI compatibility smoke test")
    parser.add_argument("--base-url", default=os.environ.get("DUO2API_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--api-key", default=os.environ.get("DUO2API_API_KEY", ""))
    parser.add_argument("--model", default=os.environ.get("DUO2API_SMOKE_MODEL", "gpt-5.5"))
    args = parser.parse_args()

    api_key = args.api_key.strip() or None
    print(f"duo2api smoke test: base_url={args.base_url.rstrip('/')} model={args.model}")
    print(f"auth: has_api_key={bool(api_key)}")

    try:
        check_models(args.base_url, api_key, args.model)
        check_chat(args.base_url, api_key, args.model)
        check_chat_stream(args.base_url, api_key, args.model)
        check_responses(args.base_url, api_key, args.model)
        check_responses_stream(args.base_url, api_key, args.model)
    except SmokeError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    print("all OpenAI compatibility smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
