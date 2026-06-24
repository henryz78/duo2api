import unittest

from context import (
    build_prompt,
    fingerprint_messages,
)


class ContextTests(unittest.TestCase):
    def test_build_prompt_includes_full_chat_history(self):
        prompt = build_prompt([
            {"role": "system", "content": "You are concise."},
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Second question"},
        ])

        self.assertIn("[System]\nYou are concise.", prompt)
        self.assertIn("[User]\nFirst question", prompt)
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

    def test_message_fingerprint_is_stable(self):
        messages = [{"role": "user", "content": "hello"}]

        self.assertEqual(fingerprint_messages(messages), fingerprint_messages(messages))


if __name__ == "__main__":
    unittest.main()
