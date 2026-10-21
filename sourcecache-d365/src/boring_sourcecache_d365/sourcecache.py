"""boring-sourcecache-d365: an index of a D365 PackagesLocalDirectory.

The design -- SQLite FTS5 with the trigram tokenizer narrows the files, exact
lines are cut from the stored text -- plus keys, models, labels and label
references as data:

    meta        key/value: name, versions, source, counts, tokenizer, builder
    models      package, model, layer, publisher, version, references (descriptors)
    files       id, path, package, model, element_type, name, kind, locale, label_file
    content     FTS5 trigram over each file's text, rowid = files.id
    labels      label_file, locale, key, ref (as code writes it), text, description
    label_text  FTS5 trigram over label texts, rowid = labels.id
    label_refs  ref, file_id, line -- every label reference in code, exact

Text is stored as decoded text with `\\n` line endings (not bytes). Lookups by
key go through `files`' indexes, never through a scan of the FTS table.
"""

from __future__ import annotations

import json
import tempfile
import uuid
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import __version__
from boring_service_kit.outcomes import Failed, Refused

SKIP_DIRS = {"bin", "XppMetadata", "Resources", "obj", ".git", ".vs"}
TEXT_SUFFIXES = {".xml", ".txt"}
MAX_FILE_BYTES = 24 * 1024 * 1024
LAYER_NAMES = {0: "sys", 1: "syp", 2: "gls", 3: "glp", 4: "fpk", 5: "fpp", 6: "sln", 7: "slp",
               8: "isv", 9: "isp", 10: "var", 11: "vap", 12: "cus", 13: "cup", 14: "usr", 15: "usp"}
MICROSOFT = ("Microsoft", "Microsoft Corporation")
# Match SQLite NOCASE exactly: it folds ASCII, not arbitrary Unicode.
_NOCASE = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
LABEL_REF = re.compile(rf"(?<![A-Za-z0-9_@])@{IDENT}(?::{IDENT})?(?![A-Za-z0-9_])")

SEARCH_DEFAULT, SEARCH_MAX = 20, 100
LINES_PER_FILE = 3
LINE_CHARS = 240
LABELS_DEFAULT, LABELS_MAX = 50, 500
ELEMENT_LINES_DEFAULT, ELEMENT_LINES_MAX = 400, 2000
ELEMENT_MAX_CHARS = 64 * 1024
# A query that runs longer than this is taken as stalled and stopped. Lenient on
# purpose: the indexes serve developers on dev boxes, and an odd, broad query
# may rightly take minutes. --stall-seconds 0 turns the guard off.
STALL_SECONDS_DEFAULT = 300
WARM_CHUNK = 8 * 1024 * 1024

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE models (package TEXT, model TEXT, layer INTEGER, layer_name TEXT, publisher TEXT,
                     version TEXT, refs TEXT, PRIMARY KEY (package, model));
CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT UNIQUE, package TEXT, model TEXT,
                    element_type TEXT, name TEXT, kind TEXT, locale TEXT, label_file TEXT, bytes INTEGER);
CREATE INDEX files_element ON files (element_type COLLATE NOCASE, name COLLATE NOCASE);
CREATE INDEX files_name ON files (name COLLATE NOCASE);
CREATE INDEX files_model ON files (package, model);
CREATE VIRTUAL TABLE content USING fts5(text, tokenize = 'trigram');
CREATE TABLE labels (id INTEGER PRIMARY KEY, file_id INTEGER, label_file TEXT, locale TEXT,
                     key TEXT, ref TEXT, text TEXT, description TEXT);
CREATE INDEX labels_ref ON labels (ref COLLATE NOCASE, locale COLLATE NOCASE);
CREATE VIRTUAL TABLE label_text USING fts5(text, content = 'labels', content_rowid = 'id',
                                           tokenize = 'trigram');
CREATE TABLE label_refs (ref TEXT, file_id INTEGER, line INTEGER);
CREATE INDEX label_refs_ref ON label_refs (ref COLLATE NOCASE);
"""


# --- building -------------------------------------------------------------


def _text(data: bytes) -> str | None:
    text = data.decode("utf-8-sig", errors="replace")
    if "\x00" in text:
        return None
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _descriptor(text: str) -> dict[str, Any]:
    root = ET.fromstring(text.encode("utf-8"))

    def value(tag: str) -> str | None:
        for child in root:
            if child.tag.rsplit("}", 1)[-1] == tag:
                return (child.text or "").strip()
        return None

    refs = []
    for child in root:
        if child.tag.rsplit("}", 1)[-1] == "ModuleReferences":
            refs = [(item.text or "").strip() for item in child if (item.text or "").strip()]
    layer_raw = value("Layer")
    layer = int(layer_raw) if layer_raw and layer_raw.isdigit() else None
    version = ".".join(value(part) or "0" for part in ("VersionMajor", "VersionMinor", "VersionBuild",
                                                        "VersionRevision"))
    return {"model": value("Name"), "package": value("ModelModule"), "layer": layer,
            "layer_name": LAYER_NAMES.get(layer, layer_raw.lower() if layer_raw else None),
            "publisher": value("Publisher"), "version": version, "refs": refs}


def parse_labels(text: str) -> Iterator[tuple[str, str, str | None]]:
    """(key, text, description) from a label file; `;` lines describe the label above."""
    current: list[Any] | None = None
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(";"):
            if current is not None:
                current[2] = (current[2] + "\n" if current[2] else "") + stripped[1:].strip()
            continue
        if "=" not in line:
            continue
        if current is not None:
            yield current[0], current[1], current[2]
        key, value = line.split("=", 1)
        current = [key.strip(), value, None]
    if current is not None:
        yield current[0], current[1], current[2]


def label_ref(label_file: str, key: str) -> str:
    """The form code uses: legacy keys carry their own `@`; new-style keys are `@File:Key`."""
    return key if key.startswith("@") else f"@{label_file}:{key}"


def _walk(source: Path) -> Iterator[Path]:
    """The text files of the tree. Symlinks are skipped: os.walk does not descend
    into linked folders, and a linked file could name anything outside the tree."""
    for directory, dirnames, filenames in os.walk(source):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(directory) / name
            if path.suffix.lower() in TEXT_SUFFIXES and not path.is_symlink():
                yield path


def classify(parts: tuple[str, ...]) -> dict[str, Any]:
    """Keys from a path below PackagesLocalDirectory (package/model/type/name...)."""
    keys: dict[str, Any] = {"package": parts[0] if parts else None, "model": None, "element_type": None,
                            "name": None, "kind": "other", "locale": None, "label_file": None}
    filename = parts[-1]
    if len(parts) == 3 and parts[1] == "Descriptor" and filename.lower().endswith(".xml"):
        keys.update(model=filename[:-4], kind="descriptor", name=filename[:-4])
    elif (len(parts) == 6 and parts[2] == "AxLabelFile" and parts[3] == "LabelResources"
          and filename.lower().endswith(".label.txt")):
        label_file = filename.split(".", 1)[0]
        keys.update(model=parts[1], element_type="AxLabelFile", name=label_file, kind="label",
                    locale=parts[4], label_file=label_file)
    elif len(parts) == 4 and filename.lower().endswith(".xml"):
        keys.update(model=parts[1], element_type=parts[2], name=filename[:-4], kind="element")
    return keys


def build(source: str, index: str, name: str, progress: Any = None,
          locales: list[str] | None = None) -> dict[str, Any]:
    """Build an index of `source` (a PackagesLocalDirectory) and publish it as
    `index`, a new immutable snapshot: never replaces an existing or open index.

    The index is built in a staged file beside it (created with the folder if
    needed) and published by a hard link, which is atomic and refuses to
    overwrite; the staged name always goes, also on failure or interruption.

    `locales` (e.g. ["en-US", "de"], matched case-insensitively and exactly):
    label files of other languages are left out entirely -- as labels and as
    searchable text. None takes every language in the tree.
    """
    root, target = Path(source), Path(index)
    if not root.is_dir():
        raise Refused("not_found", "source folder does not exist", path=source)
    if target.exists():
        raise Refused("stale", "index already exists; build a new versioned file and restart with it",
                      path=str(target))
    drive = target.drive  # "D:" on Windows, "" elsewhere
    if drive and not Path(drive + "\\").exists():
        raise Refused("not_found", f"drive {drive} does not exist on this machine; choose a data drive for --db",
                      path=index)
    try:  # the folder is created if needed
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".sourcecache-", suffix=".db", dir=target.parent)
    except OSError as error:
        raise Refused("forbidden", f"cannot create the folder or staged index for --db: {error}", path=index) from None
    os.close(fd)
    staged = Path(temp)
    try:
        report = _fill(root, staged, name, progress, locales)
        try:
            os.link(staged, target)  # complete file, atomic no-overwrite publication
        except FileExistsError:
            raise Refused("stale", "another builder published this index", path=str(target)) from None
        return {**report, "index": str(target)}
    finally:
        staged.unlink(missing_ok=True)


def _fill(root: Path, staged: Path, name: str, progress: Any, locales: list[str] | None) -> dict[str, Any]:
    """The index content, written into the (empty) staged file; build() publishes it."""
    wanted = {locale.strip().lower() for locale in locales or [] if locale.strip()} or None
    started = time.monotonic()
    try:
        db = sqlite3.connect(staged)
    except sqlite3.Error as error:
        raise Refused("forbidden", f"cannot create the index file: {error}", path=str(staged)) from None
    try:
        try:
            db.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;" + SCHEMA)
        except sqlite3.OperationalError as error:
            raise Refused("unsupported", "SQLite FTS5 with trigram support is required") from error
        counts = {"files": 0, "labels": 0, "label_refs": 0, "skipped": 0, "models": 0, "skipped_other_locales": 0}
        models: dict[tuple[str, str], dict[str, Any]] = {}
        pending_refs: list[tuple[str, int, int]] = []
        for path in _walk(root):
            parts = path.relative_to(root).parts
            keys = classify(parts)
            if wanted and keys["kind"] == "label" and (keys["locale"] or "").lower() not in wanted:
                counts["skipped_other_locales"] += 1
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    counts["skipped"] += 1
                    continue
                text = _text(path.read_bytes())
            except OSError:
                counts["skipped"] += 1
                continue
            if text is None:
                counts["skipped"] += 1
                continue
            cursor = db.execute(
                "INSERT INTO files (path, package, model, element_type, name, kind, locale, label_file, bytes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("/".join(parts), keys["package"], keys["model"], keys["element_type"], keys["name"], keys["kind"],
                 keys["locale"], keys["label_file"], len(text)))
            file_id = cursor.lastrowid
            db.execute("INSERT INTO content (rowid, text) VALUES (?, ?)", (file_id, text))
            counts["files"] += 1
            if keys["kind"] == "descriptor":
                try:
                    info = _descriptor(text)
                except ET.ParseError:
                    continue
                models[(info["package"] or keys["package"], info["model"] or keys["model"])] = info
            elif keys["kind"] == "label":
                for key, value, description in parse_labels(text):
                    db.execute("INSERT INTO labels (file_id, label_file, locale, key, ref, text, description) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (file_id, keys["label_file"], keys["locale"], key, label_ref(keys["label_file"], key),
                                value, description))
                    counts["labels"] += 1
            else:
                for number, line in enumerate(text.split("\n"), start=1):
                    if "@" in line:
                        pending_refs.extend((match.group(0), file_id, number) for match in LABEL_REF.finditer(line))
            if progress and counts["files"] % 10000 == 0:
                progress(counts["files"])
        db.executemany("INSERT INTO label_refs (ref, file_id, line) VALUES (?, ?, ?)", pending_refs)
        # Keep only references to labels that exist (drops `@param` and the like).
        db.execute("DELETE FROM label_refs WHERE ref COLLATE NOCASE NOT IN (SELECT ref FROM labels)")
        counts["label_refs"] = db.execute("SELECT COUNT(*) FROM label_refs").fetchone()[0]
        for (package, model), info in models.items():
            db.execute("INSERT OR REPLACE INTO models VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (package, model, info["layer"], info["layer_name"], info["publisher"], info["version"],
                        json.dumps(info["refs"])))
        counts["models"] = len(models)
        db.execute("INSERT INTO label_text (label_text) VALUES ('rebuild')")
        versions = {pkg: info["version"] for (pkg, _), info in models.items()}
        meta = {
            "name": name,
            "snapshot_id": uuid.uuid4().hex,
            "application_version": versions.get("ApplicationSuite"),
            "platform_version": versions.get("ApplicationPlatform"),
            "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": str(root),
            "tokenizer": "trigram",
            "builder": f"boring-sourcecache-d365 {__version__}",
            "locales": ",".join(sorted(wanted)) if wanted else "all",
            **{key: str(value) for key, value in counts.items()},
        }
        db.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
        db.execute("INSERT INTO content (content) VALUES ('optimize')")
        db.commit()
        db.execute("PRAGMA journal_mode = DELETE")
    finally:
        db.close()
    return {**meta, **counts, "seconds": round(time.monotonic() - started, 1)}  # counts as numbers


# --- querying ---------------------------------------------------------------


class Index:
    """One index file, read-only. Each thread gets its own connection, so a slow
    query never holds up the others; each call has a stall guard; warm_up()
    reads the file once so the OS keeps it in memory."""

    def __init__(self, path: str, stall_seconds: float = STALL_SECONDS_DEFAULT, pond_bound: bool = False) -> None:
        if not Path(path).is_file():
            raise Refused("not_found", "index file does not exist", path=path)
        self.path = path
        self.stall_seconds = stall_seconds
        self.pond_bound = pond_bound  # the stall guard is the Pond's call bound, not --stall-seconds
        self._uri = Path(path).resolve().as_uri() + "?mode=ro"
        self._local = threading.local()
        self.meta = {key: int(value) if isinstance(value, str) and value.isdigit() else value
                     for key, value in self.db.execute("SELECT key, value FROM meta")}  # counts as numbers
        self.name = self.meta.get("name", Path(path).stem)
        self.warm: dict[str, Any] = {"state": "cold"}

    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._uri, uri=True)
            try:
                # The immutable snapshot keeps its original schema. A small per-connection
                # descriptor index gives old snapshots unambiguous NOCASE joins.
                conn.execute("CREATE TEMP TABLE matched_models AS SELECT * FROM main.models WHERE 0")
                conn.execute("CREATE UNIQUE INDEX temp.matched_model_key ON matched_models "
                             "(package COLLATE NOCASE, model COLLATE NOCASE)")
                conn.execute("INSERT INTO matched_models SELECT * FROM main.models")
            except sqlite3.IntegrityError:
                conn.close()
                raise Refused("invalid_request", "ambiguous case-insensitive model descriptor keys") from None
            except Exception:
                conn.close()
                raise
            conn.set_progress_handler(self._stalled, 100_000)
            self._local.conn = conn
        return conn

    def _stalled(self) -> int:
        deadline = getattr(self._local, "deadline", None)
        return 1 if deadline is not None and time.monotonic() > deadline else 0

    def _guarded(self, call: Any) -> Any:
        self._local.deadline = time.monotonic() + self.stall_seconds if self.stall_seconds else None
        try:
            return call()
        except sqlite3.OperationalError as error:
            if "interrupt" not in str(error):
                raise
            advice = ("the Pond waits no longer for one call; narrow the query" if self.pond_bound
                      else "narrow it, or serve with a larger --stall-seconds")
            raise Failed("timeout", f"query stopped after {self.stall_seconds:g} s (stall guard); {advice}",
                         warm=self.warm.get("state")) from None
        finally:
            self._local.deadline = None

    def warm_up(self) -> None:
        """Read the whole file once (sequentially), so later queries find it in the OS cache."""
        size = os.path.getsize(self.path) or 1
        started = time.monotonic()
        done = 0
        self.warm = {"state": "warming", "percent": 0}
        try:
            with open(self.path, "rb", buffering=0) as handle:
                while chunk := handle.read(WARM_CHUNK):
                    done += len(chunk)
                    self.warm = {"state": "warming", "percent": min(99, done * 100 // size)}
        except OSError as error:
            self.warm = {"state": "failed", "error": str(error)}
            return
        self.warm = {"state": "warm", "seconds": round(time.monotonic() - started, 1), "mb": done // 2**20}

    def start_warm_up(self) -> threading.Thread:
        thread = threading.Thread(target=self.warm_up, name=f"warm-{self.name}", daemon=True)
        thread.start()
        return thread

    def stamp(self) -> dict[str, Any]:
        return {"name": self.name, "application_version": self.meta.get("application_version"),
                "built": self.meta.get("built"), "snapshot_id": self.meta.get("snapshot_id"),
                "platform_version": self.meta.get("platform_version")}

    # info ------------------------------------------------------------------

    def info(self) -> dict[str, Any]:
        return self._guarded(self._info)

    def _info(self) -> dict[str, Any]:
        # One grouped scan avoids a full files scan for every model on old indexes.
        counts = {(p.translate(_NOCASE), m.translate(_NOCASE)): n for p, m, n in self.db.execute(
            "SELECT package COLLATE NOCASE, model COLLATE NOCASE, COUNT(*) FROM files "
            "WHERE package IS NOT NULL AND model IS NOT NULL "
            "GROUP BY package COLLATE NOCASE, model COLLATE NOCASE")}
        models = [
            {"package": p, "model": m, "layer": layer_name or layer, "publisher": publisher,
             "scope": self._scope(publisher), "version": version,
             "files": counts.get((p.translate(_NOCASE), m.translate(_NOCASE)), 0)}
            for p, m, layer, layer_name, publisher, version in self.db.execute(
                "SELECT package, model, layer, layer_name, publisher, version "
                "FROM matched_models ORDER BY layer, package, model")
        ]
        return {"index": self.stamp(), "meta": self.meta, "models": models}

    # search ----------------------------------------------------------------

    @staticmethod
    def _phrase(query: str) -> str:
        if len(query.strip()) < 3:
            raise Refused("invalid_request", "query needs at least 3 characters (trigram index)")
        if "\n" in query:
            raise Refused("invalid_request", "query is matched within one line")
        return '"' + query.replace('"', '""') + '"'

    @staticmethod
    def _scope(publisher: str | None) -> str:
        if publisher is None or not publisher.strip():
            return "unknown"
        return "standard" if publisher in MICROSOFT else "custom"

    @staticmethod
    def _filters(package: str | None, model: str | None, element_type: str | None,
                 scope: str) -> tuple[str, list[Any]]:
        clauses, params = [], []
        for column, value in (("f.package", package), ("f.model", model), ("f.element_type", element_type)):
            if value:
                clauses.append(f"{column} = ? COLLATE NOCASE")
                params.append(value)
        if scope == "standard":
            clauses.append("m.publisher IN (?, ?)")
            params.extend(MICROSOFT)
        elif scope == "custom":
            clauses.append("(m.publisher IS NOT NULL AND trim(m.publisher) != '' AND m.publisher NOT IN (?, ?))")
            params.extend(MICROSOFT)
        elif scope != "all":
            raise Refused("invalid_request", "scope must be all, standard or custom")
        return "".join(f" AND {c}" for c in clauses), params

    def _lines(self, file_id: int, needle: str) -> tuple[list[dict[str, Any]], int]:
        text = self.db.execute("SELECT text FROM content WHERE rowid = ?", (file_id,)).fetchone()[0]
        lowered = needle.lower()
        hits = [(n, line) for n, line in enumerate(text.split("\n"), start=1) if lowered in line.lower()]
        return [{"line": n, "text": line.strip()[:LINE_CHARS]} for n, line in hits[:LINES_PER_FILE]], len(hits)

    def search(self, query: str, package: str | None = None, model: str | None = None,
               element_type: str | None = None, scope: str = "all", limit: int = SEARCH_DEFAULT,
               offset: int = 0) -> dict[str, Any]:
        return self._guarded(lambda: self._search(query, package, model, element_type, scope, limit, offset))

    def _search(self, query: str, package: str | None, model: str | None, element_type: str | None,
                scope: str, limit: int, offset: int) -> dict[str, Any]:
        if not 1 <= limit <= SEARCH_MAX or offset < 0:
            raise Refused("invalid_request", f"limit must be 1-{SEARCH_MAX}, offset 0 or more")
        phrase = self._phrase(query)
        where, params = self._filters(package, model, element_type, scope)
        # Keep FTS outermost: older SQLite otherwise drives filtered COUNT from
        # files_element and repeats MATCH for every metadata row.
        base = ("FROM content CROSS JOIN files f ON f.id = content.rowid "
                "LEFT JOIN matched_models m ON m.package = f.package COLLATE NOCASE AND m.model = f.model COLLATE NOCASE "
                f"WHERE content MATCH ?{where}")
        total = self.db.execute(f"SELECT COUNT(*) {base}", [phrase, *params]).fetchone()[0]
        rows = self.db.execute(
            f"SELECT f.id, f.path, f.package, f.model, f.element_type, f.name, f.kind, m.layer_name, m.publisher {base} "
            "ORDER BY f.package, f.model, f.element_type, f.name LIMIT ? OFFSET ?",
            [phrase, *params, limit, offset]).fetchall()
        results = []
        for file_id, path, pkg, mdl, etype, name, kind, layer, publisher in rows:
            lines, count = self._lines(file_id, query)
            results.append({"path": path, "package": pkg, "model": mdl, "element_type": etype, "name": name,
                            "kind": kind, "layer": layer, "scope": self._scope(publisher), "matching_lines": count, "lines": lines})
        return {"index": self.stamp(), "query": query, "total_files": total, "offset": offset,
                "results": results, "truncated": offset + len(results) < total}

    # element ---------------------------------------------------------------

    def element(self, element_type: str, name: str, package: str | None = None, model: str | None = None,
                start_line: int = 1, line_count: int = ELEMENT_LINES_DEFAULT) -> dict[str, Any]:
        return self._guarded(lambda: self._element(element_type, name, package, model, start_line, line_count))

    def _element(self, element_type: str, name: str, package: str | None, model: str | None,
                 start_line: int, line_count: int) -> dict[str, Any]:
        if start_line < 1 or not 1 <= line_count <= ELEMENT_LINES_MAX:
            raise Refused("invalid_request", f"start_line from 1, line_count 1-{ELEMENT_LINES_MAX}")
        where, params = self._filters(package, model, None, "all")
        rows = self.db.execute(
            "SELECT f.id, f.path, f.package, f.model, m.layer_name, m.publisher FROM files f "
            "LEFT JOIN matched_models m ON m.package = f.package COLLATE NOCASE AND m.model = f.model COLLATE NOCASE "
            f"WHERE f.element_type = ? COLLATE NOCASE AND f.name = ? COLLATE NOCASE{where} "
            "ORDER BY m.layer, f.package, f.model", [element_type, name, *params]).fetchall()
        if not rows:
            raise Refused("not_found", "no such element", element_type=element_type, name=name)
        if len(rows) > 1:
            return {"index": self.stamp(), "ambiguous": True,
                    "candidates": [{"package": p, "model": m, "layer": layer, "scope": self._scope(publisher), "path": path}
                                   for _, path, p, m, layer, publisher in rows],
                    "hint": "name package or model to choose one"}
        file_id, path, pkg, mdl, layer, publisher = rows[0]
        text = self.db.execute("SELECT text FROM content WHERE rowid = ?", (file_id,)).fetchone()[0]
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        selected: list[str] = []
        used = 0
        cut = False
        for line in lines[start_line - 1:start_line - 1 + line_count]:
            piece = line + "\n"
            if used + len(piece) > ELEMENT_MAX_CHARS:
                if not selected:
                    selected.append(piece[:ELEMENT_MAX_CHARS])
                    cut = True  # one oversized line; reported explicitly below
                break
            selected.append(piece)
            used += len(piece)
        content = "".join(selected)
        end = start_line - 1 + len(selected)
        return {"index": self.stamp(), "path": path, "package": pkg, "model": mdl, "layer": layer, "scope": self._scope(publisher),
                "element_type": element_type, "name": name, "content": content, "start_line": start_line,
                "total_lines": len(lines), "next_line": end + 1 if end < len(lines) else None,
                "returned_lines": len(selected), "line_cut": cut,
                "truncated": end < len(lines) or cut}

    # labels ----------------------------------------------------------------

    @staticmethod
    def normalize_ref(label_id: str) -> str:
        label_id = label_id.strip()
        if label_id.startswith("@"):
            return label_id
        return "@" + label_id

    def labels(self, text: str | None = None, label_id: str | None = None, locale: str | None = None,
               references: bool = False, limit: int = LABELS_DEFAULT) -> dict[str, Any]:
        return self._guarded(lambda: self._labels(text, label_id, locale, references, limit))

    def _labels(self, text: str | None, label_id: str | None, locale: str | None,
                references: bool, limit: int) -> dict[str, Any]:
        if (text is None) == (label_id is None):
            raise Refused("invalid_request", "give either text or label_id")
        if not 1 <= limit <= LABELS_MAX:
            raise Refused("invalid_request", f"limit must be 1-{LABELS_MAX}")
        indexed = self.meta.get("locales", "all")
        if locale and indexed != "all" and locale.lower() not in indexed.split(","):
            raise Refused("not_found", "this index has no labels in that locale", locale=locale, locales=indexed)
        locale_clause, locale_params = ("", []) if not locale else (" AND l.locale = ? COLLATE NOCASE", [locale])
        if text is not None:
            if references:
                raise Refused("invalid_request", "references are listed per label_id")
            phrase = self._phrase(text)
            base = f"FROM label_text JOIN labels l ON l.id = label_text.rowid WHERE label_text MATCH ?{locale_clause}"
            total = self.db.execute(f"SELECT COUNT(*) {base}", [phrase, *locale_params]).fetchone()[0]
            rows = self.db.execute(
                f"SELECT l.ref, l.text, l.label_file, l.locale, l.description {base} "
                "ORDER BY (lower(l.text) = lower(?)) DESC, length(l.text), l.ref LIMIT ?",
                [phrase, *locale_params, text, limit]).fetchall()
            return {"index": self.stamp(), "text": text, "total": total, "truncated": total > len(rows),
                    "labels": [{"ref": r, "text": t, "label_file": f, "locale": loc, "description": d}
                               for r, t, f, loc, d in rows]}
        ref = self.normalize_ref(label_id)
        labels_total = self.db.execute(f"SELECT COUNT(*) FROM labels l WHERE l.ref = ? COLLATE NOCASE{locale_clause}",
                                       [ref, *locale_params]).fetchone()[0]
        rows = self.db.execute(
            "SELECT l.ref, l.text, l.label_file, l.locale, l.description FROM labels l "
            f"WHERE l.ref = ? COLLATE NOCASE{locale_clause} "
            "ORDER BY lower(l.locale) != 'en-us', l.locale LIMIT ?", [ref, *locale_params, limit]).fetchall()
        result: dict[str, Any] = {
            "index": self.stamp(), "ref": ref, "labels_total": labels_total,
            "labels": [{"ref": r, "text": t, "label_file": f, "locale": loc, "description": d}
                       for r, t, f, loc, d in rows]}
        if labels_total > len(rows):
            result["labels_truncated"] = True
        if not rows:
            result["note"] = "no label with this ID in this index"
        if references:
            total = self.db.execute("SELECT COUNT(*) FROM label_refs WHERE ref = ? COLLATE NOCASE",
                                    [ref]).fetchone()[0]
            refs = self.db.execute(
                "SELECT f.id, f.path, f.package, f.model, f.element_type, f.name, r.line FROM label_refs r "
                "JOIN files f ON f.id = r.file_id WHERE r.ref = ? COLLATE NOCASE "
                "ORDER BY f.package, f.model, f.element_type, f.name, r.line LIMIT ?", [ref, limit]).fetchall()
            texts: dict[int, list[str]] = {}
            found = []
            for file_id, path, pkg, mdl, etype, name, line in refs:
                if file_id not in texts:
                    texts[file_id] = self.db.execute("SELECT text FROM content WHERE rowid = ?",
                                                     (file_id,)).fetchone()[0].split("\n")
                found.append({"path": path, "package": pkg, "model": mdl, "element_type": etype, "name": name,
                              "line": line, "text": texts[file_id][line - 1].strip()[:LINE_CHARS]})
            result.update(references_total=total, references=found, truncated=total > len(found))
        return result
