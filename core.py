"""Local project storage and read-only public sources."""
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from entities import CONFIDENCE, TYPES


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPSRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urlsplit(newurl)
        if parts.scheme != "https" or parts.username is not None:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request_json(url, body=None, headers=None, *, local=False, timeout=20, follow_redirects=True):
    headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, json.dumps(body).encode() if body is not None else None, headers)
    if local:
        open_request = build_opener(NoRedirect(), ProxyHandler({})).open
    elif body is not None or not follow_redirects:
        open_request = build_opener(NoRedirect()).open
    else:
        open_request = build_opener(HTTPSRedirect()).open
    with open_request(request, timeout=timeout) as response:
        data = response.read(8_000_001)
        if len(data) > 8_000_000:
            raise ValueError("Ответ превышает 8 МБ")
        return json.loads(data)


class SourceResult(NamedTuple):
    body: str
    collected: int
    cached: bool


class SourceClient:
    """Single-worker runner with public response cache and bounded retries."""

    def __init__(self):
        self.cache = {}
        self.last_request = {}

    def collect(self, source, url, key):
        cache_key = (source.id, url)
        cached = self.cache.get(cache_key) if not key else None
        if cached and time.monotonic() - cached[0] < 120:
            return SourceResult(cached[1], cached[2], True)
        headers = {source.key_header: source.key_prefix + key} if key else None
        for attempt in range(3):
            wait = self.last_request.get(source.id, 0) + source.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last_request[source.id] = time.monotonic()
            try:
                data = request_json(
                    url, headers=headers, timeout=12, follow_redirects=source.follow_redirects,
                )
            except HTTPError as error:
                if error.code not in {502, 503, 504} or attempt == 2:
                    raise
            except (URLError, TimeoutError):
                if attempt == 2:
                    raise
            else:
                break
            time.sleep(0.5 * 2 ** attempt)
        source.check(data)
        content = json.dumps(data, ensure_ascii=False, indent=2)
        collected = int(time.time())
        if not key and len(content) < 128_000:
            self.cache.pop(cache_key, None)
            self.cache[cache_key] = (time.monotonic(), content, collected)
            if len(self.cache) > 32:
                del self.cache[next(iter(self.cache))]
        return SourceResult(content, collected, False)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS projects(
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, notes TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS note_versions(
                id INTEGER PRIMARY KEY, project INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                body TEXT NOT NULL, created INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS note_versions_project ON note_versions(project, id);
            CREATE TABLE IF NOT EXISTS evidence(
                id INTEGER PRIMARY KEY, project INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                title TEXT NOT NULL, source TEXT NOT NULL, body TEXT NOT NULL, collected INTEGER NOT NULL,
                UNIQUE(project, source, body)
            );
            CREATE TABLE IF NOT EXISTS timeline_events(
                id INTEGER PRIMARY KEY, project INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                date TEXT NOT NULL, title TEXT NOT NULL, notes TEXT NOT NULL,
                evidence INTEGER REFERENCES evidence(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS timeline_project_date ON timeline_events(project,date,id);
            CREATE TABLE IF NOT EXISTS analyses(
                id INTEGER PRIMARY KEY, project INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                engine TEXT NOT NULL, model TEXT NOT NULL, question TEXT NOT NULL, claims TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS api_events(
                id INTEGER PRIMARY KEY, project INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                provider TEXT NOT NULL, started INTEGER NOT NULL,
                elapsed_ms INTEGER, status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entities(
                id INTEGER PRIMARY KEY, project INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, label TEXT NOT NULL, UNIQUE(project, id)
            );
            CREATE TABLE IF NOT EXISTS relationships(
                id INTEGER PRIMARY KEY, project INTEGER NOT NULL,
                origin INTEGER NOT NULL, destination INTEGER NOT NULL,
                relation TEXT NOT NULL, confidence TEXT NOT NULL,
                evidence INTEGER REFERENCES evidence(id) ON DELETE CASCADE,
                FOREIGN KEY(project, origin) REFERENCES entities(project, id) ON DELETE CASCADE,
                FOREIGN KEY(project, destination) REFERENCES entities(project, id) ON DELETE CASCADE
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS fragments USING fts5(evidence UNINDEXED, body);
        """)

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:
            yield
            return
        with self.db:
            self.db.execute("BEGIN")
            yield

    def project(self, name):
        if not name.strip():
            raise ValueError("Введите название проекта")
        with self.transaction():
            return self.db.execute("INSERT INTO projects(name) VALUES (?)", (name.strip(),)).lastrowid

    def note_versions(self, project, limit=None):
        return self.db.execute("""
            SELECT id,body,created FROM note_versions WHERE project=? ORDER BY id DESC LIMIT ?
        """, (project, -1 if limit is None else limit)).fetchall()

    def add_note_version(self, project, body, created):
        with self.transaction():
            self.db.execute(
                "INSERT INTO note_versions(project,body,created) VALUES(?,?,?)",
                (project, body, created),
            )

    def save_notes(self, project, body, *, checkpoint=False):
        with self.transaction():
            row = self.db.execute("SELECT notes FROM projects WHERE id=?", (project,)).fetchone()
            if row is None or row["notes"] == body:
                return False
            if checkpoint:
                self.add_note_version(project, row["notes"], int(time.time()))
            self.db.execute("UPDATE projects SET notes=? WHERE id=?", (body, project))
            return True

    def restore_note(self, project, version):
        with self.transaction():
            row = self.db.execute(
                "SELECT body FROM note_versions WHERE id=? AND project=?", (version, project)
            ).fetchone()
            if row is None:
                raise ValueError("Версия заметки не найдена в текущем проекте")
            self.save_notes(project, row["body"], checkpoint=True)
            return row["body"]

    def delete_project(self, project):
        with self.transaction():
            self.db.execute("""
                DELETE FROM fragments WHERE evidence IN (SELECT id FROM evidence WHERE project=?)
            """, (project,))
            self.db.execute("DELETE FROM projects WHERE id=?", (project,))

    def add(self, project, title, source, body, *, collected=None):
        with self.transaction():
            cursor = self.db.execute("""
                INSERT INTO evidence(project,title,source,body,collected) VALUES(?,?,?,?,?)
                ON CONFLICT(project, source, body) DO NOTHING
            """, (project, title, source, body, collected if collected is not None else int(time.time())))
            if cursor.rowcount:
                self.db.executemany("INSERT INTO fragments VALUES (?,?)", (
                    (cursor.lastrowid, body[start:start + 2400])
                    for start in range(0, len(body), 2200)
                ))
                return cursor.lastrowid
            return self.db.execute(
                "SELECT id FROM evidence WHERE project=? AND source=? AND body=?",
                (project, source, body),
            ).fetchone()["id"]

    def evidence(self, project):
        return self.db.execute(
            "SELECT * FROM evidence WHERE project=? ORDER BY id DESC", (project,)
        ).fetchall()

    def timeline(self, project):
        return self.db.execute("""
            SELECT t.*, e.source FROM timeline_events t LEFT JOIN evidence e ON e.id=t.evidence
            WHERE t.project=? ORDER BY t.date,t.id
        """, (project,)).fetchall()

    def save_event(self, project, day, title, notes, evidence, *, event=None):
        day = date.fromisoformat(day).isoformat()
        title = title.strip()
        if not title or len(title) > 300 or len(notes) > 8000:
            raise ValueError("Введите название до 300 символов и описание до 8000 символов")
        with self.transaction():
            if evidence is not None and not self.db.execute(
                "SELECT 1 FROM evidence WHERE project=? AND id=?", (project, evidence)
            ).fetchone():
                raise ValueError("Выберите материал текущего проекта")
            values = (day, title, notes, evidence, project)
            if event is None:
                self.db.execute(
                    "INSERT INTO timeline_events(date,title,notes,evidence,project) VALUES(?,?,?,?,?)", values
                )
            elif not self.db.execute("""
                UPDATE timeline_events SET date=?,title=?,notes=?,evidence=? WHERE project=? AND id=?
            """, (*values, event)).rowcount:
                raise ValueError("Событие не найдено в текущем проекте")

    def delete_event(self, project, event):
        with self.transaction():
            self.db.execute("DELETE FROM timeline_events WHERE project=? AND id=?", (project, event))

    def add_entity(self, project, kind, label):
        label = label.strip()
        if kind not in TYPES or not label or len(label) > 300:
            raise ValueError("Выберите тип и введите название до 300 символов")
        with self.transaction():
            return self.db.execute(
                "INSERT INTO entities(project,kind,label) VALUES(?,?,?)",
                (project, kind, label),
            ).lastrowid

    def entities(self, project):
        return self.db.execute(
            "SELECT * FROM entities WHERE project=? ORDER BY kind, id", (project,)
        ).fetchall()

    def add_relationship(self, project, origin, destination, relation, confidence, evidence):
        relation = relation.strip()
        if origin == destination or not relation or len(relation) > 300 or confidence not in CONFIDENCE:
            raise ValueError("Выберите разные сущности и введите описание связи до 300 символов")
        if evidence is None and confidence != "Гипотеза":
            raise ValueError("Укажите источник или сохраните связь как гипотезу")
        with self.transaction():
            if evidence is not None and not self.db.execute(
                "SELECT 1 FROM evidence WHERE project=? AND id=?", (project, evidence)
            ).fetchone():
                raise ValueError("Выберите материал текущего проекта")
            try:
                self.db.execute("""
                    INSERT INTO relationships(project,origin,destination,relation,confidence,evidence)
                    VALUES(?,?,?,?,?,?)
                """, (project, origin, destination, relation, confidence, evidence))
            except sqlite3.IntegrityError as error:
                raise ValueError("Обе сущности должны принадлежать текущему проекту") from error

    def relationships(self, project):
        return self.db.execute("""
            SELECT r.*, e.source, e.title FROM relationships r
            LEFT JOIN evidence e ON e.id=r.evidence
            WHERE r.project=? ORDER BY r.id
        """, (project,)).fetchall()

    def begin_api_event(self, project, provider):
        with self.transaction():
            return self.db.execute(
                "INSERT INTO api_events(project,provider,started,status) VALUES(?,?,?,?)",
                (project, provider, int(time.time()), "Начат"),
            ).lastrowid

    def finish_api_event(self, event, status, elapsed_ms):
        with self.transaction():
            self.db.execute(
                "UPDATE api_events SET status=?, elapsed_ms=? WHERE id=?", (status, elapsed_ms, event)
            )

    def api_events(self, project):
        return self.db.execute(
            "SELECT * FROM api_events WHERE project=? ORDER BY id DESC LIMIT 250", (project,)
        ).fetchall()

    def save_analysis(self, project, engine, model, question, claims):
        with self.transaction():
            return self.db.execute(
                "INSERT INTO analyses(project,engine,model,question,claims) VALUES(?,?,?,?,?)",
                (project, engine, model, question, json.dumps(claims, ensure_ascii=False)),
            ).lastrowid

    def update_analysis(self, project, analysis, engine, model, claims):
        with self.transaction():
            self.db.execute("""
                UPDATE analyses SET engine=?,model=?,claims=? WHERE id=? AND project=?
            """, (engine, model, json.dumps(claims, ensure_ascii=False), analysis, project))

    def analyses(self, project):
        return self.db.execute("SELECT * FROM analyses WHERE project=? ORDER BY id DESC", (project,)).fetchall()

    def search(self, project, query):
        terms = re.findall(r"[^\W_]+", query)[:16]
        if not terms:
            return []
        return self.db.execute("""
            SELECT e.id, e.source, e.title, f.body
            FROM fragments f JOIN evidence e ON e.id=f.evidence
            WHERE fragments MATCH ? AND e.project=? ORDER BY rank LIMIT 8
        """, (" OR ".join(f'"{term}"' for term in terms), project)).fetchall()


def ollama_models():
    result = request_json("http://127.0.0.1:11434/api/tags", local=True)
    return sorted({item["name"] for item in result["models"]})


def analyze(engine, model, prompt, key="", *, local_options=None):
    if engine == "Ollama":
        options = dict(local_options or {})
        system = options.pop("system", "").strip()
        messages = [{"role": "user", "content": prompt}]
        if system:
            messages.insert(0, {"role": "system", "content": system})
        result = request_json("http://127.0.0.1:11434/api/chat", {
            "model": model, "stream": False, "format": "json",
            "messages": messages, "options": options,
        }, local=True, timeout=180)
        return result["message"]["content"]
    if not key:
        raise ValueError("Задайте OPENAI_API_KEY в окружении")
    result = request_json("https://api.openai.com/v1/responses", {
        "model": model, "input": prompt, "max_output_tokens": 1500, "store": False
    }, {"Authorization": f"Bearer {key}"})
    return "\n".join(part["text"] for item in result["output"]
                     for part in item.get("content", []) if part["type"] == "output_text") or "API не вернул текст."
