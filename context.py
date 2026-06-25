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
    "Use tools for requests that ask you to run commands, create files, edit files, "
    "inspect local state, fetch data, search, query, convert, read, or execute. "
    "When no tool is needed, answer normally."
)
TOOL_RETRY_INSTRUCTIONS = (
    "You must respond only with a JSON object matching this shape: "
    '{"tool_calls":[{"name":"tool_name","arguments":{}}]}. '
    "Choose the best available tool for the request. Do not include prose."
)
TOOL_INTENT_KEYWORDS = (
    "调用",
    "工具",
    "查询",
    "搜索",
    "查找",
    "换算",
    "转换",
    "获取",
    "读取",
    "执行",
    "运行",
    "call",
    "tool",
    "search",
    "lookup",
    "query",
    "convert",
    "fetch",
    "read",
    "run",
    "execute",
    "create",
    "write",
    "edit",
    "file",
    "command",
    "shell",
    "terminal",
    "python",
    "bash",
    "powershell",
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


def _tool_name(tool: Mapping[str, Any]) -> str:
    function = tool.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name", "")).strip()
    return str(tool.get("name", "")).strip()


def validate_tools(tools: Sequence[Mapping[str, Any]] | None) -> None:
    if not tools:
        return
    for index, tool in enumerate(tools):
        if not isinstance(tool, Mapping):
            raise ValueError(f"tools[{index}] must be an object.")
        if not _tool_name(tool):
            raise ValueError(f"tools[{index}].function.name is required.")


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


def _tools_json(tools: Sequence[Mapping[str, Any]] | None) -> str:
    return _compact_json(list(tools or []))


def build_tool_retry_prompt(
    prompt: str,
    *,
    messages: Sequence[MessageLike] | None = None,
    tools: Sequence[Mapping[str, Any]] | None = None,
    previous_response: str = "",
) -> str:
    parts = [
        prompt.strip(),
        "[Tool Retry Instructions]",
        TOOL_RETRY_INSTRUCTIONS,
    ]
    if messages is not None or tools:
        request_text = _latest_request_text(messages or [])
        tools_text = _tools_json(tools)
        has_exec_command = any(_tool_name(tool) == "exec_command" for tool in (tools or []))
        schema = (
            '{"tool_calls":[{"name":"exec_command","arguments":{"command":"..."}}]}'
            if has_exec_command
            else '{"tool_calls":[{"name":"tool_name","arguments":{}}]}'
        )
        selection = [
            "Use exec_command for creating files, editing files, reading directories, running Python, "
            "running shell commands, checking versions, installing packages, or executing tests.",
            "Use the command field for the exact shell command.",
            "Prefer a single safe command that performs the requested action.",
            "If multiple steps are needed, combine them with shell syntax supported by the current OS.",
        ] if has_exec_command else [
            "Choose the best matching tool from Available tools.",
            "Use argument names from the selected tool schema.",
            "Prefer exactly one tool call for the requested action.",
        ]
        adapter = [
            "[Compatibility Tool Adapter]",
            "You are producing an OpenAI-compatible tool call for a local automation client.",
            "Your task is to convert the user's requested local action into exactly one tool call.",
            "Output rules:",
            "Output only valid JSON.",
            "Do not explain.",
            "Do not describe what you will do.",
            "Do not answer in prose.",
            "Do not use markdown.",
            "The first character must be {.",
            "The full response must match this schema exactly:",
            schema,
            "Tool selection:",
            *selection,
            "User request:",
            request_text,
            "Available tools:",
            tools_text,
        ]
        previous = previous_response.strip()
        if previous:
            adapter.extend([
                "Previous invalid response:",
                previous[:2000],
            ])
        adapter.append("Return only the JSON tool call now.")
        parts.append("\n".join(adapter))
    return "\n\n".join(part for part in parts if part).strip()


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


def _latest_user_text(messages: Sequence[MessageLike]) -> str:
    for msg in reversed(messages):
        if str(msg.get("role", "")).strip().lower() != "user":
            continue
        return _message_content_to_text(msg.get("content"))
    return ""


def _latest_request_text(messages: Sequence[MessageLike]) -> str:
    latest_user = _latest_user_text(messages)
    if latest_user:
        return latest_user
    for msg in reversed(messages):
        role = str(msg.get("role", "")).strip().lower()
        if role in {"assistant", "tool", "function"}:
            continue
        text = _message_content_to_text(msg.get("content"))
        if text:
            return text
    return ""


def _is_auto_tool_choice(tool_choice: Any) -> bool:
    return tool_choice is None or tool_choice == "auto"


def _is_required_tool_choice(tool_choice: Any) -> bool:
    if tool_choice == "required":
        return True
    if not isinstance(tool_choice, Mapping):
        return False
    choice_type = str(tool_choice.get("type", "")).strip()
    if choice_type in {"function", "custom"}:
        return True
    if choice_type == "allowed_tools":
        allowed = tool_choice.get("allowed_tools")
        return isinstance(allowed, Mapping) and allowed.get("mode") == "required"
    return False


def should_retry_required_tool_choice(
    tools: Sequence[Mapping[str, Any]] | None,
    tool_choice: Any,
    model_text: str,
) -> bool:
    if not tools or not _is_required_tool_choice(tool_choice):
        return False
    return not bool(extract_tool_calls(model_text))


def should_retry_auto_tool_choice(
    messages: Sequence[MessageLike],
    tools: Sequence[Mapping[str, Any]] | None,
    tool_choice: Any,
    model_text: str,
) -> bool:
    if not tools or not _is_auto_tool_choice(tool_choice):
        return False
    if extract_tool_calls(model_text):
        return False
    latest_request = _latest_request_text(messages).lower()
    if not latest_request:
        return False
    if any(keyword in latest_request for keyword in TOOL_INTENT_KEYWORDS):
        return True
    tool_names = [
        name.lower()
        for tool in tools
        for name in [_tool_name(tool)]
        if name
    ]
    return any(name in latest_request for name in tool_names)


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
    for model in models:
        aliases = model.get("aliases", [])
        values = {model.get("id"), model.get("gitlab_id")}
        if isinstance(aliases, Sequence) and not isinstance(aliases, str):
            values.update(aliases)
        if model_id in values:
            return True
    return False
