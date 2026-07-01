import importlib.util
import pathlib
import unittest
import urllib.error


SMOKE_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "openai_compat_smoke.py"
SPEC = importlib.util.spec_from_file_location("openai_compat_smoke", SMOKE_PATH)
smoke = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(smoke)


class OpenAICompatSmokeTests(unittest.TestCase):
    def test_sse_events_parses_named_events_and_done(self):
        events = smoke._sse_events(
            b'event: response.created\n'
            b'data: {"type":"response.created"}\n\n'
            b'data: [DONE]\n\n'
        )

        self.assertEqual(events[0], ("response.created", {"type": "response.created"}))
        self.assertEqual(events[1], ("message", "[DONE]"))

    def test_sse_events_preserves_plain_text_data(self):
        events = smoke._sse_events(b"event: note\ndata: plain text\n\n")

        self.assertEqual(events, [("note", "plain text")])

    def test_safe_body_preview_redacts_common_secret_values(self):
        preview = smoke._safe_body_preview(
            b'{"api_key":"sk-secret","cookie":"_gitlab_session=abc","token":"glpat-secret"}'
        )

        self.assertNotIn("sk-secret", preview)
        self.assertNotIn("_gitlab_session=abc", preview)
        self.assertNotIn("glpat-secret", preview)
        self.assertIn("[REDACTED]", preview)

    def test_http_error_preview_is_redacted(self):
        class FakeHTTPError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("http://example.test", 500, "boom", {}, None)

            def read(self):
                return b'{"api_key":"sk-secret"}'

        def fake_urlopen(request, timeout):
            raise FakeHTTPError()

        original = smoke.urllib.request.urlopen
        smoke.urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(smoke.SmokeError) as ctx:
                smoke._request("http://example.test/v1", "models", api_key="sk-local")
        finally:
            smoke.urllib.request.urlopen = original

        self.assertNotIn("sk-secret", str(ctx.exception))
        self.assertIn("[REDACTED]", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
