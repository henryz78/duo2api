import unittest

from context import is_known_model
from model_catalog import normalize_graphql_models, resolve_model_id


class ModelCatalogTests(unittest.TestCase):
    def test_normalize_graphql_models_builds_public_ids_and_aliases(self):
        models = normalize_graphql_models({
            "selectableModels": [
                {
                    "ref": "claude_haiku_4_5_20251001",
                    "name": "Claude Haiku 4.5 - Anthropic",
                    "modelProvider": "Anthropic",
                    "modelDescription": "Fast responses.",
                    "costIndicator": "$",
                },
                {
                    "ref": "claude_haiku_4_5_20251001_vertex",
                    "name": "Claude Haiku 4.5 - Gemini Enterprise Agent Platform",
                    "modelProvider": "Gemini Enterprise Agent Platform",
                    "modelDescription": "Fast responses.",
                    "costIndicator": "$",
                },
                {
                    "ref": "gpt_5",
                    "name": "GPT-5.1",
                    "modelProvider": "OpenAI",
                    "modelDescription": "General reasoning.",
                    "costIndicator": "$$",
                },
            ],
            "defaultModel": {
                "ref": "claude_haiku_4_5_20251001_vertex",
                "name": "Claude Haiku 4.5 - Gemini Enterprise Agent Platform",
                "modelProvider": "Gemini Enterprise Agent Platform",
            },
        })

        self.assertEqual(models[0]["id"], "claude-haiku-4.5")
        self.assertEqual(models[0]["gitlab_id"], "claude_haiku_4_5_20251001")
        self.assertIn("claude_haiku_4_5", models[0]["aliases"])
        self.assertEqual(models[1]["id"], "claude-haiku-4.5-vertex")
        self.assertEqual(models[1]["owned_by"], "google")
        self.assertEqual(models[2]["id"], "gpt-5.1")
        self.assertIn("gpt_5_1", models[2]["aliases"])

    def test_model_resolution_accepts_public_refs_and_legacy_aliases(self):
        models = normalize_graphql_models({
            "selectableModels": [
                {
                    "ref": "claude_sonnet_4_5_20250929_vertex",
                    "name": "Claude Sonnet 4.5 - Gemini Enterprise Agent Platform",
                    "modelProvider": "Gemini Enterprise Agent Platform",
                },
                {
                    "ref": "gpt_5",
                    "name": "GPT-5.1",
                    "modelProvider": "OpenAI",
                },
            ]
        })

        self.assertTrue(is_known_model("claude-sonnet-4.5-vertex", models))
        self.assertTrue(is_known_model("claude_sonnet_4_5_20250929_vertex", models))
        self.assertTrue(is_known_model("gpt_5_1", models))
        self.assertEqual(resolve_model_id("claude-sonnet-4.5-vertex", models), "claude_sonnet_4_5_20250929_vertex")
        self.assertEqual(resolve_model_id("gpt-5.1", models), "gpt_5")
        self.assertEqual(resolve_model_id("gpt_5_1", models), "gpt_5")
        self.assertEqual(resolve_model_id("unknown-model", models), "unknown-model")


if __name__ == "__main__":
    unittest.main()
