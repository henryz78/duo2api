"""Helpers for the OpenAI Responses API compatibility layer."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from context import build_prompt


_PYTHON_FILE_RE = re.compile(r"([A-Za-z0-9_.\\/.-]+\.py)\b")
_REMAINING_PYTHON_RUN_RE = re.compile(
    r"Remaining task:\s*run\s+([A-Za-z0-9_.\\/.-]+\.py)\s+with Python",
    re.IGNORECASE,
)


def _response_tool_name(tool: Mapping[str, Any]) -> str:
    function = tool.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name", "")).strip()
    return str(tool.get("name", "")).strip()


def responses_named_tools(tools: Sequence[Mapping[str, Any]] | None) -> list[Mapping[str, Any]] | None:
    if not tools:
        return None
    named = [
        tool for tool in tools
        if isinstance(tool, Mapping)
        and str(tool.get("type", "")).strip() == "function"
        and _response_tool_name(tool)
    ]
    return named or None


def _tool_parameters(tool: Mapping[str, Any]) -> Mapping[str, Any]:
    function = tool.get("function")
    if isinstance(function, Mapping):
        parameters = function.get("parameters")
        if isinstance(parameters, Mapping):
            return parameters
    parameters = tool.get("parameters")
    return parameters if isinstance(parameters, Mapping) else {}


def _tool_property_names(tool: Mapping[str, Any]) -> set[str]:
    parameters = _tool_parameters(tool)
    properties = parameters.get("properties")
    if not isinstance(properties, Mapping):
        return set()
    return {str(name) for name in properties}


def _find_response_tool_schema(name: str, tools: Sequence[Mapping[str, Any]] | None) -> Mapping[str, Any] | None:
    for tool in tools or []:
        if isinstance(tool, Mapping) and _response_tool_name(tool) == name:
            return tool
    return None


def normalize_tool_call_for_response_tools(
    tool_call: Mapping[str, Any],
    tools: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    normalized = dict(tool_call)
    function = normalized.get("function")
    if not isinstance(function, Mapping):
        return normalized

    name = str(function.get("name", "")).strip()
    schema = _find_response_tool_schema(name, tools)
    if not schema:
        return normalized

    raw_arguments = function.get("arguments", "{}")
    try:
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments)
    except (TypeError, ValueError):
        return normalized
    if not isinstance(arguments, dict):
        return normalized

    property_names = _tool_property_names(schema)
    if name == "exec_command" and "cmd" in property_names and "cmd" not in arguments and "command" in arguments:
        arguments["cmd"] = arguments["command"]
        if "command" not in property_names:
            arguments.pop("command", None)

    normalized["function"] = {
        **dict(function),
        "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
    }
    return normalized


def _tool_call_arguments_dict(tool_call: Mapping[str, Any]) -> dict[str, Any] | None:
    function = tool_call.get("function")
    if not isinstance(function, Mapping):
        return None
    raw_arguments = function.get("arguments", "{}")
    try:
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments)
    except (TypeError, ValueError):
        return None
    return arguments if isinstance(arguments, dict) else None


def _with_tool_call_arguments(tool_call: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(tool_call)
    function = normalized.get("function")
    if not isinstance(function, Mapping):
        return normalized
    normalized["function"] = {
        **dict(function),
        "arguments": json.dumps(dict(arguments), ensure_ascii=False, separators=(",", ":")),
    }
    return normalized


def _remaining_python_run_file(messages: Sequence[Mapping[str, Any]] | None) -> str:
    for message in reversed(messages or []):
        if not isinstance(message, Mapping):
            continue
        content = _response_content_to_text(message.get("content"))
        match = _REMAINING_PYTHON_RUN_RE.search(content)
        if match:
            return match.group(1).replace("\\", "/").split("/")[-1]
    return ""


def _command_writes_python_file(command: str, filename: str) -> bool:
    if not filename or filename not in command:
        return False
    command_lower = command.lower()
    return any(marker in command_lower for marker in (
        "write_text",
        "cat >",
        "printf",
        "set-content",
        "new-item",
        "out-file",
    ))


def normalize_tool_call_for_response(
    tool_call: Mapping[str, Any],
    tools: Sequence[Mapping[str, Any]] | None,
    messages: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    normalized = normalize_tool_call_for_response_tools(tool_call, tools)
    function = normalized.get("function")
    if not isinstance(function, Mapping) or function.get("name") != "exec_command":
        return normalized

    filename = _remaining_python_run_file(messages)
    arguments = _tool_call_arguments_dict(normalized)
    if not filename or arguments is None:
        return normalized

    command_key = "cmd" if "cmd" in arguments else "command"
    command = str(arguments.get(command_key, ""))
    if not _command_writes_python_file(command, filename):
        return normalized

    arguments[command_key] = f"python3 {filename}"
    if command_key == "cmd":
        arguments.pop("command", None)
    return _with_tool_call_arguments(normalized, arguments)


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


def _extract_python_file_name(*texts: str) -> str:
    for text in texts:
        match = _PYTHON_FILE_RE.search(text or "")
        if match:
            return match.group(1).replace("\\", "/").split("/")[-1]
    return ""


def _tool_arguments_to_text(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    if isinstance(arguments, Mapping):
        return json.dumps(arguments, ensure_ascii=False)
    return str(arguments or "")


def _tool_followup_guidance(
    user_text: str,
    tool_name: str,
    arguments_text: str,
    output_text: str,
) -> list[str]:
    user_lower = user_text.lower()
    run_intent = any(phrase in user_lower for phrase in (
        "then run",
        "run it",
        "execute it",
        "运行",
        "执行",
    ))
    if not run_intent or tool_name != "exec_command":
        return []

    filename = _extract_python_file_name(output_text, arguments_text, user_text)
    if not filename:
        return []

    action_text = f"{arguments_text}\n{output_text}".lower()
    wrote_file = any(marker in action_text for marker in (
        "write_text",
        "created",
        "written",
        "write ",
        "cat >",
        "printf",
        "set-content",
        "new-item",
    ))
    if not wrote_file:
        return []

    return [
        f"Completed step: {filename} has been written.",
        f"Remaining task: run {filename} with Python and report the output.",
        f"Do not recreate or rewrite {filename}.",
    ]


def responses_input_to_messages(input_value: Any) -> list[dict[str, Any]]:
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    if not isinstance(input_value, Sequence):
        return []

    messages: list[dict[str, Any]] = []
    latest_user_text = ""
    last_function_call_name = ""
    last_function_call_arguments = ""
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
                if role == "user":
                    latest_user_text = content
        elif item_type == "function_call":
            name = str(item.get("name", "")).strip()
            arguments = item.get("arguments", "{}")
            call_id = str(item.get("call_id") or item.get("id") or "").strip()
            if name:
                last_function_call_name = name
                last_function_call_arguments = _tool_arguments_to_text(arguments)
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
            header = f"Previous local tool call {call_id} completed." if call_id else "Previous local tool call completed."
            parts = [
                header,
                "Tool output:",
                output or "(no output)",
                *_tool_followup_guidance(
                    latest_user_text,
                    last_function_call_name,
                    last_function_call_arguments,
                    output,
                ),
                "Continue the original user request from this state.",
                "Do not repeat completed tool calls; use the next required tool call or provide the final answer.",
            ]
            content = "\n".join(parts)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
    return messages


def responses_body_to_messages(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    messages = responses_input_to_messages(body.get("input"))
    instructions = str(body.get("instructions") or "").strip()
    if instructions:
        return [{"role": "system", "content": instructions}] + messages
    return messages


def build_responses_prompt(body: Mapping[str, Any]) -> str:
    tool_choice = body.get("tool_choice")
    tools = None if tool_choice == "none" else body.get("tools")
    return build_prompt(
        responses_body_to_messages(body),
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
