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
