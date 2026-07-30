from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class LoadSettingsTests(unittest.TestCase):
    required_environment = {
        "OPENAI_API_KEY": "test-key",
        "OPENAI_MODEL": "gpt-test",
        "DEMO_USERNAME": "demo",
        "DEMO_PASSWORD": "secret",
        "SESSION_SECRET": "signing-secret",
    }

    def test_loads_required_environment_and_documented_defaults(self) -> None:
        with patch.dict(os.environ, self.required_environment, clear=True):
            from file_agent.config import load_settings

            settings = load_settings()

        self.assertEqual(settings.openai_api_key, "test-key")
        self.assertEqual(settings.openai_base_url, "https://api.openai.com/v1")
        self.assertEqual(settings.openai_model, "gpt-test")
        self.assertEqual(settings.workspace_seed_path.as_posix(), "workspace")
        self.assertEqual(settings.runtime_root.as_posix(), "/tmp/file-agent")
        self.assertEqual(settings.session_ttl_seconds, 3600)
        self.assertEqual(settings.max_llm_calls, 20)
        self.assertEqual(settings.max_tool_calls, 80)
        self.assertEqual(settings.run_timeout_seconds, 300)
        self.assertEqual(settings.max_run_file_content_bytes, 262_144)

    def test_reports_all_missing_required_settings(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            from file_agent.config import ConfigurationError, load_settings

            with self.assertRaises(ConfigurationError) as raised:
                load_settings()

        self.assertEqual(
            str(raised.exception),
            "Missing required environment variables: "
            "DEMO_PASSWORD, DEMO_USERNAME, OPENAI_API_KEY, OPENAI_MODEL, "
            "SESSION_SECRET",
        )

    def test_rejects_non_positive_numeric_limits_with_setting_name(self) -> None:
        environment = {**self.required_environment, "MAX_TOOL_CALLS": "0"}

        with patch.dict(os.environ, environment, clear=True):
            from file_agent.config import ConfigurationError, load_settings

            with self.assertRaises(ConfigurationError) as raised:
                load_settings()

        self.assertEqual(
            str(raised.exception),
            "MAX_TOOL_CALLS must be a positive integer; got '0'",
        )

    def test_settings_representation_redacts_secrets(self) -> None:
        with patch.dict(os.environ, self.required_environment, clear=True):
            from file_agent.config import load_settings

            representation = repr(load_settings())

        self.assertNotIn("test-key", representation)
        self.assertNotIn("secret", representation)
        self.assertNotIn("signing-secret", representation)


if __name__ == "__main__":
    unittest.main()
