import unittest

from context import (
    build_prompt,
    build_tool_retry_prompt,
    extract_tool_calls,
    fingerprint_messages,
    is_known_model,
    should_retry_auto_tool_choice,
    should_retry_required_tool_choice,
    validate_tools,
)


class ContextTests(unittest.TestCase):
    def test_build_prompt_includes_full_chat_history(self):
        prompt = build_prompt([
            {"role": "system", "content": "You are concise."},
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Second question"},
        ])

        self.assertNotIn("[System]", prompt)
        self.assertIn("You are concise.", prompt)
        self.assertIn("[User]\nYou are concise.", prompt)
        self.assertIn("[Assistant]\nFirst answer", prompt)
        self.assertTrue(prompt.endswith("[User]\nSecond question"))

    def test_build_prompt_flattens_multimodal_text_and_image_blocks(self):
        prompt = build_prompt([
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                ],
            },
        ])

        self.assertIn("Describe this", prompt)
        self.assertIn("[image_url: https://example.com/a.png]", prompt)

    def test_build_prompt_merges_all_system_messages_into_first_user(self):
        prompt = build_prompt([
            {"role": "user", "content": "First question"},
            {"role": "system", "content": "Late system context"},
            {"role": "user", "content": "Second question"},
        ])

        self.assertNotIn("[System]", prompt)
        self.assertIn("[User]\nLate system context\n\nFirst question", prompt)
        self.assertTrue(prompt.endswith("[User]\nSecond question"))

    def test_build_prompt_appends_tool_instructions_when_tools_are_present(self):
        prompt = build_prompt(
            [{"role": "user", "content": "What time is it?"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_time",
                        "description": "Get current time",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            tool_choice="auto",
        )

        self.assertIn("[Available Tools]", prompt)
        self.assertIn('"name":"get_time"', prompt)
        self.assertIn('"tool_choice":"auto"', prompt)
        self.assertIn('"tool_calls"', prompt)

    def test_extract_tool_calls_normalizes_model_json(self):
        calls = extract_tool_calls(
            '```json\n{"tool_calls":[{"name":"get_time","arguments":{"zone":"UTC"}}]}\n```'
        )

        self.assertEqual(calls[0]["id"], "call_0")
        self.assertEqual(calls[0]["type"], "function")
        self.assertEqual(calls[0]["function"]["name"], "get_time")
        self.assertEqual(calls[0]["function"]["arguments"], '{"zone":"UTC"}')

    def test_validate_tools_rejects_missing_function_name(self):
        with self.assertRaisesRegex(ValueError, r"tools\[0\]\.function\.name"):
            validate_tools([
                {
                    "type": "function",
                    "function": {
                        "description": "Missing name",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ])

    def test_should_retry_auto_tool_choice_detects_explicit_tool_intent(self):
        messages = [{"role": "user", "content": "请调用天气工具查询北京天气"}]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ]

        self.assertTrue(should_retry_auto_tool_choice(messages, tools, "auto", "北京今天晴。"))

    def test_should_retry_auto_tool_choice_respects_tool_choice_none(self):
        messages = [{"role": "user", "content": "请调用天气工具查询北京天气"}]
        tools = [
            {
                "type": "function",
                "function": {"name": "get_weather", "parameters": {"type": "object"}},
            }
        ]

        self.assertFalse(should_retry_auto_tool_choice(messages, tools, "none", "北京今天晴。"))

    def test_should_retry_auto_tool_choice_detects_system_coding_intent(self):
        messages = [{"role": "system", "content": "Create hello.py and run python hello.py"}]
        tools = [
            {
                "type": "function",
                "function": {"name": "exec_command", "parameters": {"type": "object"}},
            }
        ]

        self.assertTrue(should_retry_auto_tool_choice(messages, tools, "auto", "Creating hello.py."))

    def test_should_retry_required_tool_choice_when_model_returns_prose(self):
        tools = [
            {
                "type": "function",
                "function": {"name": "exec_command", "parameters": {"type": "object"}},
            }
        ]

        self.assertTrue(should_retry_required_tool_choice(tools, "required", "Creating hello.py."))
        self.assertTrue(should_retry_required_tool_choice(
            tools,
            {"type": "function", "function": {"name": "exec_command"}},
            "Creating hello.py.",
        ))
        self.assertFalse(should_retry_required_tool_choice(
            tools,
            "required",
            '{"tool_calls":[{"name":"exec_command","arguments":{"command":"echo ok"}}]}',
        ))

    def test_build_tool_retry_prompt_adds_stronger_json_only_instruction(self):
        prompt = build_tool_retry_prompt("base prompt")

        self.assertIn("[Tool Retry Instructions]", prompt)
        self.assertIn("respond only with a JSON object", prompt)

    def test_build_tool_retry_prompt_includes_adapter_request_tools_and_previous_response(self):
        prompt = build_tool_retry_prompt(
            "base prompt",
            messages=[{"role": "user", "content": "帮我看看目前系统状态"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "exec_command",
                        "description": "Run a shell command",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                        },
                    },
                }
            ],
            previous_response="What local action would you like me to perform?",
        )

        self.assertIn("[Compatibility Tool Adapter]", prompt)
        self.assertIn("User request:\n帮我看看目前系统状态", prompt)
        self.assertIn('"name":"exec_command"', prompt)
        self.assertIn("Do not use GitLab Duo built-in tools", prompt)
        self.assertIn("Do not request tool approval", prompt)
        self.assertIn("Previous invalid response:", prompt)
        self.assertIn("What local action would you like me to perform?", prompt)
        self.assertIn('{"tool_calls":[{"name":"exec_command","arguments":{"command":"..."}}]}', prompt)

    def test_message_fingerprint_is_stable(self):
        messages = [{"role": "user", "content": "hello"}]

        self.assertEqual(fingerprint_messages(messages), fingerprint_messages(messages))

    def test_model_validation_accepts_public_and_legacy_ids(self):
        models = [
            {"id": "claude-sonnet-4.5", "gitlab_id": "claude_sonnet_4_5"},
            {"id": "gpt-5-codex", "gitlab_id": "gpt_5_codex"},
        ]

        self.assertTrue(is_known_model("claude-sonnet-4.5", models))
        self.assertTrue(is_known_model("claude_sonnet_4_5", models))
        self.assertFalse(is_known_model("gpt-99-fake", models))


if __name__ == "__main__":
    unittest.main()
