"""Conversation context helpers for OpenAI-compatible requests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


MessageLike = dict[str, Any]


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


def build_prompt(messages: Sequence[MessageLike]) -> str:
    """Flatten OpenAI messages into the prompt sent to GitLab Duo."""
    parts: list[str] = []
    for msg in messages:
        role = str(msg.get("role", "user")).strip().lower() or "user"
        content = _message_content_to_text(msg.get("content"))
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
    return prompt


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
