import unittest
from unittest.mock import patch

from google.genai import types

from omar_ai_core import runtime


class ReasoningConfigurationTests(unittest.TestCase):
    def test_default_thinking_level_is_medium(self):
        with patch.object(runtime, "get_secret", return_value="MEDIUM"):
            self.assertEqual(runtime._configured_thinking_level(), types.ThinkingLevel.MEDIUM)

    def test_invalid_thinking_level_falls_back_to_medium(self):
        with patch.object(runtime, "get_secret", return_value="unsupported"):
            self.assertEqual(runtime._configured_thinking_level(), types.ThinkingLevel.MEDIUM)

    def test_planning_and_memory_tools_are_exposed(self):
        names = {item["name"] for item in runtime.TOOL_DECLARATIONS}
        self.assertTrue({"task_plan", "recall_memory", "forget_memory"}.issubset(names))


if __name__ == "__main__":
    unittest.main()
