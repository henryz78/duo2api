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
