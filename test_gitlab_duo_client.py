import asyncio
import json
import sys
import types
import unittest

sys.modules.setdefault("httpx", types.SimpleNamespace(AsyncClient=object))
sys.modules.setdefault("websockets", types.SimpleNamespace(connect=None))

from context import extract_tool_calls
from gitlab_duo_client import _recv_until_done


def _checkpoint_with_tool(tool_info):
    return json.dumps({
        "channel_values": {
            "ui_chat_log": [
                {
                    "message_type": "agent",
                    "message_id": "agent-1",
                    "content": "Creating `hello.py` and running it with Python.",
                    "status": "success",
                },
                {
                    "message_type": "request",
                    "message_id": "request-call_123",
                    "content": "Tool create_file_with_contents requires approval.",
                    "tool_info": tool_info,
                },
            ]
        }
    })


class FakeWebSocket:
    def __init__(self, frames):
        self.frames = list(frames)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)


class GitLabDuoToolApprovalTests(unittest.TestCase):
    def test_recv_until_done_bridges_create_file_tool_info_to_exec_command(self):
        checkpoint = _checkpoint_with_tool({
            "name": "create_file_with_contents",
            "args": {
                "file_path": "hello.py",
                "contents": "print(\"CODEX_GPT55_OK\")\n",
            },
        })
        frame = json.dumps({
            "newCheckpoint": {
                "status": "TOOL_CALL_APPROVAL_REQUIRED",
                "checkpoint": checkpoint,
            }
        })

        answer, _, _ = asyncio.run(_recv_until_done(FakeWebSocket([frame]), "wf-1"))
        calls = extract_tool_calls(answer)

        self.assertEqual(calls[0]["function"]["name"], "exec_command")
        command = json.loads(calls[0]["function"]["arguments"])["command"]
        self.assertIn("hello.py", command)
        self.assertIn("CODEX_GPT55_OK", command)

    def test_recv_until_done_bridges_run_command_tool_info_to_exec_command(self):
        checkpoint = _checkpoint_with_tool({
            "name": "run_command",
            "args": {
                "program": "bash",
                "args": "-lc 'python3 hello.py'",
            },
        })
        frame = json.dumps({
            "newCheckpoint": {
                "status": "TOOL_CALL_APPROVAL_REQUIRED",
                "checkpoint": checkpoint,
            }
        })

        answer, _, _ = asyncio.run(_recv_until_done(FakeWebSocket([frame]), "wf-1"))
        calls = extract_tool_calls(answer)

        self.assertEqual(calls[0]["function"]["name"], "exec_command")
        command = json.loads(calls[0]["function"]["arguments"])["command"]
        self.assertEqual(command, "bash -lc 'python3 hello.py'")


if __name__ == "__main__":
    unittest.main()
