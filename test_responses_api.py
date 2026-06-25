import unittest

import responses_api
from responses_api import (
    build_responses_prompt,
    response_function_call_sse,
    responses_input_to_messages,
)


class ResponsesApiTests(unittest.TestCase):
    def test_responses_named_tools_keeps_only_tools_with_names(self):
        helper = getattr(responses_api, "responses_named_tools", None)
        self.assertIsNotNone(helper)
        named_tool = {
            "type": "function",
            "function": {
                "name": "exec_command",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            },
        }

        self.assertEqual(
            helper([
                {"type": "computer_use_preview"},
                {"type": "function", "function": {}},
                {"type": "custom", "name": "apply_patch", "format": {"type": "grammar"}},
                named_tool,
            ]),
            [named_tool],
        )

    def test_build_responses_prompt_includes_instructions_as_system_context(self):
        prompt = build_responses_prompt({
            "instructions": "You are Codex. Use tools for local file operations.",
            "input": "Create hello.py and run it.",
        })

        self.assertIn("You are Codex. Use tools for local file operations.", prompt)
        self.assertIn("[User]\nYou are Codex. Use tools for local file operations.\n\nCreate hello.py", prompt)

    def test_responses_input_to_messages_preserves_task_and_tool_output(self):
        messages = responses_input_to_messages([
            {"type": "message", "role": "developer", "content": "Follow safety rules."},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Create hello.py"}]},
            {
                "type": "function_call",
                "call_id": "call_123",
                "name": "exec_command",
                "arguments": '{"cmd":"ls"}',
            },
            {"type": "function_call_output", "call_id": "call_123", "output": "hello.py\n"},
        ])

        self.assertEqual(messages[0], {"role": "system", "content": "Follow safety rules."})
        self.assertEqual(messages[1], {"role": "user", "content": "Create hello.py"})
        self.assertEqual(messages[2]["tool_calls"][0]["function"]["name"], "exec_command")
        self.assertEqual(messages[3]["role"], "tool")
        self.assertEqual(messages[3]["tool_call_id"], "call_123")
        self.assertIn("Previous local tool call call_123 completed.", messages[3]["content"])
        self.assertIn("hello.py", messages[3]["content"])
        self.assertIn("Continue the original user request", messages[3]["content"])
        self.assertIn("Do not repeat completed tool calls", messages[3]["content"])

    def test_build_responses_prompt_tells_model_to_continue_after_tool_output(self):
        prompt = build_responses_prompt({
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": "Create hello.py containing print(\"CODEX_GPT55_OK\"), then run it.",
                },
                {
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "exec_command",
                    "arguments": '{"command":"write hello.py"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_123",
                    "output": "hello.py created successfully",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "exec_command", "parameters": {"type": "object"}},
                }
            ],
            "tool_choice": "required",
        })

        self.assertIn("[Tool Result call_123]", prompt)
        self.assertIn("Previous local tool call call_123 completed.", prompt)
        self.assertIn("Continue the original user request", prompt)
        self.assertIn("Do not repeat completed tool calls", prompt)

    def test_build_responses_prompt_includes_exec_command_tool(self):
        prompt = build_responses_prompt({
            "input": "List files",
            "tools": [
                {
                    "type": "function",
                    "name": "exec_command",
                    "parameters": {
                        "type": "object",
                        "properties": {"cmd": {"type": "string"}},
                    },
                }
            ],
            "tool_choice": "auto",
        })

        self.assertIn("[User]\nList files", prompt)
        self.assertIn('"name":"exec_command"', prompt)
        self.assertIn("[Tool Calling Instructions]", prompt)

    def test_response_function_call_sse_uses_responses_events(self):
        text = response_function_call_sse(
            "resp_123",
            "gpt-5.5",
            123456,
            {
                "id": "call_abc",
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "arguments": '{"cmd":"ls"}',
                },
            },
            {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        )

        self.assertIn("event: response.output_item.added", text)
        self.assertIn("event: response.function_call_arguments.delta", text)
        self.assertIn("event: response.function_call_arguments.done", text)
        self.assertIn("event: response.completed", text)
        self.assertIn('"call_id": "call_abc"', text)
        self.assertIn('"name": "exec_command"', text)
        self.assertIn('"arguments": "{\\"cmd\\":\\"ls\\"}"', text)


if __name__ == "__main__":
    unittest.main()
