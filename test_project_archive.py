import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from core import Store
from project_archive import export_project, import_project


class ProjectArchiveTests(unittest.TestCase):
    def test_roundtrip_reassigns_references_and_rebuilds_search(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "project.nexus.json"
            db = Path(folder) / "projects.sqlite3"
            store = Store(db)
            with closing(store.db):
                project = store.project("Источники")
                with store.transaction():
                    store.db.execute("UPDATE projects SET notes='Заметка' WHERE id=?", (project,))
                evidence = store.add(project, "Материал", "file:///report", "Сводка за июль", collected=42)
                a = store.add_entity(project, "Домен", "example.org")
                b = store.add_entity(project, "Организация", "Example")
                store.add_relationship(project, a, b, "публикует", "Средняя", evidence)
                store.save_analysis(project, "Ollama", "model", "Вопрос", [{
                    "text": "Опубликована сводка", "status": "Цитата сверена",
                    "citations": [{"id": evidence, "source": "file:///report", "quote": "Сводка"}],
                }])
                store.begin_api_event(project, "Ollama")
                export_project(db, project, path)
                self.assertNotIn("api_events", json.loads(path.read_bytes()))

                imported = import_project(db, path)
                self.assertNotEqual(imported, project)
                self.assertEqual(store.db.execute(
                    "SELECT notes FROM projects WHERE id=?", (imported,)
                ).fetchone()[0], "Заметка")
                copied = store.evidence(imported)[0]
                self.assertNotEqual(copied["id"], evidence)
                self.assertEqual(copied["collected"], 42)
                self.assertEqual(store.search(imported, "июль")[0]["id"], copied["id"])
                link = store.relationships(imported)[0]
                self.assertNotEqual(link["origin"], a)
                self.assertEqual(link["evidence"], copied["id"])
                citation = json.loads(store.analyses(imported)[0]["claims"])[0]["citations"][0]
                self.assertEqual(citation["id"], copied["id"])
                self.assertEqual(citation["source"], copied["source"])
                self.assertEqual(store.api_events(imported), [])

    def test_damaged_package_does_not_leave_partial_records(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "project.nexus.json"
            db = Path(folder) / "projects.sqlite3"
            store = Store(db)
            with closing(store.db):
                project = store.project("Оригинал")
                store.add(project, "Материал", "file:///report", "Сводка")
                export_project(db, project, path)
                payload = json.loads(path.read_bytes())
                payload["relationships"] = [{"origin": 999, "destination": 1000, "evidence": None}]
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    import_project(db, path)
                self.assertEqual(store.db.execute("SELECT count(*) FROM projects").fetchone()[0], 1)
                self.assertEqual(store.db.execute("SELECT count(*) FROM fragments").fetchone()[0], 1)

    def test_export_cannot_overwrite_active_database(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "projects.sqlite3"
            store = Store(db)
            with closing(store.db):
                project = store.project("Оригинал")
                with self.assertRaises(ValueError):
                    export_project(db, project, db)
                self.assertEqual(store.db.execute("SELECT count(*) FROM projects").fetchone()[0], 1)
