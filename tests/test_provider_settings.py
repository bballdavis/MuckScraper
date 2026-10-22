"""Tests for provider settings saved from Settings › LLM providers:
encryption of saved keys (aggregator/secret_box.py), validation
(aggregator/config_store.py) and how llm_client applies overrides."""

import os
import unittest
from unittest import mock

from news_fetcher import llm_client, saved_settings


class SecretBoxTests(unittest.TestCase):
    def test_round_trip_with_derived_key(self):
        from aggregator import secret_box
        with mock.patch.dict(os.environ, {"SECRET_KEY": "s" * 32, "CONFIG_ENCRYPTION_KEY": ""}):
            token = secret_box.encrypt("sk-ant-secret-1234")
            self.assertNotIn("sk-ant-secret", token)
            self.assertEqual(secret_box.decrypt(token), "sk-ant-secret-1234")
            self.assertEqual(secret_box.key_source(), "SECRET_KEY")

    def test_explicit_key_and_key_change(self):
        from cryptography.fernet import Fernet
        from aggregator import secret_box
        with mock.patch.dict(os.environ, {"SECRET_KEY": "s" * 32, "CONFIG_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
            token = secret_box.encrypt("gsk_abc")
            self.assertEqual(secret_box.key_source(), "CONFIG_ENCRYPTION_KEY")
            self.assertEqual(secret_box.decrypt(token), "gsk_abc")
        with mock.patch.dict(os.environ, {"SECRET_KEY": "s" * 32, "CONFIG_ENCRYPTION_KEY": ""}):
            # Another key can't read it: None, not an exception.
            self.assertIsNone(secret_box.decrypt(token))


class ValidateTests(unittest.TestCase):
    def test_kinds(self):
        from aggregator.config_store import validate
        self.assertIsNone(validate("LLM_PROVIDER", "anthropic"))
        self.assertIsNotNone(validate("LLM_PROVIDER", "openai"))
        self.assertIsNone(validate("LLM_FAST_PROVIDER", "main"))
        self.assertIsNotNone(validate("LLM_PROVIDER", "main"))
        self.assertIsNone(validate("OLLAMA_HOST", "http://10.0.0.5:11434", "url"))
        self.assertIsNotNone(validate("OLLAMA_HOST", "javascript:alert(1)", "url"))
        self.assertIsNone(validate("OLLAMA_MODEL", "gemma4:12b", "model"))
        self.assertIsNotNone(validate("OLLAMA_MODEL", "two words", "model"))
        self.assertIsNotNone(validate("GROQ_API_KEY", "has space", "secret"))
        self.assertIsNone(validate("GROQ_API_KEY", "", "secret"))
        self.assertIsNone(validate("NYT_API_DAILY_LIMIT", "450", "int"))
        self.assertIsNotNone(validate("NYT_API_DAILY_LIMIT", "-1", "int"))
        self.assertIsNotNone(validate("NYT_API_DAILY_LIMIT", "lots", "int"))


class SavedSettingsTests(unittest.TestCase):
    def tearDown(self):
        saved_settings.set_source(None)

    def test_saved_value_then_environment_then_default(self):
        saved_settings.set_source(lambda: {"NYT_API_KEY": "saved-key"})
        with mock.patch.dict(os.environ, {"NYT_API_KEY": "env-key", "GNEWS_API_KEY": "env-gnews"}):
            self.assertEqual(saved_settings.get("NYT_API_KEY"), "saved-key")
            self.assertEqual(saved_settings.get("GNEWS_API_KEY"), "env-gnews")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(saved_settings.get("NYT_API_DAILY_LIMIT", "450"), "450")

    def test_unchanged_source_keeps_last_values(self):
        answers = iter([{"NEWS_API_KEY": "k1"}, None, None])
        saved_settings.set_source(lambda: next(answers))
        self.assertEqual(saved_settings.get("NEWS_API_KEY"), "k1")
        self.assertEqual(saved_settings.get("NEWS_API_KEY"), "k1")


class ConfigureTests(unittest.TestCase):
    def tearDown(self):
        llm_client.configure()

    def test_override_beats_environment_and_blank_falls_back(self):
        env = {"LLM_PROVIDER": "ollama", "ANTHROPIC_MODEL": "env-model", "LLM_FAST_PROVIDER": "groq"}
        with mock.patch.dict(os.environ, env):
            llm_client.configure({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k", "ANTHROPIC_MODEL": ""})
            self.assertEqual(llm_client.LLM_PROVIDER, "anthropic")
            self.assertEqual(llm_client.ANTHROPIC_MODEL, "env-model")
            self.assertEqual(llm_client.ANTHROPIC_FAST_MODEL, "env-model")
            self.assertEqual(llm_client.LLM_FAST_PROVIDER, "groq")

    def test_same_as_main_undoes_environment_fast_provider(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "LLM_FAST_PROVIDER": "groq"}):
            llm_client.configure({"LLM_PROVIDER": "gemini", "LLM_FAST_PROVIDER": "main"})
            self.assertEqual(llm_client.LLM_FAST_PROVIDER, "gemini")

    def test_config_source_applies_on_sync(self):
        calls = []

        def source():
            calls.append(1)
            return {"LLM_PROVIDER": "groq", "GROQ_API_KEY": "x", "GROQ_MODEL": "m"} if len(calls) == 1 else None

        saved_settings.set_source(source)
        try:
            self.assertEqual(llm_client.provider_for_tier(llm_client.TIER_QUALITY), "groq")
            self.assertTrue(llm_client.is_configured(llm_client.TIER_QUALITY))
        finally:
            saved_settings.set_source(None)

    def test_failing_source_keeps_settings(self):
        calls = []

        def source():
            calls.append(1)
            if len(calls) == 1:
                return {"LLM_PROVIDER": "gemini"}
            raise RuntimeError("db down")

        saved_settings.set_source(source)
        try:
            self.assertEqual(llm_client.provider_for_tier(llm_client.TIER_QUALITY), "gemini")
            # The next read fails; the last good settings stay in force.
            self.assertEqual(llm_client.provider_for_tier(llm_client.TIER_QUALITY), "gemini")
            self.assertEqual(len(calls), 2)
        finally:
            saved_settings.set_source(None)


if __name__ == "__main__":
    unittest.main()


class LmStudioTests(unittest.TestCase):
    """LM Studio: chat without a key, embeddings while another provider
    writes, and a refused embedding of the wrong size."""

    def tearDown(self):
        llm_client.configure()

    def _response(self, payload):
        response = mock.Mock(status_code=200, ok=True)
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return response

    def test_embeddings_from_lmstudio_while_chat_uses_anthropic(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            llm_client.configure({
                "LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k", "ANTHROPIC_MODEL": "m",
                "EMBEDDING_PROVIDER": "lmstudio", "LMSTUDIO_HOST": "http://10.0.0.9:1234/v1",
                "LMSTUDIO_EMBEDDING_MODEL": "text-embedding-nomic-embed-text-v1.5",
            })
            self.assertFalse(llm_client.uses_ollama())
            self.assertEqual(llm_client.embedding_model_name(), "text-embedding-nomic-embed-text-v1.5")
            with mock.patch.object(llm_client, "_sync_config"), \
                 mock.patch.object(llm_client.requests, "post",
                                   return_value=self._response({"data": [{"embedding": [0.1] * 768}]})) as post:
                self.assertEqual(len(llm_client.get_embedding("hello")), 768)
            url = post.call_args.args[0]
            self.assertEqual(url, "http://10.0.0.9:1234/v1/embeddings")
            self.assertEqual(post.call_args.kwargs["json"]["input"], "hello")
            self.assertNotIn("Authorization", post.call_args.kwargs["headers"])

    def test_wrong_size_embedding_is_refused(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            llm_client.configure({"EMBEDDING_PROVIDER": "lmstudio", "LMSTUDIO_EMBEDDING_MODEL": "big-model"})
            with mock.patch.object(llm_client, "_sync_config"), \
                 mock.patch.object(llm_client.requests, "post",
                                   return_value=self._response({"data": [{"embedding": [0.1] * 1024}]})):
                self.assertIsNone(llm_client.get_embedding("hello"))

    def test_chat_needs_a_model_but_no_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            llm_client.configure({"LLM_PROVIDER": "lmstudio"})
            self.assertEqual(llm_client.LMSTUDIO_HOST, "http://localhost:1234/v1")
            self.assertFalse(llm_client.is_configured())
            llm_client.configure({"LLM_PROVIDER": "lmstudio", "LMSTUDIO_MODEL": "qwen3-8b"})
            self.assertTrue(llm_client.is_configured())
            reply = self._response({"choices": [{"message": {"content": " OK "}}]})
            with mock.patch.object(llm_client, "_sync_config"), \
                 mock.patch.object(llm_client.requests, "post", return_value=reply) as post:
                self.assertEqual(llm_client.generate_text("Say OK"), "OK")
            self.assertTrue(post.call_args.args[0].endswith("/chat/completions"))
            self.assertNotIn("Authorization", post.call_args.kwargs["headers"])

    def test_validation(self):
        from aggregator.config_store import validate
        self.assertIsNone(validate("EMBEDDING_PROVIDER", "lmstudio"))
        self.assertIsNotNone(validate("EMBEDDING_PROVIDER", "anthropic"))
        self.assertIsNone(validate("LLM_PROVIDER", "lmstudio"))
