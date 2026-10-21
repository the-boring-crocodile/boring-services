"""The notebook: entries with append-only versions.

    meta        key/value: role (global|premise), created, schema
    entries     id, created, created_by, version (current), retired
    versions    entry_id, version, at, author, by, kind, title, body, tags, context,
                evidence, confidence, reason, retired -- never updated, only added;
                kind: add, revise, confirm, retire
    entry_tags  entry_id, tag -- the current version's tags, for exact filters
    entry_text  FTS5 trigram over the current version (title, body, tags, context, evidence)

The author of every version is the verified caller, passed in by the transport
(its access policy's author for the token's subject) -- never taken from the
caller's arguments. `by` is the caller's own, unverified note
of which agent wrote it.
"""

from __future__ import annotations

import json
import functools
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from boring_service_kit.outcomes import Refused

ROLES = ("global", "premise")
CONFIDENCE = ("observed", "inferred", "hypothesis")
TAG = re.compile(r"^[a-z0-9][a-z0-9.+_-]{0,39}$")
LIMITS = {"title": 200, "body": 8000, "context": 200, "evidence": 2000, "reason": 300, "by": 80}
MAX_TAGS = 10
SEARCH_DEFAULT, SEARCH_MAX = 20, 100
SNIPPET_CHARS = 200
COUNT_CAP = 1000  # totals beyond this are reported as a lower bound (total_capped)
SCHEMA_VERSION = "3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS entries (id INTEGER PRIMARY KEY, created TEXT, created_by TEXT,
                                    version INTEGER, retired INTEGER DEFAULT 0, updated TEXT);
CREATE TABLE IF NOT EXISTS versions (entry_id INTEGER, version INTEGER, at TEXT, author TEXT, by TEXT,
                                     title TEXT, body TEXT, tags TEXT, context TEXT, evidence TEXT,
                                     confidence TEXT, reason TEXT, retired INTEGER DEFAULT 0, kind TEXT,
                                     PRIMARY KEY (entry_id, version));
CREATE TABLE IF NOT EXISTS entry_tags (entry_id INTEGER, tag TEXT);
CREATE INDEX IF NOT EXISTS entry_tags_tag ON entry_tags (tag);
CREATE INDEX IF NOT EXISTS entries_updated ON entries (retired, updated);
CREATE VIRTUAL TABLE IF NOT EXISTS entry_text USING fts5(title, body, tags, context, evidence, tokenize = 'trigram');
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(name: str, value: str | None, required: bool = False, minimum: int = 1) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise Refused("invalid_request", f"{name} is required")
        return None
    value = value.strip()
    if len(value) < minimum or len(value) > LIMITS[name]:
        raise Refused("invalid_request", f"{name} must be {minimum}-{LIMITS[name]} characters", field=name)
    return value


def _tags(tags: list[str] | None) -> list[str]:
    clean = []
    for tag in tags or []:
        tag = tag.strip().lower()
        if not TAG.match(tag):
            raise Refused("invalid_request", "tags are lowercase letters, digits and . + _ -, at most 40 characters",
                          tag=tag)
        if tag not in clean:
            clean.append(tag)
    if len(clean) > MAX_TAGS:
        raise Refused("invalid_request", f"at most {MAX_TAGS} tags")
    return clean


def _phrase(text: str, name: str) -> str:
    if len(text.strip()) < 3:
        raise Refused("invalid_request", f"{name} needs at least 3 characters (trigram index)")
    return '"' + text.replace('"', '""') + '"'


def _age_days(at: str) -> float:
    then = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - then).total_seconds() / 86400, 1)


def _snippet(body: str, query: str | None) -> str:
    """Up to SNIPPET_CHARS of the body, centred on the first hit when there is one."""
    text = " ".join(body.split())
    if len(text) <= SNIPPET_CHARS:
        return text
    start = 0
    if query:
        hit = text.lower().find(query.lower())
        if hit > 0:
            start = max(0, min(hit - SNIPPET_CHARS // 3, len(text) - SNIPPET_CHARS))
    piece = text[start:start + SNIPPET_CHARS]
    return ("..." if start > 0 else "") + piece + ("..." if start + SNIPPET_CHARS < len(text) else "")


def _confidence(value: str | None) -> str:
    value = value or "observed"
    if value not in CONFIDENCE:
        raise Refused("invalid_request", f"confidence must be one of {', '.join(CONFIDENCE)}")
    return value


def serialized(fn):
    """The store's one lock: every public method holds it for its whole call
    (one SQLite connection, shared by the server's threads)."""
    @functools.wraps(fn)
    def call(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return call


class Store:
    def __init__(self, path: str, role: str = "global") -> None:
        if role not in ROLES:
            raise Refused("invalid_request", f"role must be one of {', '.join(ROLES)}")
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            has_meta = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
            stored = dict(self.db.execute("SELECT key, value FROM meta")) if has_meta else {}
            if stored and stored.get("schema") not in {"1", "2", SCHEMA_VERSION}:
                raise Refused("unsupported", "unknown notebook schema; database left unchanged",
                              schema=stored.get("schema"))
            if stored and stored.get("role") != role:
                raise Refused("invalid_request", "this notebook was created with another role",
                              role=stored.get("role"), requested=role)
            self.db.execute("PRAGMA journal_mode = WAL")
            self.db.execute("PRAGMA foreign_keys = ON")
            self.db.executescript("BEGIN IMMEDIATE;" + SCHEMA)
            if not stored:
                self.db.executemany("INSERT INTO meta VALUES (?, ?)",
                                    [("role", role), ("created", _now()), ("schema", SCHEMA_VERSION)])
            self._check()
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            self.db.close()
            raise
        self.role = role

    def _check(self) -> None:
        """Schema 3 only, exactly as this release creates it; anything else is left unchanged."""
        version = self.db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if version is None or version[0] != SCHEMA_VERSION:
            raise Refused("unsupported", f"notebook schema {version[0] if version else None}; "
                          f"this release reads schema {SCHEMA_VERSION}; database left unchanged")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(versions)")}
        indexed = [row[1] for row in self.db.execute("PRAGMA table_info(entry_text)")]
        if "kind" not in columns or indexed != ["title", "body", "tags", "context", "evidence"]:
            raise Refused("unsupported", "unknown notebook layout; database left unchanged")

    # --- writing ----------------------------------------------------------------

    def _write_version(self, entry_id: int, version: int, author: str, fields: dict[str, Any]) -> None:
        now = _now()
        self.db.execute(
            "INSERT INTO versions (entry_id, version, at, author, by, kind, title, body, tags, context, evidence, "
            "confidence, reason, retired) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry_id, version, now, author, fields["by"], fields["kind"], fields["title"], fields["body"],
             json.dumps(fields["tags"]), fields["context"], fields["evidence"], fields["confidence"],
             fields["reason"], int(fields["retired"])))
        self.db.execute("UPDATE entries SET version = ?, retired = ?, updated = ? WHERE id = ?",
                        (version, int(fields["retired"]), now, entry_id))
        self.db.execute("DELETE FROM entry_tags WHERE entry_id = ?", (entry_id,))
        self.db.executemany("INSERT INTO entry_tags VALUES (?, ?)", [(entry_id, t) for t in fields["tags"]])
        self.db.execute("DELETE FROM entry_text WHERE rowid = ?", (entry_id,))
        self.db.execute("INSERT INTO entry_text (rowid, title, body, tags, context, evidence) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (entry_id, fields["title"], fields["body"], " ".join(fields["tags"]),
                         fields["context"] or "", fields["evidence"] or ""))

    @serialized
    def add(self, author: str, title: str, body: str, tags: list[str] | None = None, context: str | None = None,
            evidence: str | None = None, confidence: str | None = None, by: str | None = None) -> dict[str, Any]:
        fields = {"title": _text("title", title, True, 3), "body": _text("body", body, True),
                  "tags": _tags(tags), "context": _text("context", context),
                  "evidence": _text("evidence", evidence), "confidence": _confidence(confidence),
                  "by": _text("by", by), "reason": None, "retired": False, "kind": "add"}
        self.db.execute("BEGIN IMMEDIATE")
        try:
            entry_id = self.db.execute("INSERT INTO entries (created, created_by, version) VALUES (?, ?, 0)",
                                       (_now(), author)).lastrowid
            self._write_version(entry_id, 1, author, fields)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return self.get(entry_id)

    def _change(self, author: str, entry_id: int, expected_version: int, reason: str | None,
                by: str | None, updates: dict[str, Any], retire: bool, kind: str) -> dict[str, Any]:
        """For revise, retire and confirm, which hold the lock (@serialized)."""
        reason = _text("reason", reason, True)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT version, retired FROM entries WHERE id = ?", (entry_id,)).fetchone()
            if row is None:
                raise Refused("not_found", "no such entry", id=entry_id)
            version, retired = row
            if version != expected_version:
                raise Refused("stale", "the entry changed since it was read", id=entry_id,
                              current_version=version)
            if retired:
                raise Refused("invalid_request", "the entry is retired; add a new entry instead", id=entry_id)
            current = self._version(entry_id, version)
            fields = {k: current[k] for k in ("title", "body", "tags", "context", "evidence", "confidence")}
            fields.update({k: v for k, v in updates.items() if v is not None})
            fields.update(by=_text("by", by), reason=reason, retired=retire, kind=kind)
            self._write_version(entry_id, version + 1, author, fields)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return self.get(entry_id)

    @serialized
    def revise(self, author: str, entry_id: int, expected_version: int, reason: str, title: str | None = None,
               body: str | None = None, tags: list[str] | None = None, context: str | None = None,
               evidence: str | None = None, confidence: str | None = None, by: str | None = None) -> dict[str, Any]:
        updates = {"title": _text("title", title, minimum=3) if title is not None else None,
                   "body": _text("body", body) if body is not None else None,
                   "tags": _tags(tags) if tags is not None else None,
                   "context": _text("context", context) if context is not None else None,
                   "evidence": _text("evidence", evidence) if evidence is not None else None,
                   "confidence": _confidence(confidence) if confidence is not None else None}
        if all(v is None for v in updates.values()):
            raise Refused("invalid_request", "a revision changes at least one field")
        return self._change(author, entry_id, expected_version, reason, by, updates, retire=False, kind="revise")

    @serialized
    def retire(self, author: str, entry_id: int, expected_version: int, reason: str,
               by: str | None = None) -> dict[str, Any]:
        return self._change(author, entry_id, expected_version, reason, by, {}, retire=True, kind="retire")

    @serialized
    def confirm(self, author: str, entry_id: int, expected_version: int, evidence: str,
                by: str | None = None) -> dict[str, Any]:
        """Checked again, still true: a new version with the same content and fresh evidence."""
        evidence = _text("evidence", evidence, True)
        return self._change(author, entry_id, expected_version, "confirmed against fresh evidence", by,
                            {"evidence": evidence}, retire=False, kind="confirm")

    # --- reading ----------------------------------------------------------------

    def _version(self, entry_id: int, version: int) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT version, at, author, by, kind, title, body, tags, context, evidence, confidence, reason, retired "
            "FROM versions WHERE entry_id = ? AND version = ?", (entry_id, version)).fetchone()
        keys = ("version", "at", "author", "by", "kind", "title", "body", "tags", "context", "evidence",
                "confidence", "reason", "retired")
        data = dict(zip(keys, row))
        data["tags"] = json.loads(data["tags"])
        data["retired"] = bool(data["retired"])
        return data

    @serialized
    def get(self, entry_id: int, history: bool = False) -> dict[str, Any]:
        row = self.db.execute("SELECT created, created_by, version FROM entries WHERE id = ?",
                              (entry_id,)).fetchone()
        if row is None:
            raise Refused("not_found", "no such entry", id=entry_id)
        created, created_by, version = row
        result = {"id": entry_id, "created": created, "created_by": created_by, **self._version(entry_id, version)}
        result["age_days"] = _age_days(result["at"])
        if history:
            result["history"] = [
                {k: v for k, v in self._version(entry_id, n).items() if k in ("version", "at", "author", "by",
                                                                               "kind", "reason", "retired", "title")}
                for n in range(version, 0, -1)]
        return result

    def _summary(self, entry_id: int, query: str | None) -> dict[str, Any]:
        entry = self.get(entry_id)
        return {"id": entry_id, "title": entry["title"], "tags": entry["tags"], "context": entry["context"],
                "version": entry["version"], "updated": entry["at"], "age_days": entry["age_days"],
                "author": entry["author"], "created_by": entry["created_by"], "retired": entry["retired"],
                "snippet": _snippet(entry["body"], query)}

    @serialized
    def search(self, query: str | None = None, tag: str | None = None, context: str | None = None,
               author: str | None = None, include_retired: bool = False, limit: int = SEARCH_DEFAULT,
               offset: int = 0, max_age_days: float | None = None) -> dict[str, Any]:
        if not 1 <= limit <= SEARCH_MAX or offset < 0:
            raise Refused("invalid_request", f"limit must be 1-{SEARCH_MAX}, offset 0 or more")
        clauses, params = [], []
        source = "entries e"
        match = []
        if query is not None:
            # Long literals use FTS. Short words in a mixed query are checked
            # against current fields after FTS narrows candidates. An all-short
            # query cannot use the trigram index and is refused explicitly.
            terms = query.split()
            long_terms = [term for term in terms if len(term) >= 3]
            if not long_terms:
                raise Refused("invalid_request", "query needs a term of at least 3 characters")
            match.extend(_phrase(term, "query term") for term in long_terms)
            for short in (term for term in terms if len(term) < 3):
                clauses.append("EXISTS (SELECT 1 FROM versions v WHERE v.entry_id=e.id AND v.version=e.version "
                               "AND (" + " OR ".join(
                                   f"instr(lower(coalesce(v.{field}, '')), lower(?)) > 0"
                                   for field in ("title", "body", "tags", "context", "evidence")) + "))")
                params.extend([short] * 5)
        if context:
            match.append("context : " + _phrase(context, "context"))
        if match:
            # The full-text query runs once, into a lookup set (IN with a subquery is evaluated once).
            # As a join, SQLite walked entries by the updated index and re-ran the MATCH per row:
            # 8 s instead of 8 ms at 10,000 entries. A MATERIALIZED CTE did not
            # change that plan on SQLite 3.37.
            clauses.append("e.id IN (SELECT rowid FROM entry_text WHERE entry_text MATCH ?)")
            params.append(" AND ".join(match))
        if tag:
            clauses.append("e.id IN (SELECT entry_id FROM entry_tags WHERE tag = ?)")
            params.append(tag.strip().lower())
        if max_age_days is not None:
            if max_age_days <= 0:
                raise Refused("invalid_request", "max_age_days must be positive")
            clauses.append("e.updated >= ?")
            params.append((datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        if author:
            # The author a result shows: of the current version (created_by is
            # who added the entry; history lists every version's author).
            clauses.append("EXISTS (SELECT 1 FROM versions v WHERE v.entry_id=e.id AND v.version=e.version "
                           "AND v.author = ?)")
            params.append(author)
        if not include_retired:
            clauses.append("e.retired = 0")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = self.db.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM {source}{where} LIMIT ?)",
                                [*params, COUNT_CAP + 1]).fetchone()[0]
        capped = total > COUNT_CAP
        ids = [r[0] for r in self.db.execute(
            f"SELECT e.id FROM {source}{where} ORDER BY e.updated DESC, e.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset])]
        result = {"role": self.role, "total": min(total, COUNT_CAP), "offset": offset,
                  "truncated": capped or offset + len(ids) < total,
                  "entries": [self._summary(i, query) for i in ids]}
        if capped:
            result["total_capped"] = True  # at least this many; narrow the search
        return result

    @serialized
    def recent(self, limit: int = SEARCH_DEFAULT, include_retired: bool = True) -> dict[str, Any]:
        return self.search(limit=limit, include_retired=include_retired)
