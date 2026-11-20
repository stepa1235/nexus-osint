"""NEXUS OSINT desktop entry point."""
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import QDate, QSettings, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton,
    QSpinBox, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from analyst import build_prompt, render_claims, suggest_cloud, verify_claims
from documents import extract
from entities import CONFIDENCE, TYPES
from graph_export import serialize_graph
from project_archive import export_project, import_project
from core import SourceClient, SourceResult, Store, analyze, ollama_models
from sources import discover
from timeline import date_variants, event_label


class Window(QWidget):
    def __init__(self):
        super().__init__()
        data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        self.database = data / "nexus-osint/projects.sqlite3"
        self.store = Store(self.database)
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.work = None
        self.setWindowTitle("NEXUS OSINT")
        self.resize(1100, 720)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("NEXUS OSINT"))
        row = QHBoxLayout()
        layout.addLayout(row)
        self.projects = QComboBox()
        row.addWidget(self.projects, 1)
        self.button(row, "Новый проект", self.new_project)
        self.button(row, "Экспорт Markdown", self.export)
        self.button(row, "Резервная копия", self.backup)
        self.button(row, "Удалить проект", self.delete_project)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.status = QLabel()
        layout.addWidget(self.status)

        dashboard = self.panel("Dashboard")
        self.summary = QLabel()
        dashboard.addWidget(self.summary)
        project_tools = QHBoxLayout()
        dashboard.addLayout(project_tools)
        self.button(project_tools, "Экспорт проекта", self.export_investigation)
        self.button(project_tools, "Импорт проекта", self.import_investigation)
        project_tools.addStretch()
        self.notes = QTextEdit(placeholderText="Заметки проекта")
        dashboard.addWidget(self.notes)
        self.notes_timer = QTimer(self)
        self.notes_timer.setSingleShot(True)
        self.notes_timer.setInterval(1200)
        self.notes_project = None
        self.notes.textChanged.connect(self.save_notes)
        self.button(project_tools, "История заметок", self.show_note_history)

        search = self.panel("Search Hub")
        self.sources = QComboBox()
        self.registry, plugin_errors = discover()
        self.source_client = SourceClient()
        for source in self.registry.values():
            self.sources.addItem(source.title, source.id)
        self.source_description = QLabel()
        self.source_description.setWordWrap(True)
        self.sources.currentIndexChanged.connect(self.describe_source)
        self.describe_source()
        search.addWidget(self.sources)
        search.addWidget(self.source_description)
        if plugin_errors:
            search.addWidget(QLabel("Ошибки подключения модулей: " + ", ".join(plugin_errors)))
        self.button(search, "Запросить публичный источник", self.collect)
        self.button(search, "Импортировать документ", self.import_text)
        search.addStretch()

        evidence = self.panel("Evidence Manager")
        self.evidence_page = evidence.parentWidget()
        self.evidence_query = QLineEdit(placeholderText="Поиск по сохранённым материалам")
        self.evidence_query.returnPressed.connect(self.search_evidence)
        evidence.addWidget(self.evidence_query)
        self.button(evidence, "Найти фрагменты", self.search_evidence)
        self.items = QListWidget()
        self.material = QTextEdit(readOnly=True)
        evidence.addWidget(self.items)
        evidence.addWidget(self.material)
        self.items.currentRowChanged.connect(self.open_evidence)

        timeline = self.panel("Хронология")
        timeline_tools = QHBoxLayout()
        timeline.addLayout(timeline_tools)
        self.button(timeline_tools, "Добавить событие", lambda: self.edit_event())
        self.button(timeline_tools, "Изменить", self.edit_selected_event)
        self.button(timeline_tools, "Удалить", self.remove_event)
        self.button(timeline_tools, "Открыть источник", self.open_event_source)
        self.timeline_items = QListWidget()
        timeline.addWidget(self.timeline_items)
        self.timeline_details = QTextEdit(readOnly=True)
        timeline.addWidget(self.timeline_details)
        self.timeline_items.currentRowChanged.connect(self.show_event)

        graph = self.panel("Graph Explorer")
        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        tools = QHBoxLayout()
        graph.addLayout(tools)
        self.graph_query = QLineEdit(placeholderText="Фильтр по названию")
        self.graph_kind = QComboBox()
        self.graph_kind.addItems(["Все типы", *TYPES])
        self.graph_query.textChanged.connect(self.draw_graph)
        self.graph_kind.currentIndexChanged.connect(self.draw_graph)
        tools.addWidget(self.graph_query, 1)
        tools.addWidget(self.graph_kind)
        self.button(tools, "Добавить сущность", self.new_entity)
        self.button(tools, "Добавить связь", self.new_relationship)
        graph.addWidget(self.view)
        self.button(graph, "Экспорт JSON / GraphML", self.export_graph)
        self.button(graph, "Увеличить", lambda: self.view.scale(1.2, 1.2))
        self.button(graph, "Уменьшить", lambda: self.view.scale(1 / 1.2, 1 / 1.2))

        ai = self.panel("AI Analyst")
        self.engine = QComboBox()
        self.engine.addItems(["Авто", "Ollama", "OpenAI", "Гибрид"])
        ai.addWidget(self.engine)
        self.question = QTextEdit(placeholderText="Вопрос по материалам проекта")
        self.answer = QTextEdit(readOnly=True)
        ai.addWidget(self.question)
        self.button(ai, "Выполнить анализ", self.ask)
        ai.addWidget(self.answer)

        audit = self.panel("Журнал API")
        self.api_log = QTextEdit(readOnly=True)
        audit.addWidget(self.api_log)
        settings = self.panel("Settings")
        self.preferences = QSettings(str(self.database.parent / "settings.ini"), QSettings.Format.IniFormat)
        form = QFormLayout()
        settings.addLayout(form)
        self.local_model = QComboBox()
        self.local_model.setEditable(True)
        self.local_model.setEditText(self.preferences.value("ollama/model", "", type=str))
        form.addRow("Модель Ollama", self.local_model)
        self.button(settings, "Получить модели Ollama", self.fetch_models)
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0, 2)
        self.temperature.setSingleStep(0.1)
        self.temperature.setValue(self.preferences.value("ollama/temperature", 0.2, type=float))
        form.addRow("Температура", self.temperature)
        self.context = QSpinBox()
        self.context.setRange(1024, 131072)
        self.context.setSingleStep(1024)
        self.context.setValue(self.preferences.value("ollama/context", 4096, type=int))
        form.addRow("Окно контекста", self.context)
        self.system_prompt = QTextEdit()
        self.system_prompt.setPlainText(self.preferences.value("ollama/system", "", type=str))
        form.addRow("Системный промпт Ollama", self.system_prompt)
        self.button(settings, "Сохранить настройки", self.save_settings)
        settings.addStretch()
        self.projects.currentIndexChanged.connect(self.refresh)
        self.load_projects()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)

    def panel(self, name):
        page = QWidget()
        self.tabs.addTab(page, name)
        return QVBoxLayout(page)

    def button(self, layout, title, action):
        button = QPushButton(title)
        button.clicked.connect(action)
        layout.addWidget(button)

    @property
    def project(self):
        return self.projects.currentData()

    def load_projects(self, selected=None):
        self.projects.blockSignals(True)
        self.projects.clear()
        for row in self.store.db.execute("SELECT id,name FROM projects ORDER BY id"):
            self.projects.addItem(row["name"], row["id"])
        if selected is not None:
            self.projects.setCurrentIndex(self.projects.findData(selected))
        self.projects.blockSignals(False)
        self.refresh()

    def new_project(self):
        name, accepted = QInputDialog.getText(self, "Новый проект", "Название")
        if accepted and name.strip():
            self.load_projects(self.store.project(name))

    def delete_project(self):
        project = self.project
        if project is None:
            return
        if self.work:
            self.status.setText("Дождитесь завершения текущего запроса")
            return
        answer = QMessageBox.question(
            self, "Удаление проекта",
            f'Удалить «{self.projects.currentText()}», его материалы, анализ и граф из рабочей базы?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.store.delete_project(project)
            self.load_projects()
            self.status.setText("Проект удалён из рабочей базы")

    def refresh(self):
        if self.notes_project != self.project:
            self.notes_timer.stop()
        self.rows = self.store.evidence(self.project)
        self.evidence_query.clear()
        self.items.clear()
        self.material.clear()
        analyses = self.store.analyses(self.project)
        self.answer.setPlainText(render_claims(json.loads(analyses[0]["claims"])) if analyses else "")
        row = self.store.db.execute("SELECT notes FROM projects WHERE id=?", (self.project,)).fetchone()
        self.notes.blockSignals(True)
        self.notes.setPlainText(row["notes"] if row else "")
        self.notes.setEnabled(row is not None)
        self.notes.blockSignals(False)
        self.summary.setText(f"Проектов: {self.projects.count()}    Материалов: {len(self.rows)}")
        for item in self.rows:
            self.items.addItem(item["title"])
        self.draw_graph()
        self.refresh_timeline()
        self.refresh_api_log()

    def refresh_timeline(self):
        self.events = self.store.timeline(self.project)
        variants = date_variants(self.events)
        self.timeline_items.clear()
        for event in self.events:
            self.timeline_items.addItem(event_label(event, variants))
        if self.events:
            self.timeline_items.setCurrentRow(0)
        else:
            self.timeline_details.clear()

    def selected_event(self):
        index = self.timeline_items.currentRow()
        if 0 <= index < len(self.events):
            return self.events[index]
        return None

    def show_event(self):
        event = self.selected_event()
        if event is None:
            self.timeline_details.clear()
            return
        source = f'[{event["evidence"]}] {event["source"]}' if event["evidence"] is not None else "Гипотеза"
        self.timeline_details.setPlainText(f'{event["notes"]}\n\n{source}'.strip())

    def open_event_source(self):
        event = self.selected_event()
        if event is not None and event["evidence"] is not None:
            for index, row in enumerate(self.rows):
                if row["id"] == event["evidence"]:
                    self.tabs.setCurrentWidget(self.evidence_page)
                    self.items.setCurrentRow(index)
                    self.open_evidence(index)
                    return
        self.status.setText("Выберите событие с источником")

    def edit_selected_event(self):
        event = self.selected_event()
        if event is None:
            self.status.setText("Выберите событие")
            return
        self.edit_event(event)

    def edit_event(self, event=None):
        project = self.project
        if project is None:
            self.status.setText("Создайте проект")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Изменить событие" if event else "Добавить событие")
        layout = QFormLayout(dialog)
        day = QDateEdit(calendarPopup=True, displayFormat="yyyy-MM-dd")
        day.setMinimumDate(QDate(1, 1, 1))
        day.setMaximumDate(QDate(9999, 12, 31))
        day.setDate(QDate.fromString(event["date"], Qt.DateFormat.ISODate) if event else QDate.currentDate())
        title = QLineEdit(event["title"] if event else "")
        title.setObjectName("eventTitle")
        title.setMaxLength(300)
        source = QComboBox()
        source.addItem("Без источника · гипотеза", None)
        for row in self.rows:
            source.addItem(f'[{row["id"]}] {row["title"]}', row["id"])
        if event and event["evidence"] is not None:
            source.setCurrentIndex(source.findData(event["evidence"]))
        notes = QTextEdit()
        notes.setPlainText(event["notes"] if event else "")
        layout.addRow("Дата", day)
        layout.addRow("Название", title)
        layout.addRow("Материал", source)
        layout.addRow("Описание", notes)
        error_label = QLabel()
        error_label.setWordWrap(True)
        layout.addRow(error_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addRow(buttons)

        def save():
            try:
                self.store.save_event(
                    project, day.date().toString(Qt.DateFormat.ISODate), title.text(),
                    notes.toPlainText(), source.currentData(), event=event["id"] if event else None,
                )
            except (ValueError, sqlite3.Error) as error:
                error_label.setText(str(error))
                return
            dialog.accept()

        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh_timeline()
            self.status.setText("Событие сохранено")

    def remove_event(self):
        event = self.selected_event()
        if event is None:
            self.status.setText("Выберите событие")
            return
        if QMessageBox.question(
            self, "Удалить событие", "Удалить выбранное событие из хронологии?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            self.store.delete_event(self.project, event["id"])
            self.refresh_timeline()
            self.status.setText("Событие удалено")

    def draw_graph(self):
        self.scene.clear()
        if self.project is None:
            return
        query = self.graph_query.text().strip().casefold()
        kind = self.graph_kind.currentText()
        root = self.graph_label(self.projects.currentText(), 0, 0)
        evidence = self.rows if kind == "Все типы" else []
        for index, item in enumerate(row for row in evidence if query in row["title"].casefold()):
            y = index * 34
            node = self.graph_label(f'[{item["id"]}] {item["title"]}', 248, y, item["source"])
            self.graph_line(root, node, "#417981")
        entities = [
            item for item in self.store.entities(self.project)
            if (kind == "Все типы" or item["kind"] == kind)
            and query in item["label"].casefold()
        ]
        nodes = {}
        columns = {}
        for item in entities:
            column, row = columns.setdefault(item["kind"], [len(columns), 0])
            x = column * 290
            y = 80 + len(evidence) * 34 + row * 64
            if row == 0:
                self.graph_label(item["kind"], x, y - 30)
            nodes[item["id"]] = self.graph_label(item["label"], x, y, item["label"])
            columns[item["kind"]][1] += 1
        for item in self.store.relationships(self.project):
            if item["origin"] in nodes and item["destination"] in nodes:
                tooltip = f'{item["relation"]} · {item["confidence"]}'
                if item["source"]:
                    tooltip += f'\n[{item["evidence"]}] {item["title"]}\n{item["source"]}'
                self.graph_line(nodes[item["origin"]], nodes[item["destination"]], "#9783df", tooltip)

    def graph_label(self, text, x, y, tooltip=""):
        summary = text if len(text) <= 32 else text[:31] + "…"
        item = self.scene.addSimpleText(summary)
        item.setBrush(QBrush(Qt.GlobalColor.white))
        item.setPos(x, y)
        item.setToolTip(tooltip or text)
        return item

    def graph_line(self, origin, destination, color, tooltip=""):
        left = origin.sceneBoundingRect().center()
        right = destination.sceneBoundingRect().center()
        line = self.scene.addLine(left.x(), left.y(), right.x(), right.y(), QPen(QColor(color), 2))
        line.setZValue(-1)
        line.setToolTip(tooltip)

    def new_entity(self):
        if self.project is None:
            self.status.setText("Создайте проект")
            return
        kind, accepted = QInputDialog.getItem(self, "Сущность", "Тип", list(TYPES), 0, False)
        if not accepted:
            return
        label, accepted = QInputDialog.getText(self, "Сущность", "Название")
        if not accepted:
            return
        try:
            self.store.add_entity(self.project, kind, label)
        except ValueError as error:
            self.status.setText(str(error))
            return
        self.draw_graph()
        self.status.setText("Сущность сохранена")

    def new_relationship(self):
        entities = self.store.entities(self.project)
        if len(entities) < 2:
            self.status.setText("Добавьте минимум две сущности в проект")
            return
        choices = {f'[{item["id"]}] {item["kind"]} · {item["label"]}': item["id"] for item in entities}
        origin, accepted = QInputDialog.getItem(self, "Связь", "Откуда", list(choices), 0, False)
        if not accepted:
            return
        destinations = [name for name in choices if name != origin]
        destination, accepted = QInputDialog.getItem(self, "Связь", "Куда", destinations, 0, False)
        if not accepted:
            return
        relation, accepted = QInputDialog.getText(self, "Связь", "Описание")
        if not accepted:
            return
        confidence, accepted = QInputDialog.getItem(
            self, "Связь", "Достоверность", list(CONFIDENCE), 0, False
        )
        if not accepted:
            return
        evidence = {"Без источника": None}
        evidence.update({f'[{item["id"]}] {item["title"]}': item["id"] for item in self.rows})
        source, accepted = QInputDialog.getItem(self, "Связь", "Материал", list(evidence), 0, False)
        if not accepted:
            return
        try:
            self.store.add_relationship(
                self.project, choices[origin], choices[destination], relation, confidence, evidence[source]
            )
        except ValueError as error:
            self.status.setText(str(error))
            return
        self.draw_graph()
        self.status.setText("Связь сохранена")

    def save_notes(self):
        project = self.project
        if project is None:
            return
        checkpoint = project != self.notes_project or not self.notes_timer.isActive()
        try:
            saved = self.store.save_notes(project, self.notes.toPlainText(), checkpoint=checkpoint)
        except sqlite3.Error as error:
            self.status.setText(f"Ошибка сохранения заметок: {error}")
            return
        if saved:
            self.notes_project = project
            self.notes_timer.start()

    def show_note_history(self):
        if self.project is None:
            self.status.setText("Выберите проект")
            return
        project = self.project
        versions = self.store.note_versions(project, limit=100)
        if not versions:
            self.status.setText("У заметок пока нет сохранённых версий")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("История заметок")
        dialog.resize(720, 480)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Последние 100 предыдущих версий"))
        choices = QComboBox()
        layout.addWidget(choices)
        for version in versions:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(version["created"]))
            choices.addItem(f'{stamp} · версия {version["id"]}', version["id"])
        preview = QTextEdit(readOnly=True)
        layout.addWidget(preview, 1)
        choices.currentIndexChanged.connect(lambda index: preview.setPlainText(versions[index]["body"]))
        preview.setPlainText(versions[0]["body"])
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        restore = buttons.addButton("Восстановить", QDialogButtonBox.ButtonRole.AcceptRole)
        restore.clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                body = self.store.restore_note(project, choices.currentData())
            except (ValueError, sqlite3.Error) as error:
                self.status.setText(f"Ошибка восстановления заметки: {error}")
                return
            self.notes.blockSignals(True)
            self.notes.setPlainText(body)
            self.notes.blockSignals(False)
            self.notes_timer.stop()
            self.status.setText("Версия заметки восстановлена")

    def search_evidence(self):
        query = self.evidence_query.text().strip()
        if self.project is None or not query:
            self.status.setText("Выберите проект и введите поисковый запрос")
            return
        self.items.setCurrentRow(-1)
        matches = self.store.search(self.project, query)
        self.material.setPlainText("\n\n".join(
            f'[{item["id"]}] {item["title"]}\n{item["source"]}\n\n{item["body"]}'
            for item in matches
        ) if matches else "Совпадений нет")

    def open_evidence(self, index):
        if 0 <= index < len(self.rows):
            row = self.rows[index]
            self.material.setPlainText(row["source"] + "\n\n" + row["body"])

    def refresh_api_log(self):
        entries = []
        for event in self.store.api_events(self.project):
            started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event["started"]))
            duration = f' · {event["elapsed_ms"]} мс' if event["elapsed_ms"] is not None else ""
            entries.append(f'{started} · {event["provider"]} · {event["status"]}{duration}')
        self.api_log.setPlainText("\n".join(entries))

    def submit(self, func, done, *, api=None):
        if self.work:
            self.status.setText("Дождитесь завершения текущего запроса")
            return
        event = self.store.begin_api_event(*api) if api else None
        self.work = (self.pool.submit(func), done, event, time.monotonic())
        self.status.setText("Выполняется запрос…")
        if event is not None:
            self.refresh_api_log()

    def poll(self):
        if not self.work or not self.work[0].done():
            return
        future, done, event, started = self.work
        self.work = None
        try:
            if event is not None:
                if future.exception() is not None:
                    outcome = "Ошибка API"
                else:
                    result = future.result()
                    outcome = "Кэш" if isinstance(result, SourceResult) and result.cached else "Ответ получен"
                self.store.finish_api_event(event, outcome, round((time.monotonic() - started) * 1000))
                self.refresh_api_log()
            result = future.result()
            self.status.setText("Готово")
            done(result)
        except Exception as error:
            self.status.setText(f"Ошибка: {error}")

    def describe_source(self):
        source = self.registry[self.sources.currentData()]
        description = f"{source.description} · {source.host}"
        if source.key_env:
            description += f" · Ключ из {source.key_env}"
        self.source_description.setText(description)

    def collect(self):
        project = self.project
        if project is None:
            self.status.setText("Создайте проект")
            return
        if self.work:
            self.status.setText("Дождитесь завершения текущего запроса")
            return
        source = self.registry[self.sources.currentData()]
        value, accepted = QInputDialog.getText(self, source.title, source.input_label)
        if not accepted:
            return
        try:
            url = source.endpoint(value)
        except ValueError as error:
            self.status.setText(str(error))
            return
        key = os.getenv(source.key_env, "") if source.key_env else ""
        if source.key_env and not key:
            self.status.setText(f"Задайте переменную {source.key_env}")
            return

        def display(result):
            body, collected, cached = result
            self.save_material(project, f"{source.title} — {value}", url, body, collected=collected)
            if cached:
                self.status.setText("Получено из кэша")

        self.submit(
            lambda: self.source_client.collect(source, url, key), display,
            api=(project, source.id),
        )

    def save_material(self, project, title, source, content, *, collected=None):
        self.store.add(project, title, source, content, collected=collected)
        if project == self.project:
            self.refresh()

    def import_text(self):
        project = self.project
        if project is None:
            self.status.setText("Создайте проект")
            return
        filename, _ = QFileDialog.getOpenFileName(
            self, "Импорт документа", filter="Документы (*.pdf *.docx *.txt *.csv *.json *.html *.md)"
        )
        if not filename:
            return
        path = Path(filename)

        self.submit(lambda: extract(path), lambda body: self.save_material(
            project, path.name, path.as_uri(), body
        ))

    def ask(self):
        if self.work:
            self.status.setText("Дождитесь завершения текущего запроса")
            return
        project = self.project
        question = self.question.toPlainText().strip()
        if project is None or not question:
            self.status.setText("Выберите проект и введите вопрос")
            return
        engine = self.engine.currentText()
        excerpts = [dict(row) for row in self.store.search(project, question)]
        if engine == "Авто":
            engine = "Ollama"
            if suggest_cloud(question, excerpts):
                choice, accepted = QInputDialog.getItem(
                    self, "Выбор модели", "Для сравнительного анализа можно использовать GPT",
                    ["Ollama", "OpenAI"], 0, False,
                )
                if not accepted:
                    return
                engine = choice
        hybrid = engine == "Гибрид"
        if hybrid:
            engine = "Ollama"
        model = self.select_model(engine)
        if not model:
            return
        prompt = build_prompt(question, excerpts)
        key = self.cloud_key(model, prompt) if engine == "OpenAI" else ""
        if key is None:
            return

        def display(text):
            claims = verify_claims(text, excerpts)
            for claim in claims:
                claim["provider"] = engine
            analysis = self.store.save_analysis(project, engine, model, question, claims)
            self.display_analysis(project, claims)
            if hybrid and project == self.project:
                self.hybrid_review(project, analysis, model, question, excerpts, claims)

        local_options = self.local_options() if engine == "Ollama" else None
        self.submit(
            lambda: analyze(engine, model, prompt, key, local_options=local_options),
            display, api=(project, engine),
        )

    def select_model(self, engine):
        configured = self.local_model.currentText().strip() if engine == "Ollama" else ""
        if configured:
            return configured
        model, accepted = QInputDialog.getText(self, engine, "Идентификатор модели")
        return model.strip() if accepted else ""

    def local_options(self):
        return {
            "temperature": self.temperature.value(),
            "num_ctx": self.context.value(),
            "system": self.system_prompt.toPlainText(),
        }

    def save_settings(self):
        self.preferences.setValue("ollama/model", self.local_model.currentText().strip())
        self.preferences.setValue("ollama/temperature", self.temperature.value())
        self.preferences.setValue("ollama/context", self.context.value())
        self.preferences.setValue("ollama/system", self.system_prompt.toPlainText())
        self.preferences.sync()
        if self.preferences.status() == QSettings.Status.NoError:
            self.status.setText("Настройки сохранены")
        else:
            self.status.setText("Ошибка сохранения настроек")

    def fetch_models(self):
        project = self.project

        def display(models):
            current = self.local_model.currentText().strip()
            self.local_model.clear()
            self.local_model.addItems(models)
            if current:
                self.local_model.setCurrentText(current)
            if not models:
                self.status.setText("Ollama не вернул установленных моделей")

        audit = (project, "Ollama · список моделей") if project is not None else None
        self.submit(ollama_models, display, api=audit)

    def cloud_key(self, model, prompt):
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            self.status.setText("OpenAI недоступен. Задайте OPENAI_API_KEY")
            return None
        preview = QMessageBox(self)
        preview.setWindowTitle("Отправка в OpenAI")
        preview.setTextFormat(Qt.TextFormat.PlainText)
        preview.setText(f"Отправить запрос в OpenAI API? Модель {model}. Лимит ответа 1500 токенов.")
        preview.setDetailedText(prompt)
        preview.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        preview.setDefaultButton(QMessageBox.StandardButton.No)
        if preview.exec() != QMessageBox.StandardButton.Yes:
            self.status.setText("Облачный запрос отменён")
            return None
        return key

    def display_analysis(self, project, claims):
        if project == self.project:
            self.answer.setPlainText(render_claims(claims))

    def hybrid_review(self, project, analysis, local_model, question, excerpts, local_claims):
        if not os.getenv("OPENAI_API_KEY"):
            self.status.setText("Локальный анализ сохранён. Для GPT нужен OPENAI_API_KEY")
            return
        model = self.select_model("OpenAI")
        if not model:
            return
        prompt = build_prompt(question, excerpts, local_claims)
        key = self.cloud_key(model, prompt)
        if key is None:
            return

        def display(text):
            reviewed = verify_claims(text, excerpts)
            for claim in reviewed:
                claim["provider"] = "OpenAI"
            combined = local_claims + reviewed
            self.store.update_analysis(project, analysis, "Гибрид", f"{local_model} / {model}", combined)
            self.display_analysis(project, combined)

        self.submit(lambda: analyze("OpenAI", model, prompt, key), display, api=(project, "OpenAI"))

    def export_investigation(self):
        project = self.project
        if project is None:
            self.status.setText("Выберите проект")
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "Экспорт проекта", "investigation.nexus.json", "Проект NEXUS (*.nexus.json)"
        )
        if not filename:
            return
        self.submit(
            lambda: export_project(self.database, project, Path(filename)),
            lambda _: self.status.setText("Файл проекта сохранён"),
        )

    def import_investigation(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Импорт проекта", filter="Проект NEXUS (*.nexus.json);;JSON (*.json)"
        )
        if not filename:
            return

        def display(project):
            self.load_projects(project)
            self.status.setText("Импортированный проект открыт")

        self.submit(lambda: import_project(self.database, Path(filename)), display)

    def export_graph(self):
        if self.project is None:
            self.status.setText("Создайте проект")
            return
        filename, selected = QFileDialog.getSaveFileName(
            self, "Экспорт графа", "nexus.json", "JSON (*.json);;GraphML (*.graphml)"
        )
        if not filename:
            return
        path = Path(filename)
        graphml = path.suffix.lower() == ".graphml" or selected.startswith("GraphML")
        if not path.suffix:
            path = path.with_suffix(".graphml" if graphml else ".json")
        try:
            content = serialize_graph(
                self.projects.currentText(), self.rows, graphml,
                self.store.entities(self.project), self.store.relationships(self.project),
            )
            path.write_text(content, encoding="utf-8")
            self.status.setText("Граф сохранён")
        except OSError as error:
            self.status.setText(f"Ошибка экспорта графа: {error}")

    def backup(self):
        filename, _ = QFileDialog.getSaveFileName(
            self, "Резервная копия базы", "nexus-backup.sqlite3", "SQLite (*.sqlite3)"
        )
        if not filename:
            return
        destination = Path(filename)

        def copy_database():
            descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            try:
                with closing(sqlite3.connect(self.database)) as source, closing(sqlite3.connect(destination)) as target:
                    source.backup(target)
            except Exception:
                destination.unlink()
                raise

        self.submit(copy_database, lambda _: None)

    def export(self):
        if self.project is None:
            self.status.setText("Создайте проект")
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Экспорт отчёта", "nexus.md", "Markdown (*.md)")
        if not filename:
            return
        sections = [f"# {self.projects.currentText()}", self.notes.toPlainText()]
        events = self.store.timeline(self.project)
        if events:
            variants = date_variants(events)
            entries = []
            for event in events:
                lines = [event_label(event, variants), event["notes"]]
                if event["source"]:
                    lines.append(f'[{event["evidence"]}] {event["source"]}')
                entries.append("\n".join(lines))
            details = "\n\n".join(entries)
            sections.append("## Хронология\n\n" + "\n".join("    " + line for line in details.splitlines()))
        for row in self.rows:
            sections.append(f'## [{row["id"]}] {row["title"]}\n\nИсточник: {row["source"]}\n\n'
                            + "\n".join("    " + line for line in row["body"].splitlines()))
        for analysis in self.store.analyses(self.project):
            sections.append(
                f'## Анализ · {analysis["engine"]} / {analysis["model"]}\n\n'
                + "\n".join("    " + line for line in (
                    analysis["question"] + "\n\n" + render_claims(json.loads(analysis["claims"]))
                ).splitlines())
            )
        try:
            Path(filename).write_text("\n\n".join(sections) + "\n", encoding="utf-8")
            self.status.setText("Отчёт сохранён")
        except OSError as error:
            self.status.setText(f"Ошибка экспорта: {error}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QWidget { background: #191c25; color: #e5e9f7; font-size: 14px; }
        QPushButton, QComboBox, QTabBar::tab { background: #272d40; padding: 8px; }
        QPushButton:hover, QTabBar::tab:selected { background: #3b4770; }
        QTextEdit, QListWidget, QGraphicsView { background: #11141c; padding: 5px; }
    """)
    window = Window()
    window.show()
    sys.exit(app.exec())
