"""Helpers for the OpenAI Responses API compatibility layer."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from context import build_prompt


def _response_tool_name(tool: Mapping[str, Any]) -> str:
    function = tool.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name", "")).strip()
    return str(tool.get("name", "")).strip()


def responses_named_tools(tools: Sequence[Mapping[str, Any]] | None) -> list[Mapping[str, Any]] | None:
    if not tools:
        return None
    named = [tool for tool in tools if isinstance(tool, Mapping) and _response_tool_name(tool)]
    return named or None


def _response_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, Mapping):
        return str(content.get("text") or content.get("output_text") or "").strip()
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, Mapping):
                continue
            block_type = str(block.get("type", ""))
            if block_type in ("input_text", "output_text", "text"):
                text = str(block.get("text", "")).strip()
                if text:
                    parts.append(text)
            elif block_type == "input_image":
                image_url = block.get("image_url") or block.get("url") or ""
                if image_url:
                    parts.append(f"[image_url: {image_url}]")
        return "\n".join(parts).strip()
    return str(content).strip()


def responses_input_to_messages(input_value: Any) -> list[dict[str, Any]]:
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    if not isinstance(input_value, Sequence):
        return []

    messages: list[dict[str, Any]] = []
    for item in input_value:
        if not isinstance(item, Mapping):
            continue
        item_type = str(item.get("type", "message"))
        if item_type == "message":
            role = str(item.get("role", "user")).strip().lower() or "user"
            if role == "developer":
                role = "system"
            content = _response_content_to_text(item.get("content"))
            if content:
                messages.append({"role": role, "content": content})
        elif item_type == "function_call":
            name = str(item.get("name", "")).strip()
            arguments = item.get("arguments", "{}")
            call_id = str(item.get("call_id") or item.get("id") or "").strip()
            if name:
                messages.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": call_id or f"call_{len(messages)}",
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }],
                })
        elif item_type == "function_call_output":
            output = _response_content_to_text(item.get("output"))
            call_id = str(item.get("call_id") or "").strip()
            messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
    return messages


def build_responses_prompt(body: Mapping[str, Any]) -> str:
    tool_choice = body.get("tool_choice")
    tools = None if tool_choice == "none" else body.get("tools")
    return build_prompt(
        responses_input_to_messages(body.get("input")),
        tools=tools,
        tool_choice=tool_choice,
    )


def sse_event(event: str, data: Mapping[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def response_created_sse(resp_id: str, model: str, created_at: int) -> str:
    return sse_event("response.created", {
        "type": "response.created",
        "response": {
            "id": resp_id,
            "object": "response",
            "created_at": created_at,
            "status": "in_progress",
            "model": model,
            "output": [],
        },
    })


def response_function_call_sse(
    resp_id: str,
    model: str,
    created_at: int,
    tool_call: Mapping[str, Any],
    usage: Mapping[str, Any],
) -> str:
    function = tool_call.get("function") if isinstance(tool_call.get("function"), Mapping) else {}
    name = str(function.get("name", "")).strip()
    arguments = str(function.get("arguments", "{}"))
    item_id = f"fc_{uuid.uuid4().hex[:16]}"
    call_id = str(tool_call.get("id") or f"call_{uuid.uuid4().hex[:16]}")
    added_item = {
        "type": "function_call",
        "id": item_id,
        "call_id": call_id,
        "name": name,
        "arguments": "",
        "status": "in_progress",
    }
    done_item = {**added_item, "arguments": arguments, "status": "completed"}
    chunks = [
        sse_event("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": added_item,
        })
    ]
    for start in range(0, len(arguments), 64):
        chunks.append(sse_event("response.function_call_arguments.delta", {
            "type": "response.function_call_arguments.delta",
            "item_id": item_id,
            "output_index": 0,
            "call_id": call_id,
            "delta": arguments[start:start + 64],
        }))
    chunks.extend([
        sse_event("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done",
            "item_id": item_id,
            "output_index": 0,
            "call_id": call_id,
            "arguments": arguments,
        }),
        sse_event("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": done_item,
        }),
        response_completed_sse(resp_id, model, created_at, [done_item], usage),
    ])
    return "".join(chunks)


def response_completed_sse(
    resp_id: str,
    model: str,
    created_at: int,
    output: Sequence[Mapping[str, Any]],
    usage: Mapping[str, Any],
) -> str:
    return sse_event("response.completed", {
        "type": "response.completed",
        "response": {
            "id": resp_id,
            "object": "response",
            "created_at": created_at,
            "status": "completed",
            "model": model,
            "output": list(output),
            "usage": dict(usage),
        },
    })


def text_output_items(message_id: str, text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    added_item = {
        "type": "message",
        "id": message_id,
        "status": "in_progress",
        "role": "assistant",
        "content": [],
    }
    done_item = {
        **added_item,
        "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }
    return added_item, done_item
