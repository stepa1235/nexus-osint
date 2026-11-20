"""Versioned, portable exports of individual investigations."""
import json
import os
from contextlib import closing
from tempfile import NamedTemporaryFile
from typing import NamedTuple

from core import Store

FORMAT = "nexus-osint-project"
VERSION = 1
LIMIT = 64 * 1024 * 1024


class EvidenceRef(NamedTuple):
    id: int
    source: str
    body: str


def fields(row, names):
    return {name: row[name] for name in names.split()}


def export_project(database, project, destination):
    if destination.resolve() == database.resolve() or (
        destination.exists() and os.path.samefile(database, destination)
    ):
        raise ValueError("Выберите файл отдельно от рабочей базы")
    store = Store(database)
    with closing(store.db) as db, store.transaction():
        meta = db.execute("SELECT name,notes FROM projects WHERE id=?", (project,)).fetchone()
        if meta is None:
            raise ValueError("Проект не найден")
        payload = {
            "format": FORMAT,
            "version": VERSION,
            "project": dict(meta),
            "note_versions": [
                fields(row, "body created") for row in reversed(store.note_versions(project))
            ],
            "evidence": [fields(row, "id title source body collected") for row in reversed(store.evidence(project))],
            "timeline": [fields(row, "date title notes evidence") for row in store.timeline(project)],
            "entities": [fields(row, "id kind label") for row in store.entities(project)],
            "relationships": [fields(row, "origin destination relation confidence evidence")
                              for row in store.relationships(project)],
            "analyses": [
                {**fields(row, "engine model question"), "claims": json.loads(row["claims"])}
                for row in reversed(store.analyses(project))
            ],
        }
    write_package(destination, payload)


def write_package(path, payload):
    temp = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".nexus-", delete=False) as file:
            temp = file.name
            total = 0
            for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(payload):
                data = chunk.encode("utf-8")
                total += len(data)
                if total > LIMIT:
                    raise ValueError("Размер файла проекта превышает 64 МиБ")
                file.write(data)
            file.write(b"\n" if total < LIMIT else b"")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None:
            os.unlink(temp)


def text(row, key):
    value = row[key]
    if not isinstance(value, str):
        raise ValueError(f"Поле {key} должно быть строкой")
    return value


def integer(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("Недопустимый идентификатор или время получения")
    return value


def records(value):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("Некорректный список записей проекта")
    return value


def remap_claims(claims, evidence):
    result = []
    for claim in records(claims):
        new_claim = fields(claim, "text status")
        for key in new_claim:
            text(claim, key)
        if "provider" in claim:
            new_claim["provider"] = text(claim, "provider")
        citations = []
        for citation in records(claim["citations"]):
            entry = evidence[integer(citation["id"])]
            quote = text(citation, "quote")
            if not quote.strip() or quote not in entry.body:
                raise ValueError("Цитата не найдена в материале проекта")
            citations.append({"id": entry.id, "source": entry.source, "quote": quote})
        new_claim["citations"] = citations
        result.append(new_claim)
    return result


def import_project(database, source):
    with source.open("rb") as file:
        raw = file.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("Размер файла проекта превышает 64 МиБ")
    payload = json.loads(raw)
    if (not isinstance(payload, dict) or payload.get("format") != FORMAT
            or type(payload.get("version")) is not int or payload["version"] != VERSION):
        raise ValueError("Формат или версия файла проекта не поддерживается")
    store = Store(database)
    with closing(store.db), store.transaction():
        try:
            return restore(store, payload)
        except (KeyError, TypeError) as error:
            raise ValueError("Файл проекта содержит отсутствующие поля или неверные ссылки") from error


def restore(store, payload):
    meta = payload["project"]
    project = store.project(text(meta, "name"))
    store.db.execute("UPDATE projects SET notes=? WHERE id=?", (text(meta, "notes"), project))
    for row in records(payload.get("note_versions", [])):
        store.add_note_version(project, text(row, "body"), integer(row["created"]))
    evidence = {}
    for row in records(payload["evidence"]):
        old_id = integer(row["id"])
        if old_id in evidence:
            raise ValueError("Повторяющийся идентификатор материала")
        source, body = text(row, "source"), text(row, "body")
        new_id = store.add(project, text(row, "title"), source, body, collected=integer(row["collected"]))
        evidence[old_id] = EvidenceRef(new_id, source, body)
    for row in records(payload.get("timeline", [])):
        ref = row["evidence"]
        store.save_event(
            project, text(row, "date"), text(row, "title"), text(row, "notes"),
            evidence[integer(ref)].id if ref is not None else None,
        )
    entities = {}
    for row in records(payload["entities"]):
        old_id = integer(row["id"])
        if old_id in entities:
            raise ValueError("Повторяющийся идентификатор сущности")
        entities[old_id] = store.add_entity(project, text(row, "kind"), text(row, "label"))
    for row in records(payload["relationships"]):
        source = row["evidence"]
        store.add_relationship(
            project, entities[integer(row["origin"])], entities[integer(row["destination"])],
            text(row, "relation"), text(row, "confidence"),
            evidence[integer(source)].id if source is not None else None,
        )
    for row in records(payload["analyses"]):
        store.save_analysis(
            project, text(row, "engine"), text(row, "model"), text(row, "question"),
            remap_claims(row["claims"], evidence),
        )
    return project
