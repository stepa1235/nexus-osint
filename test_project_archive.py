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
                store.add_note_version(project, "Старая заметка", 40)
                evidence = store.add(project, "Материал", "file:///report", "Сводка за июль", collected=42)
                store.save_event(project, "2024-07-10", "Сводка", "", evidence)
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
                versions = store.note_versions(imported)
                self.assertEqual([(row["body"], row["created"]) for row in versions], [("Старая заметка", 40)])
                copied = store.evidence(imported)[0]
                self.assertNotEqual(copied["id"], evidence)
                self.assertEqual(copied["collected"], 42)
                event = store.timeline(imported)[0]
                self.assertEqual(event["date"], "2024-07-10")
                self.assertEqual(event["evidence"], copied["id"])
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

    def test_older_package_without_note_versions_still_imports(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "project.nexus.json"
            db = Path(folder) / "projects.sqlite3"
            store = Store(db)
            with closing(store.db):
                project = store.project("Оригинал")
                export_project(db, project, path)
                payload = json.loads(path.read_bytes())
                del payload["note_versions"]
                del payload["timeline"]
                path.write_text(json.dumps(payload), encoding="utf-8")
                imported = import_project(db, path)
                self.assertEqual(store.note_versions(imported), [])

    def test_export_cannot_overwrite_active_database(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "projects.sqlite3"
            store = Store(db)
            with closing(store.db):
                project = store.project("Оригинал")
                with self.assertRaises(ValueError):
                    export_project(db, project, db)
                self.assertEqual(store.db.execute("SELECT count(*) FROM projects").fetchone()[0], 1)
