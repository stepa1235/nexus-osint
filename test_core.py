import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import Store, analyze, ollama_models
from sources import ct, dns, rdap


class CoreTests(unittest.TestCase):
    def test_persistence_deduplication_and_project_search(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "projects.db"
            store = Store(path)
            first, second = store.project("Первый"), store.project("Второй")
            for _ in range(2):
                store.add(first, "Документ", "file:///test", "Исследование организации")
            store.db.close()
            store = Store(path)
            self.assertEqual(len(store.evidence(first)), 1)
            self.assertEqual(len(store.search(first, 'организации "')), 1)
            self.assertFalse(store.search(second, "организации"))
            store.db.close()

    def test_graph_links_require_same_project_and_source(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "projects.db")
            try:
                first = store.project("Первый")
                second = store.project("Второй")
                origin = store.add_entity(first, "Домен", "example.org")
                destination = store.add_entity(first, "Организация", "Example")
                foreign = store.add_entity(second, "Организация", "Example")
                store.add(second, "Чужой материал", "file:///foreign", "Текст")
                evidence = store.evidence(second)[0]["id"]
                with self.assertRaises(ValueError):
                    store.add_relationship(first, origin, foreign, "Связь", "Гипотеза", None)
                with self.assertRaises(ValueError):
                    store.add_relationship(first, origin, destination, "Связь", "Высокая", None)
                with self.assertRaises(ValueError):
                    store.add_relationship(first, origin, destination, "Связь", "Высокая", evidence)
                store.add_relationship(first, origin, destination, "Связь", "Гипотеза", None)
                self.assertEqual(len(store.relationships(first)), 1)
                self.assertEqual(store.relationships(second), [])
            finally:
                store.db.close()

    def test_delete_project_cleans_related_records_and_fragments(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "projects.db")
            try:
                removed, kept = store.project("Удаляемый"), store.project("Сохраняемый")
                store.add(removed, "Материал", "file:///removed", "Удаляемый текст")
                store.add(kept, "Материал", "file:///kept", "Сохраняемый текст")
                material = store.evidence(removed)[0]["id"]
                origin = store.add_entity(removed, "Домен", "example.org")
                destination = store.add_entity(removed, "Организация", "Example")
                store.add_relationship(removed, origin, destination, "Связь", "Высокая", material)
                store.save_analysis(removed, "Ollama", "model", "Вопрос", [])
                store.delete_project(removed)
                self.assertEqual(store.evidence(removed), [])
                self.assertEqual(store.entities(removed), [])
                self.assertEqual(store.relationships(removed), [])
                self.assertEqual(store.analyses(removed), [])
                self.assertIsNone(store.db.execute(
                    "SELECT 1 FROM fragments WHERE evidence=?", (material,)
                ).fetchone())
                self.assertEqual(len(store.search(kept, "Сохраняемый")), 1)
            finally:
                store.db.close()

    def test_source_urls(self):
        self.assertIn("name=example.org", dns("Example.ORG."))
        self.assertIn("example.org", ct("example.org"))
        self.assertTrue(rdap("8.8.8.8").endswith("/8.8.8.8"))
        with self.assertRaises(ValueError):
            rdap("127.0.0.1")
        with self.assertRaises(ValueError):
            dns("https://example.org")

    @patch("core.request_json")
    def test_cloud_requires_key(self, request):
        with self.assertRaises(ValueError):
            analyze("OpenAI", "test", "test")
        request.assert_not_called()

    @patch("core.request_json", return_value={"message": {"content": "ответ"}})
    def test_ollama_request(self, request):
        options = {"system": "Исследуй источники", "temperature": 0.3, "num_ctx": 4096}
        self.assertEqual(analyze("Ollama", "test", "test", local_options=options), "ответ")
        body = request.call_args.args[1]
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["options"], {"temperature": 0.3, "num_ctx": 4096})
        self.assertTrue(request.call_args.kwargs["local"])
        self.assertEqual(request.call_args.args[0], "http://127.0.0.1:11434/api/chat")


    @patch("core.request_json", return_value={"models": [{"name": "b"}, {"name": "a"}, {"name": "b"}]})
    def test_ollama_models(self, request):
        self.assertEqual(ollama_models(), ["a", "b"])
        request.assert_called_once_with("http://127.0.0.1:11434/api/tags", local=True)
