import asyncio
import json
import unittest

try:
    import server
except ModuleNotFoundError as exc:
    server = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipIf(server is None, f"server dependencies unavailable: {_IMPORT_ERROR}")
class ServerToolTests(unittest.TestCase):
    def test_validation_error_message_uses_openai_param_shape(self):
        exc = server.RequestValidationError([
            {"loc": ("body", "messages"), "msg": "Field required", "type": "missing"}
        ])

        param, message = server._validation_error_param_and_message(exc)

        self.assertEqual(param, "messages")
        self.assertEqual(message, "messages: Field required")

    def test_chat_request_accepts_openai_compat_fields(self):
        body = server.ChatRequest(
            model="gpt-5.5",
            messages=[server.Message(role="user", content="hi")],
            stream=True,
            stream_options={"include_usage": True},
            max_completion_tokens=123,
            response_format={"type": "json_object"},
            parallel_tool_calls=False,
        )

        self.assertEqual(body.stream_options, {"include_usage": True})
        self.assertEqual(body.max_completion_tokens, 123)
        self.assertEqual(body.response_format, {"type": "json_object"})
        self.assertFalse(body.parallel_tool_calls)

    def test_chat_request_converts_legacy_functions_to_tools(self):
        body = server.ChatRequest(
            model="gpt-5.5",
            messages=[server.Message(role="user", content="call weather")],
            functions=[{
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            }],
            function_call={"name": "get_weather"},
        )

        tools = server._chat_tools(body)
        tool_choice = server._chat_tool_choice(body)

        self.assertEqual(tools[0]["type"], "function")
        self.assertEqual(tools[0]["function"]["name"], "get_weather")
        self.assertEqual(tool_choice, {"type": "function", "function": {"name": "get_weather"}})

    def test_build_prompt_adds_json_and_token_constraints(self):
        body = server.ChatRequest(
            model="gpt-5.5",
            messages=[server.Message(role="user", content="return data")],
            max_completion_tokens=50,
            response_format={"type": "json_object"},
        )

        prompt = server._build_prompt(body)

        self.assertIn("[Response Constraints]", prompt)
        self.assertIn("valid JSON object", prompt)
        self.assertIn("approximately 50 output tokens", prompt)

    def test_build_prompt_accepts_response_format_schema_field(self):
        body = server.ChatRequest(
            model="gpt-5.5",
            messages=[server.Message(role="user", content="return data")],
            response_format={
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                },
            },
        )

        prompt = server._build_prompt(body)

        self.assertIn("JSON schema", prompt)
        self.assertIn('"answer":{"type":"string"}', prompt)
        self.assertIn('"required":["answer"]', prompt)

    def test_apply_stop_sequences_truncates_at_first_match(self):
        self.assertEqual(
            server._apply_stop_sequences("alpha STOP beta END gamma", ["END", "STOP"]),
            "alpha ",
        )
        self.assertEqual(server._apply_stop_sequences("alpha beta", "STOP"), "alpha beta")

    def test_responses_request_accepts_openai_compat_fields(self):
        body = server.ResponsesRequest(
            model="gpt-5.5",
            input="hi",
            previous_response_id="resp_prev",
            metadata={"trace": "abc"},
            parallel_tool_calls=False,
            reasoning={"effort": "low"},
            store=False,
            truncation="auto",
            text={"format": {"type": "json_object"}},
        )

        self.assertEqual(body.previous_response_id, "resp_prev")
        self.assertEqual(body.metadata, {"trace": "abc"})
        self.assertFalse(body.parallel_tool_calls)
        self.assertEqual(body.reasoning, {"effort": "low"})
        self.assertFalse(body.store)
        self.assertEqual(body.truncation, "auto")
        self.assertEqual(body.text, {"format": {"type": "json_object"}})

    def test_get_model_returns_single_openai_model_object(self):
        originals = {
            "_check_auth": server._check_auth,
            "get_available_models": server.get_available_models,
        }
        server._check_auth = lambda request: None

        async def fake_get_available_models():
            return [{
                "id": "gpt-5.5",
                "gitlab_id": "gpt_5_5",
                "name": "GPT-5.5",
                "owned_by": "openai",
                "model_provider": "OpenAI",
                "cost_indicator": "$$$$",
                "aliases": ["gpt_5_5"],
            }]

        server.get_available_models = fake_get_available_models
        try:
            payload = asyncio.run(server.get_model(object(), "gpt-5.5"))
        finally:
            for name, original in originals.items():
                setattr(server, name, original)

        self.assertEqual(payload["id"], "gpt-5.5")
        self.assertEqual(payload["object"], "model")
        self.assertEqual(payload["owned_by"], "openai")
        self.assertEqual(payload["gitlab_id"], "gpt_5_5")

    def test_do_stream_includes_usage_only_when_requested(self):
        class FakeDuoChat:
            async def stream(self, prompt, model=None):
                yield "hi"

            async def close(self):
                return None

        async def collect(include_usage):
            chunks = []
            async for chunk in server._do_stream(
                "prompt",
                "chatcmpl-test",
                4,
                "gpt-5.5",
                "gpt_5_5",
                include_usage=include_usage,
            ):
                chunks.append(chunk)
            return "".join(chunks)

        original = server.DuoChat
        server.DuoChat = FakeDuoChat
        try:
            with_usage = asyncio.run(collect(True))
            without_usage = asyncio.run(collect(False))
        finally:
            server.DuoChat = original

        self.assertIn('"usage"', with_usage)
        self.assertIn('"choices": []', with_usage)
        self.assertIn('"usage": null', with_usage)
        self.assertIn('"prompt_tokens": 4', with_usage)
        self.assertNotIn('"usage"', without_usage)

    def test_do_complete_returns_legacy_function_call_when_requested(self):
        class FakeDuoChat:
            async def close(self):
                return None

        async def fake_send_with_optional_tool_retry(*args, **kwargs):
            return "", [{
                "id": "call_abc",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city":"北京"}',
                },
            }], 2

        originals = {
            "DuoChat": server.DuoChat,
            "_send_with_optional_tool_retry": server._send_with_optional_tool_retry,
        }
        server.DuoChat = FakeDuoChat
        server._send_with_optional_tool_retry = fake_send_with_optional_tool_retry
        try:
            response = asyncio.run(server._do_complete(
                "prompt",
                "chatcmpl-test",
                4,
                "gpt-5.5",
                "gpt_5_5",
                tools_enabled=True,
                messages=[],
                tools=[{"type": "function", "function": {"name": "get_weather"}}],
                tool_choice="auto",
                legacy_functions=True,
            ))
        finally:
            for name, original in originals.items():
                setattr(server, name, original)

        payload = json.loads(response.body)
        message = payload["choices"][0]["message"]
        self.assertEqual(payload["choices"][0]["finish_reason"], "function_call")
        self.assertEqual(message["function_call"], {"name": "get_weather", "arguments": '{"city":"北京"}'})
        self.assertNotIn("tool_calls", message)

    def test_do_stream_returns_legacy_function_call_when_requested(self):
        class FakeDuoChat:
            async def close(self):
                return None

        async def fake_send_with_optional_tool_retry(*args, **kwargs):
            return "", [{
                "id": "call_abc",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city":"北京"}',
                },
            }], 2

        async def collect():
            chunks = []
            async for chunk in server._do_stream(
                "prompt",
                "chatcmpl-test",
                4,
                "gpt-5.5",
                "gpt_5_5",
                tools_enabled=True,
                messages=[],
                tools=[{"type": "function", "function": {"name": "get_weather"}}],
                tool_choice="auto",
                legacy_functions=True,
                include_usage=True,
            ):
                chunks.append(chunk)
            return "".join(chunks)

        originals = {
            "DuoChat": server.DuoChat,
            "_send_with_optional_tool_retry": server._send_with_optional_tool_retry,
        }
        server.DuoChat = FakeDuoChat
        server._send_with_optional_tool_retry = fake_send_with_optional_tool_retry
        try:
            text = asyncio.run(collect())
        finally:
            for name, original in originals.items():
                setattr(server, name, original)

        self.assertIn('"function_call": {"name": "get_weather", "arguments": "{\\"city\\":\\"北京\\"}"}', text)
        self.assertIn('"finish_reason": "function_call"', text)
        self.assertIn('"choices": []', text)

    def test_do_complete_applies_stop_sequences_for_text(self):
        class FakeDuoChat:
            async def send(self, prompt, model=None):
                return "alpha STOP beta"

            async def close(self):
                return None

        original = server.DuoChat
        server.DuoChat = FakeDuoChat
        try:
            response = asyncio.run(server._do_complete(
                "prompt",
                "chatcmpl-test",
                4,
                "gpt-5.5",
                "gpt_5_5",
                stop="STOP",
            ))
        finally:
            server.DuoChat = original

        payload = json.loads(response.body)
        self.assertEqual(payload["choices"][0]["message"]["content"], "alpha ")

    def test_status_light_is_local_and_redacts_secrets(self):
        called_probe = False

        async def fake_probe(deep=False):
            nonlocal called_probe
            called_probe = True
            return {"ok": True}

        originals = {
            "_check_auth": server._check_auth,
            "_load_config": server._load_config,
            "probe_gitlab_auth": server.probe_gitlab_auth,
            "_version_status": server._version_status,
            "model_cache_status": server.model_cache_status,
        }
        server._check_auth = lambda request, require_configured_keys=False: None
        server._load_config = lambda: {
            "gitlab": {
                "namespace_id": "135911158",
                "model": "gpt-5.5",
                "cookies": {
                    "_gitlab_session": "secret-session",
                    "remember_user_token": "",
                },
            },
            "server": {"api_keys": ["sk-secret"]},
        }
        server.probe_gitlab_auth = fake_probe
        server._version_status = lambda: {"commit": "abc123", "branch": "main"}
        server.model_cache_status = lambda: {
            "cache_ttl_seconds": 300,
            "has_cached_models": False,
            "cached_count": 0,
            "expires_in_seconds": 0,
            "fallback_count": 18,
        }
        try:
            payload = asyncio.run(server.service_status(object(), deep=False))
        finally:
            for name, original in originals.items():
                setattr(server, name, original)

        serialized = json.dumps(payload)
        self.assertFalse(called_probe)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["version"]["commit"], "abc123")
        self.assertTrue(payload["features"]["responses_api"])
        self.assertTrue(payload["features"]["responses_non_stream"])
        self.assertTrue(payload["features"]["responses_text_sse_done_events"])
        self.assertTrue(payload["features"]["single_model_endpoint"])
        self.assertTrue(payload["features"]["chat_stream_usage"])
        self.assertTrue(payload["features"]["chat_legacy_functions"])
        self.assertTrue(payload["features"]["chat_legacy_function_call_response"])
        self.assertTrue(payload["features"]["prompted_response_format"])
        self.assertTrue(payload["features"]["prompted_token_limits"])
        self.assertTrue(payload["features"]["chat_stop_non_stream"])
        self.assertEqual(payload["features"]["duo_tool_bridge"], ["create_file_with_contents", "run_command"])
        self.assertTrue(payload["config"]["has_namespace_id"])
        self.assertTrue(payload["config"]["has_session_cookie"])
        self.assertFalse(payload["config"]["has_remember_token"])
        self.assertEqual(payload["config"]["api_keys_count"], 1)
        self.assertEqual(payload["config"]["default_model"], "gpt-5.5")
        self.assertNotIn("secret-session", serialized)
        self.assertNotIn("sk-secret", serialized)

    def test_status_deep_includes_gitlab_health(self):
        seen: dict[str, object] = {}

        async def fake_probe(deep=False):
            seen["deep"] = deep
            return {
                "ok": True,
                "gitlab_authenticated": True,
                "checks": {"csrf_token": True, "workflow": True},
            }

        originals = {
            "_check_auth": server._check_auth,
            "_load_config": server._load_config,
            "probe_gitlab_auth": server.probe_gitlab_auth,
            "_version_status": server._version_status,
            "model_cache_status": server.model_cache_status,
        }
        server._check_auth = lambda request, require_configured_keys=False: None
        server._load_config = lambda: {
            "gitlab": {"namespace_id": "135911158", "model": "gpt-5.5", "cookies": {}},
            "server": {"api_keys": ["sk-secret"]},
        }
        server.probe_gitlab_auth = fake_probe
        server._version_status = lambda: {"commit": "abc123", "branch": "main"}
        server.model_cache_status = lambda: {
            "cache_ttl_seconds": 300,
            "has_cached_models": False,
            "cached_count": 0,
            "expires_in_seconds": 0,
            "fallback_count": 18,
        }
        try:
            payload = asyncio.run(server.service_status(object(), deep=True))
        finally:
            for name, original in originals.items():
                setattr(server, name, original)

        self.assertTrue(seen["deep"])
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["gitlab_health"]["gitlab_authenticated"])
        self.assertTrue(payload["gitlab_health"]["checks"]["workflow"])

    def test_responses_filters_nameless_tools_before_validation(self):
        captured: dict[str, object] = {}
        named_tool = {
            "type": "function",
            "function": {
                "name": "exec_command",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            },
        }

        def fake_stream(*args, **kwargs):
            captured["tools"] = kwargs["tools"]

            async def iterator():
                yield ""

            return iterator()

        originals = {
            "_check_auth": server._check_auth,
            "_load_config": server._load_config,
            "get_available_models": server.get_available_models,
            "is_known_model": server.is_known_model,
            "resolve_gitlab_model_id": server.resolve_gitlab_model_id,
            "_do_responses_stream": server._do_responses_stream,
        }
        server._check_auth = lambda request: None
        server._load_config = lambda: {"gitlab": {"model": "gpt-5.5"}}

        async def fake_get_available_models():
            return []

        server.get_available_models = fake_get_available_models
        server.is_known_model = lambda model, models: True
        server.resolve_gitlab_model_id = lambda model, models: "gpt_5_5"
        server._do_responses_stream = fake_stream
        try:
            response = asyncio.run(server.responses(
                object(),
                server.ResponsesRequest(
                    model="gpt-5.5",
                    input="Create hello.py",
                    tools=[
                        {"type": "computer_use_preview"},
                        {"type": "function", "function": {}},
                        named_tool,
                    ],
                    tool_choice="auto",
                ),
            ))
        finally:
            for name, original in originals.items():
                setattr(server, name, original)

        self.assertEqual(type(response).__name__, "StreamingResponse")
        self.assertEqual(captured["tools"], [named_tool])

    def test_responses_stream_false_returns_json_response(self):
        captured: dict[str, object] = {}

        async def fake_complete(*args, **kwargs):
            captured["called"] = True
            captured["tools"] = kwargs["tools"]
            return server.JSONResponse(content={"object": "response", "status": "completed"})

        named_tool = {
            "type": "function",
            "function": {
                "name": "exec_command",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            },
        }
        originals = {
            "_check_auth": server._check_auth,
            "_load_config": server._load_config,
            "get_available_models": server.get_available_models,
            "is_known_model": server.is_known_model,
            "resolve_gitlab_model_id": server.resolve_gitlab_model_id,
            "_do_responses_complete": server._do_responses_complete,
        }
        server._check_auth = lambda request: None
        server._load_config = lambda: {"gitlab": {"model": "gpt-5.5"}}

        async def fake_get_available_models():
            return []

        server.get_available_models = fake_get_available_models
        server.is_known_model = lambda model, models: True
        server.resolve_gitlab_model_id = lambda model, models: "gpt_5_5"
        server._do_responses_complete = fake_complete
        try:
            response = asyncio.run(server.responses(
                object(),
                server.ResponsesRequest(
                    model="gpt-5.5",
                    input="Create hello.py",
                    stream=False,
                    tools=[named_tool],
                    tool_choice="auto",
                ),
            ))
        finally:
            for name, original in originals.items():
                setattr(server, name, original)

        self.assertEqual(type(response).__name__, "JSONResponse")
        self.assertTrue(captured["called"])
        self.assertEqual(captured["tools"], [named_tool])

    def test_do_responses_complete_returns_text_json(self):
        class FakeDuoChat:
            async def send(self, prompt, model=None):
                return "hello from responses"

            async def close(self):
                return None

        original = server.DuoChat
        server.DuoChat = FakeDuoChat
        try:
            response = asyncio.run(server._do_responses_complete(
                "prompt",
                "resp_test",
                4,
                "gpt-5.5",
                "gpt_5_5",
            ))
        finally:
            server.DuoChat = original

        payload = json.loads(response.body)
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["output"][0]["type"], "message")
        self.assertEqual(payload["output"][0]["content"][0]["text"], "hello from responses")
        self.assertEqual(payload["usage"]["input_tokens"], 4)

    def test_do_responses_complete_returns_function_call_json(self):
        class FakeDuoChat:
            async def close(self):
                return None

        async def fake_send_with_optional_tool_retry(*args, **kwargs):
            return "", [{
                "id": "call_abc",
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "arguments": '{"command":"python3 hello.py"}',
                },
            }], 2

        tools = [{
            "type": "function",
            "function": {
                "name": "exec_command",
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                },
            },
        }]
        originals = {
            "DuoChat": server.DuoChat,
            "_send_with_optional_tool_retry": server._send_with_optional_tool_retry,
        }
        server.DuoChat = FakeDuoChat
        server._send_with_optional_tool_retry = fake_send_with_optional_tool_retry
        try:
            response = asyncio.run(server._do_responses_complete(
                "prompt",
                "resp_test",
                4,
                "gpt-5.5",
                "gpt_5_5",
                tools_enabled=True,
                messages=[],
                tools=tools,
                tool_choice="auto",
            ))
        finally:
            for name, original in originals.items():
                setattr(server, name, original)

        payload = json.loads(response.body)
        item = payload["output"][0]
        self.assertEqual(item["type"], "function_call")
        self.assertEqual(item["call_id"], "call_abc")
        self.assertEqual(item["name"], "exec_command")
        self.assertEqual(json.loads(item["arguments"]), {"cmd": "python3 hello.py"})

    def test_auto_tool_choice_retries_once_when_tool_intent_is_explicit(self):
        class FakeDuoChat:
            prompts: list[str] = []

            async def send(self, prompt, model=None):
                self.prompts.append(prompt)
                if len(self.prompts) == 1:
                    return "北京今天晴。"
                return '{"tool_calls":[{"name":"get_weather","arguments":{"city":"北京"}}]}'

            async def close(self):
                return None

        original = server.DuoChat
        server.DuoChat = FakeDuoChat
        try:
            response = asyncio.run(server._do_complete(
                "base prompt",
                "chatcmpl-test",
                3,
                "claude-sonnet-4.5",
                "claude_sonnet_4_5",
                tools_enabled=True,
                messages=[{"role": "user", "content": "请调用天气工具查询北京天气"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                        },
                    }
                ],
                tool_choice="auto",
            ))
        finally:
            server.DuoChat = original

        payload = json.loads(response.body)
        self.assertEqual(payload["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "get_weather")


if __name__ == "__main__":
    unittest.main()
