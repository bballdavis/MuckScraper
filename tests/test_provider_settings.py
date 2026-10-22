"""Tests for provider settings saved from Settings › LLM providers:
encryption of saved keys (aggregator/secret_box.py), validation
(aggregator/llm_config.py) and how llm_client applies overrides."""

import os
import unittest
from unittest import mock

from news_fetcher import llm_client


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
        from aggregator.llm_config import validate
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

        llm_client.set_config_source(source)
        try:
            self.assertEqual(llm_client.provider_for_tier(llm_client.TIER_QUALITY), "groq")
            self.assertTrue(llm_client.is_configured(llm_client.TIER_QUALITY))
        finally:
            llm_client.set_config_source(None)

    def test_failing_source_keeps_settings(self):
        llm_client.configure({"LLM_PROVIDER": "gemini"})

        def source():
            raise RuntimeError("db down")

        llm_client.set_config_source(source)
        try:
            self.assertEqual(llm_client.provider_for_tier(llm_client.TIER_QUALITY), "gemini")
        finally:
            llm_client.set_config_source(None)


if __name__ == "__main__":
    unittest.main()
