"""Conversation context helpers for OpenAI-compatible requests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


MessageLike = dict[str, Any]
ModelLike = dict[str, Any]
PROMPT_PREAMBLE = (
    "You are a versatile AI assistant. Please respond helpfully and completely "
    "to the conversation below, following any instructions or context provided by the user."
)
TOOL_CALLING_INSTRUCTIONS = (
    "When a tool is needed, respond only with a JSON object in this exact shape: "
    '{"tool_calls":[{"name":"tool_name","arguments":{}}]}. '
    "When no tool is needed, answer normally."
)


def _message_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = str(block.get("text", "")).strip()
                if text:
                    parts.append(text)
            elif block_type == "image_url":
                image_url = block.get("image_url") or {}
                url = image_url.get("url") if isinstance(image_url, Mapping) else ""
                if url:
                    parts.append(f"[image_url: {url}]")
            elif block_type in ("input_audio", "file"):
                parts.append(f"[{block_type}]")
        return "\n".join(parts).strip()
    return str(content).strip()


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _tools_prompt(tools: Sequence[Mapping[str, Any]] | None, tool_choice: Any = None) -> str:
    if not tools:
        return ""
    payload = {
        "tools": list(tools),
        "tool_choice": "auto" if tool_choice is None else tool_choice,
        "tool_response_format": {"tool_calls": [{"name": "tool_name", "arguments": {}}]},
    }
    return "\n\n".join([
        "[Available Tools]",
        _compact_json(payload),
        "[Tool Calling Instructions]",
        TOOL_CALLING_INSTRUCTIONS,
    ])


def _normalize_tool_call(call: Mapping[str, Any], index: int) -> dict[str, Any] | None:
    function = call.get("function")
    if isinstance(function, Mapping):
        name = str(function.get("name", "")).strip()
        arguments = function.get("arguments", {})
    else:
        name = str(call.get("name", "")).strip()
        arguments = call.get("arguments", {})
    if not name:
        return None
    if not isinstance(arguments, str):
        arguments = _compact_json(arguments)
    return {
        "id": str(call.get("id") or f"call_{index}"),
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments,
        },
    }


def extract_tool_calls(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, Mapping):
            continue
        raw_calls = value.get("tool_calls")
        if not isinstance(raw_calls, list):
            continue
        calls = [
            normalized
            for call_index, call in enumerate(raw_calls)
            if isinstance(call, Mapping)
            for normalized in [_normalize_tool_call(call, call_index)]
            if normalized is not None
        ]
        if calls:
            return calls
    return []


def build_prompt(
    messages: Sequence[MessageLike],
    *,
    tools: Sequence[Mapping[str, Any]] | None = None,
    tool_choice: Any = None,
) -> str:
    """Flatten OpenAI messages into the prompt sent to GitLab Duo."""
    parts: list[str] = []
    system_prefix = "\n\n".join(
        content
        for msg in messages
        if str(msg.get("role", "user")).strip().lower() == "system"
        for content in [_message_content_to_text(msg.get("content"))]
        if content
    )
    first_user_seen = False
    for msg in messages:
        role = str(msg.get("role", "user")).strip().lower() or "user"
        content = _message_content_to_text(msg.get("content"))
        if role == "system":
            continue
        if role == "user" and not first_user_seen:
            first_user_seen = True
            original_content = content
            content = "\n\n".join(part for part in [system_prefix, original_content] if part)
        tool_calls = msg.get("tool_calls")
        if role == "assistant" and tool_calls:
            content = "\n\n".join(part for part in [content, json.dumps(tool_calls, ensure_ascii=False)] if part)
        if role == "tool":
            tool_id = str(msg.get("tool_call_id", "")).strip()
            label = f"Tool Result {tool_id}" if tool_id else "Tool Result"
        else:
            label = role.capitalize()
        if content:
            parts.append(f"[{label}]\n{content}")
    if not any(str(m.get("role", "")).lower() == "user" for m in messages):
        raise ValueError("No user message found in request.")
    prompt = "\n\n".join(parts).strip()
    if not prompt:
        raise ValueError("No message content found in request.")
    tool_prompt = _tools_prompt(tools, tool_choice)
    if tool_prompt:
        prompt = f"{prompt}\n\n{tool_prompt}"
    return f"{PROMPT_PREAMBLE}\n\n{prompt}"


def fingerprint_messages(messages: Sequence[MessageLike]) -> str:
    compact = [
        {
            "role": msg.get("role", "user"),
            "content": msg.get("content"),
            "tool_call_id": msg.get("tool_call_id"),
            "tool_calls": msg.get("tool_calls"),
        }
        for msg in messages
    ]
    canonical = json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"messages-{digest[:16]}"


def is_known_model(model_id: str, models: Sequence[ModelLike]) -> bool:
    if not model_id:
        return False
    return any(model_id in (model.get("id"), model.get("gitlab_id")) for model in models)
