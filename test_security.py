import json
import tempfile
import unittest
from pathlib import Path

from security import (
    apply_config_update,
    auth_keys_from_config,
    clear_auth_cache,
    estimate_tokens,
    public_config_status,
    public_upstream_error_message,
    redact_secret,
)


class SecurityTests(unittest.TestCase):
    def test_redact_secret_shows_only_status(self):
        self.assertEqual(redact_secret(""), "")
        self.assertEqual(redact_secret("需填"), "")
        self.assertEqual(redact_secret("abcdef1234567890"), "abcd...7890")

    def test_estimate_tokens_uses_utf8_bytes_for_cjk(self):
        english = estimate_tokens("hello world")
        chinese = estimate_tokens("你好世界")

        self.assertGreaterEqual(chinese, english)
        self.assertEqual(estimate_tokens(""), 1)

    def test_public_upstream_error_message_hides_raw_upstream_details(self):
        raw = RuntimeError('Failed to create workflow (404): {"message":"404 Namespace Not Found"}')

        message = public_upstream_error_message(raw)

        self.assertIn("GitLab Duo upstream request failed", message)
        self.assertNotIn("/api/v4", message)
        self.assertNotIn("Namespace Not Found", message)

    def test_public_config_status_excludes_runtime_secrets(self):
        cfg = self._config_data()

        status = public_config_status(cfg, available_models=18)
        serialized = json.dumps(status)

        self.assertEqual(status["namespace_id"], "135817766")
        self.assertEqual(status["api_keys_count"], 1)
        self.assertTrue(status["has_session_cookie"])
        self.assertTrue(status["has_remember_token"])
        self.assertNotIn("_gitlab_session", serialized)
        self.assertNotIn("cookie-secret", serialized)
        self.assertNotIn("remember-secret", serialized)
        self.assertNotIn("sk-live-secret-1234", serialized)

    def test_public_config_status_accepts_legacy_config_shape(self):
        cfg = {
            "gitlab": {
                "namespace_id": "135817766",
                "model": "gpt-5.5",
                "session": "legacy-cookie-secret",
            },
            "api_keys": ["sk-legacy-secret"],
        }

        status = public_config_status(cfg, available_models=31)
        serialized = json.dumps(status)

        self.assertTrue(status["has_session_cookie"])
        self.assertEqual(status["api_keys_count"], 1)
        self.assertNotIn("legacy-cookie-secret", serialized)
        self.assertNotIn("sk-legacy-secret", serialized)

    def test_redacted_api_keys_are_rejected_without_overwriting_config(self):
        cfg = self._config_data()

        with self.assertRaises(ValueError):
            apply_config_update(cfg, api_keys=["sk-l...1234"])

        self.assertEqual(cfg["server"]["api_keys"], ["sk-live-secret-1234"])

    def test_redacted_cookies_are_rejected_without_overwriting_config(self):
        cfg = self._config_data()

        with self.assertRaises(ValueError):
            apply_config_update(cfg, gitlab_session="cook...cret")

        self.assertEqual(cfg["gitlab"]["cookies"]["_gitlab_session"], "cookie-secret")

    def test_blank_api_keys_keep_existing_configured_keys(self):
        cfg = self._config_data()

        apply_config_update(
            cfg,
            namespace_id="2468",
            model="gpt-5-codex",
            api_keys=[],
        )

        self.assertEqual(cfg["gitlab"]["namespace_id"], "2468")
        self.assertEqual(cfg["gitlab"]["model"], "gpt-5-codex")
        self.assertEqual(cfg["server"]["api_keys"], ["sk-live-secret-1234"])

    def test_blank_namespace_keeps_existing_configured_value(self):
        cfg = self._config_data()

        apply_config_update(cfg, namespace_id="")

        self.assertEqual(cfg["gitlab"]["namespace_id"], "135817766")

    def test_auth_keys_from_config_uses_ttl_cache_and_can_be_cleared(self):
        with self._temp_config() as config_path:
            first = auth_keys_from_config(config_path, now=100.0)
            with open(config_path) as f:
                cfg = json.load(f)
            cfg["server"]["api_keys"] = ["sk-new-secret"]
            with open(config_path, "w") as f:
                json.dump(cfg, f)
            cached = auth_keys_from_config(config_path, now=101.0)
            clear_auth_cache()
            refreshed = auth_keys_from_config(config_path, now=101.0)

        self.assertEqual(first, {"sk-live-secret-1234"})
        self.assertEqual(cached, {"sk-live-secret-1234"})
        self.assertEqual(refreshed, {"sk-new-secret"})

    def test_auth_keys_from_config_accepts_legacy_root_api_keys(self):
        with self._temp_config({
            "gitlab": {"session": "legacy-cookie-secret", "namespace_id": "135817766"},
            "api_keys": ["sk-legacy-secret"],
        }) as config_path:
            keys = auth_keys_from_config(config_path, now=200.0)

        self.assertEqual(keys, {"sk-legacy-secret"})

    def _config_data(self):
        return {
            "gitlab": {
                "namespace_id": "135817766",
                "model": "claude-sonnet-4.5",
                "cookies": {
                    "_gitlab_session": "cookie-secret",
                    "remember_user_token": "remember-secret",
                },
                "user_agent": "test-agent",
            },
            "server": {
                "host": "0.0.0.0",
                "port": 8000,
                "api_keys": ["sk-live-secret-1234"],
            },
        }

    def _temp_config(self, data=None):
        if data is None:
            data = self._config_data()
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "config.json"
        with open(path, "w") as f:
            json.dump(data, f)

        class TempConfig:
            def __enter__(self):
                return path

            def __exit__(self, exc_type, exc, tb):
                tmp.cleanup()
                clear_auth_cache()

        return TempConfig()


if __name__ == "__main__":
    unittest.main()
