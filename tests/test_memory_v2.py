import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omar_ai_core.memory import store


class MemoryV2Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.db_path = root / "memory" / "jarvis-memory.db"
        self.legacy_path = root / "memory" / "long_term.json"
        self.patches = (
            patch.object(store, "MEMORY_DB_PATH", self.db_path),
            patch.object(store, "MEMORY_PATH", self.legacy_path),
        )
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def test_memory_is_not_trimmed_to_the_old_character_limit(self):
        for index in range(40):
            store.update_memory(
                {
                    "projects": {
                        f"project_{index}": {
                            "value": f"Detailed project number {index} " + "x" * 120,
                            "importance": 0.6,
                        }
                    }
                }
            )
        memories = store.load_memory()
        self.assertEqual(len(memories["projects"]), 40)
        self.assertGreater(len(json.dumps(memories)), 2200)

    def test_retrieval_returns_relevant_memories_only(self):
        store.update_memory(
            {
                "preferences": {
                    "music": {"value": "Prefers instrumental jazz while working"},
                    "food": {"value": "Likes vegetable pizza"},
                },
                "projects": {
                    "jarvis": {"value": "Building a Windows voice assistant"},
                },
            }
        )
        results = store.search_memories("what music do I prefer while working", limit=3)
        self.assertTrue(results)
        self.assertEqual(results[0]["key"], "music")
        self.assertNotIn("food", {item["key"] for item in results})

    def test_legacy_json_is_migrated_once(self):
        self.legacy_path.parent.mkdir(parents=True)
        self.legacy_path.write_text(
            json.dumps(
                {
                    "identity": {"name": {"value": "Javier", "updated": "2026-07-20"}},
                    "preferences": {},
                    "projects": {},
                    "relationships": {},
                    "wishes": {},
                    "notes": {},
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(store.load_memory()["identity"]["name"]["value"], "Javier")
        store.update_memory({"identity": {"name": {"value": "Javi"}}})
        self.assertEqual(store.load_memory()["identity"]["name"]["value"], "Javi")

    def test_secrets_are_refused(self):
        result = store.remember("gemini_api_key", "AIza" + "x" * 36, "notes")
        self.assertIn("Sensitive", result)
        self.assertEqual(store.list_memories(), [])

    def test_memory_can_be_edited_and_deleted(self):
        store.remember("city", "Barcelona", "identity", importance=0.8)
        store.remember("city", "Valencia", "identity", importance=0.8)
        entry = store.list_memories()[0]
        self.assertEqual(entry["value"], "Valencia")
        self.assertTrue(store.delete_memory(memory_id=entry["id"]))
        self.assertEqual(store.list_memories(), [])


if __name__ == "__main__":
    unittest.main()
