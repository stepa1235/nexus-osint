"""Desktop smoke test with temporary app data and local evidence."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import SourceResult, Store

try:
    from app import QApplication, QFileDialog, QInputDialog, QMessageBox, Window
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 is required")
class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_project_import_graph_report_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "source.txt"
            source.write_text("Организация опубликовала отчёт.", encoding="utf-8")
            report = folder / "report.md"
            with patch.dict(os.environ, {"XDG_DATA_HOME": directory}):
                window = Window()
                try:
                    window.show()
                    self.application.processEvents()
                    with patch.object(QInputDialog, "getText", return_value=("Исследование", True)):
                        window.new_project()
                    window.notes.setPlainText("Заметка")
                    with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")):
                        window.import_text()
                    window.work[0].result(timeout=5)
                    window.poll()
                    self.assertEqual(window.items.count(), 1)
                    self.assertEqual(len(window.scene.items()), 3)
                    window.evidence_query.setText("опубликовала")
                    window.search_evidence()
                    self.assertIn(source.as_uri(), window.material.toPlainText())
                    with patch.object(QFileDialog, "getSaveFileName", return_value=(str(report), "")):
                        window.export()
                    text = report.read_text(encoding="utf-8")
                    self.assertIn("Заметка", text)
                    self.assertIn(source.as_uri(), text)
                    for kind, label in (("Домен", "example.org"), ("Организация", "Example")):
                        with patch.object(QInputDialog, "getItem", return_value=(kind, True)):
                            with patch.object(QInputDialog, "getText", return_value=(label, True)):
                                window.new_entity()
                    entities = window.store.entities(window.project)
                    domain, organization = entities
                    evidence = window.store.evidence(window.project)[0]
                    selected = [
                        f'[{domain["id"]}] Домен · example.org',
                        f'[{organization["id"]}] Организация · Example',
                        "Средняя",
                        f'[{evidence["id"]}] {evidence["title"]}',
                    ]
                    with patch.object(QInputDialog, "getItem", side_effect=[(name, True) for name in selected]):
                        with patch.object(QInputDialog, "getText", return_value=("публикует", True)):
                            window.new_relationship()
                    self.assertEqual(len(window.store.relationships(window.project)), 1)
                    window.graph_kind.setCurrentText("Домен")
                    self.assertFalse(any("публикует" in item.toolTip() for item in window.scene.items()))
                    window.graph_kind.setCurrentText("Все типы")
                    self.assertTrue(any("публикует" in item.toolTip() for item in window.scene.items()))
                    graph = folder / "graph.json"
                    with patch.object(QFileDialog, "getSaveFileName", return_value=(str(graph), "JSON")):
                        window.export_graph()
                    exported = json.loads(graph.read_text())
                    self.assertEqual(exported["nodes"][1]["source"], source.as_uri())
                    self.assertEqual(exported["edges"][1]["confidence"], "Средняя")
                    self.assertEqual(exported["edges"][1]["citation"], source.as_uri())
                    graphml = folder / "graph.graphml"
                    with patch.object(QFileDialog, "getSaveFileName", return_value=(str(graphml), "GraphML")):
                        window.export_graph()
                    self.assertIn(source.as_uri(), graphml.read_text())
                    backup = folder / "backup.sqlite3"
                    with patch.object(QFileDialog, "getSaveFileName", return_value=(str(backup), "")):
                        window.backup()
                    window.work[0].result(timeout=5)
                    window.poll()
                    snapshot = Store(backup)
                    try:
                        self.assertEqual(len(snapshot.evidence(window.project)), 1)
                    finally:
                        snapshot.db.close()
                    reopened = Window()
                    try:
                        self.assertEqual(reopened.notes.toPlainText(), "Заметка")
                        self.assertEqual(reopened.items.count(), 1)
                        self.assertEqual(len(reopened.store.relationships(reopened.project)), 1)
                    finally:
                        reopened.close()
                        reopened.store.db.close()
                finally:
                    window.close()
                    window.pool.shutdown()
                    window.store.db.close()


    def test_hybrid_preserves_local_analysis_and_requires_cloud_consent(self):
        response = json.dumps({"claims": [{
            "text": "Сведения опубликованы", "kind": "observation",
            "citations": [{"id": 1, "quote": "Отчёт опубликован"}],
        }]})
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_DATA_HOME": directory, "OPENAI_API_KEY": "test-secret"}):
                window = Window()
                try:
                    project = window.store.project("Проект")
                    window.store.add(project, "Отчёт", "file:///report", "Отчёт опубликован")
                    window.load_projects(project)
                    window.engine.setCurrentText("Гибрид")
                    window.question.setPlainText("Когда опубликован отчёт?")
                    for decision in (QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes):
                        with patch("app.analyze", return_value=response) as provider:
                            with patch.object(window, "select_model", side_effect=["local", "cloud"]):
                                with patch.object(QMessageBox, "exec", return_value=decision):
                                    window.ask()
                                    window.work[0].result(timeout=5)
                                    window.poll()
                                    if decision == QMessageBox.StandardButton.No:
                                        self.assertIsNone(window.work)
                                        self.assertEqual(window.store.analyses(project)[0]["engine"], "Ollama")
                                        self.assertEqual(provider.call_count, 1)
                                        continue
                                    window.work[0].result(timeout=5)
                                    window.poll()
                            self.assertEqual(provider.call_count, 2)
                            cloud_call = provider.call_args.args
                            self.assertEqual(cloud_call[0], "OpenAI")
                            self.assertIn('"preliminary_analysis"', cloud_call[2])
                            self.assertIn('file:///report', cloud_call[2])
                    latest = window.store.analyses(project)[0]
                    self.assertEqual(latest["engine"], "Гибрид")
                    claims = json.loads(latest["claims"])
                    self.assertEqual([claim["provider"] for claim in claims], ["Ollama", "OpenAI"])
                    events = window.store.api_events(project)
                    self.assertEqual([event["provider"] for event in events], ["OpenAI", "Ollama", "Ollama"])
                    self.assertNotIn("test-secret", window.api_log.toPlainText())
                finally:
                    window.close()
                    window.pool.shutdown()
                    window.store.db.close()


    def test_search_hub_records_cached_source_with_original_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_DATA_HOME": directory}):
                window = Window()
                try:
                    project = window.store.project("Проект")
                    window.load_projects(project)
                    response = SourceResult('{"Status": 0}', 1700000000, True)
                    with patch.object(window.source_client, "collect", return_value=response):
                        with patch.object(QInputDialog, "getText", return_value=("iana.org", True)):
                            window.collect()
                        window.work[0].result(timeout=5)
                        window.poll()
                    evidence = window.store.evidence(project)[0]
                    self.assertEqual(evidence["collected"], 1700000000)
                    self.assertIn("dns.google", evidence["source"])
                    self.assertEqual(window.store.api_events(project)[0]["status"], "Кэш")
                finally:
                    window.close()
                    window.pool.shutdown()
                    window.store.db.close()

    def test_dashboard_import_opens_a_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_DATA_HOME": directory}):
                window = Window()
                try:
                    original = window.store.project("Исследование")
                    window.load_projects(original)
                    window.notes.setPlainText("Заметка для переноса")
                    package = str(Path(directory) / "investigation.nexus.json")
                    with patch.object(QFileDialog, "getSaveFileName", return_value=(package, "")):
                        window.export_investigation()
                    window.work[0].result(timeout=5)
                    window.poll()
                    with patch.object(QFileDialog, "getOpenFileName", return_value=(package, "")):
                        window.import_investigation()
                    window.work[0].result(timeout=5)
                    window.poll()
                    self.assertNotEqual(window.project, original)
                    self.assertEqual(window.projects.count(), 2)
                    self.assertEqual(window.notes.toPlainText(), "Заметка для переноса")
                finally:
                    window.close()
                    window.pool.shutdown()
                    window.store.db.close()
