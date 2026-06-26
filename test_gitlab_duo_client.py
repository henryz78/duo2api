import asyncio
import json
import sys
import types
import unittest

sys.modules.setdefault("httpx", types.SimpleNamespace(AsyncClient=object))
sys.modules.setdefault("websockets", types.SimpleNamespace(connect=None))

from context import extract_tool_calls
from gitlab_duo_client import _recv_until_done, normalize_config


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
    def test_normalize_config_accepts_legacy_session_and_root_api_keys(self):
        cfg = normalize_config({
            "gitlab": {
                "session": "legacy-session",
                "namespace_id": "135911158",
                "model": "gpt-5.5",
            },
            "api_keys": ["sk-legacy"],
        })

        self.assertEqual(cfg["gitlab"]["cookies"]["_gitlab_session"], "legacy-session")
        self.assertEqual(cfg["gitlab"]["cookies"]["remember_user_token"], "")
        self.assertEqual(cfg["server"]["api_keys"], ["sk-legacy"])
        self.assertEqual(cfg["server"]["host"], "0.0.0.0")
        self.assertEqual(cfg["server"]["port"], 8000)

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

    def test_recv_until_done_reports_unknown_tool_info_without_argument_values(self):
        checkpoint = _checkpoint_with_tool({
            "name": "read_file",
            "args": {
                "path": "secret.py",
                "token": "should-not-leak",
            },
        })
        frame = json.dumps({
            "newCheckpoint": {
                "status": "TOOL_CALL_APPROVAL_REQUIRED",
                "checkpoint": checkpoint,
            }
        })

        answer, _, _ = asyncio.run(_recv_until_done(FakeWebSocket([frame]), "wf-1"))

        self.assertIn("Unsupported GitLab Duo tool_info", answer)
        self.assertIn("read_file", answer)
        self.assertIn("path", answer)
        self.assertIn("token", answer)
        self.assertNotIn("secret.py", answer)
        self.assertNotIn("should-not-leak", answer)


if __name__ == "__main__":
    unittest.main()
