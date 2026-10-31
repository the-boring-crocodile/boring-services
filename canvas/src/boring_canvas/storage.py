"""Canvas state: one SQLite database in the state directory, hashed tokens.

    meta      key/value: schema, created, imported (legacy files read at creation)
    canvases  id, token_hash, scene_svg, elements_json, created_s, modified_s,
              owner, name -- (owner, name) unique; both set for an MCP canvas,
              both NULL for an ownerless one made before canvases were MCP-only

The choices: HTTP not Pirate-IPC; SQLite like Knowledge and Fishmarket, which
replaced one JSON file per canvas, a file lock and a hard-link name index
(0.2.18); the token stored as a hash and never round-tripped back out.

The server and `boring-canvas-admin` open the same database from two
processes. Every write is one IMMEDIATE transaction, so a write that read a
record cannot publish it after an admin delete or token reissue, and the
count and name checks of a create are atomic with its insert.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("boring_canvas.storage")

_SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", _SVG_NS)

# 42 days: an absolute TTL from creation, not a sliding idle window, because
# canvases get revisited across multi-day working sessions. Configurable per
# CanvasStore and via server.py's --max-age-days.
DEFAULT_MAX_AGE_DAYS = 42

# A chat can create canvases, so creation is limited. RATE_LIMIT catches a
# tight-loop burst; MAX_CANVASES catches slow accumulation even when the rate
# limit is respected. 0 means unlimited on either.
DEFAULT_CREATE_RATE_LIMIT = 10  # per RATE_LIMIT_WINDOW_S; 0 = unlimited
RATE_LIMIT_WINDOW_S = 60
DEFAULT_MAX_CANVASES = 100  # total existing at once; 0 = unlimited

# A canvas created over MCP belongs to the Pond that created it (`owner`, the
# introspected token's `sub`) and has a `name` unique per owner.
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
_NAME_RE = re.compile(NAME_PATTERN)

# The id canvas.html gives the pushed drawing (PUSHED_IMAGE_ID there; a test
# keeps the two equal) and the fields that say a person moved it.
PUSHED_SCENE_ELEMENT_ID = "boring-canvas-pushed-scene"
_PUSHED_GEOMETRY = ("x", "y", "width", "height", "angle", "isDeleted")

DB_FILE = "canvas.sqlite"
SCHEMA_VERSION = "1"
SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE canvases (
  id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, scene_svg TEXT, elements_json TEXT,
  created_s REAL NOT NULL, modified_s REAL NOT NULL, owner TEXT, name TEXT,
  CHECK ((owner IS NULL) = (name IS NULL)), UNIQUE (owner, name));
"""
# Seconds a write waits for the other process's transaction (admin beside the server).
BUSY_TIMEOUT_S = 30
EDIT_LOG_ENTRIES = 500  # per canvas, newest kept (get_edits)


class CanvasNotFound(Exception):
    """No canvas exists at this id or name."""


class InvalidToken(Exception):
    """The token presented does not match the canvas's stored hash."""


class InvalidSVG(Exception):
    """Pushed content is not well-formed XML, so it cannot be sanitized."""


class InvalidElements(Exception):
    """Posted scene elements are not valid JSON."""


class RateLimited(Exception):
    """Too many canvases created in the current rolling window."""


class TooManyCanvases(Exception):
    """At or over the configured total-canvas ceiling."""


class NameTaken(Exception):
    """The owner already has a canvas with this name."""


class StoreRefused(Exception):
    """The database cannot be used by this release (unknown schema) or the
    legacy canvas files cannot be imported. Nothing was changed."""


@dataclass(frozen=True)
class Canvas:
    canvas_id: str
    token_hash: str
    scene_svg: str | None
    elements_json: str | None
    created_s: float
    owner: str | None = None  # Pond (token sub) of an MCP-created canvas, else None
    name: str | None = None  # set exactly when owner is


_COLUMNS = "id, token_hash, scene_svg, elements_json, created_s, owner, name"


def _canvas(row: tuple) -> Canvas:
    return Canvas(*row)


def scene_version_of(scene_svg: str | None) -> str:
    """Identity of the sanitized SVG snapshot, shared by polling, rendering and projection."""
    return hashlib.sha256((scene_svg or "").encode("utf-8")).hexdigest()[:16]


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _sanitize_svg(raw: str) -> str:
    """Strip active-content elements and on*=/javascript: attributes.

    Removed elements: script, foreignObject (it embeds arbitrary XHTML, and
    an <iframe> inside one carries no script tag or on* attribute for the
    other checks to catch), object, embed and iframe, and the animation
    elements set/animate/animateTransform/animateMotion (they can set any
    attribute, href included, after sanitizing). A `use` may only reference
    a fragment of this document ("#id"), not an external file.

    Attribute values are normalized before the scheme check
    (_url_scheme_text), so "java&#09;script:" does not pass: the XML parser
    has decoded the character reference to a tab, which a browser's URL
    parser drops. javascript:, vbscript: and data: values are removed,
    except data:image/ (embedded raster images in exported SVGs).

    canvas.html only ever uses the SVG as an image source, where browsers
    run no scripts. The sanitizer does not rely on that: a later route that
    served the SVG as a document would otherwise reopen all of the above.
    It is a blocklist, not an allowlist, on purpose: a wrong allowlist would
    silently break real Excalidraw exports.

    Parses as real XML via the stdlib, not regex, so a disguised
    '</script >' or mixed-case tag cannot slip past a string match. Two
    read-only passes collect what to remove before any mutation. Anything
    that is not well-formed XML raises InvalidSVG.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise InvalidSVG(f"not well-formed SVG/XML: {exc}") from None

    _STRIP_TAGS = {"script", "foreignObject", "object", "embed", "iframe",
                   "set", "animate", "animateTransform", "animateMotion"}
    to_remove: list[tuple[ET.Element, ET.Element]] = []
    for parent in root.iter():
        for child in list(parent):
            if child.tag.rsplit("}", 1)[-1] in _STRIP_TAGS:
                to_remove.append((parent, child))
    for parent, child in to_remove:
        parent.remove(child)

    for el in root.iter():
        is_use = el.tag.rsplit("}", 1)[-1] == "use"
        for attr, value in list(el.attrib.items()):
            local = attr.rsplit("}", 1)[-1]
            target = _url_scheme_text(value)
            if (local.lower().startswith("on")
                    or target.startswith(_FORBIDDEN_SCHEMES)
                    and not target.startswith("data:image/")
                    or is_use and local == "href" and not target.startswith("#")):
                del el.attrib[attr]

    return ET.tostring(root, encoding="unicode")


_FORBIDDEN_SCHEMES = ("javascript:", "vbscript:", "data:")
_URL_IGNORED = re.compile(r"[\x00-\x20\x7f-\x9f]+")


def _url_scheme_text(value: str) -> str:
    """An attribute value as a scheme check must see it: entities are
    already decoded by the XML parser; whitespace and control characters
    (which browsers strip or ignore in URLs) removed everywhere, lowercased."""
    return _URL_IGNORED.sub("", value).lower()


def _legacy_records(state_dir: Path) -> Iterator[tuple]:
    """The canvases a release before 0.2.18 stored, as rows: one file
    <32 hex>.json per canvas in state_dir (the names/ index only repeated the
    files' owner and name). Raises StoreRefused naming the first file that is
    not a complete canvas record, or two files claiming one owner's name, so
    nothing is imported half."""
    named: dict[tuple[str, str], str] = {}
    for path in sorted(state_dir.glob("*.json")):
        canvas_id = path.stem
        if not re.fullmatch(r"[0-9a-f]{32}", canvas_id):
            continue  # not a canvas file (a service config, for example)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreRefused(f"legacy canvas file {path.name} cannot be read: {exc}") from None
        problem = None
        if not isinstance(data, dict) or not isinstance(data.get("token_hash"), str):
            problem = "no token_hash"
        elif not isinstance(data.get("created_s"), (int, float)) or isinstance(data["created_s"], bool):
            problem = "no numeric created_s"
        elif not all(data.get(k) is None or isinstance(data[k], str) for k in ("scene_svg", "elements_json")):
            problem = "scene_svg/elements_json not text"
        elif (data.get("owner") is None) != (data.get("name") is None) or (
                data.get("owner") is not None and not (
                    isinstance(data["owner"], str) and isinstance(data["name"], str)
                    and _NAME_RE.fullmatch(data["name"]))):
            problem = "owner and name not both set and valid"
        if problem:
            raise StoreRefused(f"legacy canvas file {path.name} is not a canvas record: {problem}")
        if data.get("owner") is not None:
            key = (data["owner"], data["name"])
            if key in named:
                raise StoreRefused(f"legacy canvas files {named[key]} and {path.name} both name "
                                   f"{data['owner']}/{data['name']}; move one aside, then start again")
            named[key] = path.name
        yield (canvas_id, data["token_hash"], data.get("scene_svg"), data.get("elements_json"),
               data["created_s"], path.stat().st_mtime, data.get("owner"), data.get("name"))


def serialized(method):
    """The store's thread lock: every public method holds it for its whole
    call (one SQLite connection, shared by the server's threads)."""
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class CanvasStore:
    """Canvases in state_dir/canvas.sqlite."""

    def __init__(self, state_dir: Path, max_age_days: float = DEFAULT_MAX_AGE_DAYS,
                 create_rate_limit: int = DEFAULT_CREATE_RATE_LIMIT,
                 max_canvases: int = DEFAULT_MAX_CANVASES) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.max_age_days = max_age_days
        self.create_rate_limit = create_rate_limit
        self.max_canvases = max_canvases
        # In memory only, deliberately: the per-canvas edit log, its sequence
        # numbers and the element versions already seen (what is new in
        # write_elements), and the create-rate window. A restart resets them.
        self._edit_log: dict[str, list[dict]] = {}
        self._edit_seq: dict[str, int] = {}
        # Element id -> version (the pushed drawing: its geometry), for the
        # elements of the last scene stored per canvas only.
        self._known_versions: dict[str, dict[str, object]] = {}
        self._recent_creates: list[float] = []
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.state_dir / DB_FILE, check_same_thread=False,
                                  isolation_level=None, timeout=BUSY_TIMEOUT_S)
        try:
            self.db.execute("PRAGMA journal_mode = WAL")
            self.db.execute("PRAGMA synchronous = FULL")  # a saved stroke survives a power cut
            with self._write():
                self._open_schema()
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def _open_schema(self) -> None:
        """Create the schema and import the legacy files on first open (one
        transaction: two processes opening a fresh state directory import
        once); refuse a database this release does not know."""
        has_meta = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        if has_meta:
            schema = self.db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if schema is None or schema[0] != SCHEMA_VERSION:
                raise StoreRefused(f"canvas database schema {schema[0] if schema else None}; this "
                                   f"release reads schema {SCHEMA_VERSION}; database left unchanged")
            return
        for statement in SCHEMA.split(";"):
            if statement.strip():
                self.db.execute(statement)
        rows = list(_legacy_records(self.state_dir))
        self.db.executemany(f"INSERT INTO canvases ({_COLUMNS}, modified_s) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            [row[:5] + row[6:] + (row[5],) for row in rows])
        self.db.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("schema", SCHEMA_VERSION), ("created", str(time.time())), ("imported", str(len(rows)))])
        if rows:
            log.warning("imported %d canvases from the legacy files in %s; the files are left "
                        "as they were (a rollback reads them)", len(rows), self.state_dir)

    @contextmanager
    def _write(self):
        """One IMMEDIATE transaction: it takes SQLite's write lock up front, so
        this process's threads (the RLock) and the other process (the busy
        timeout) wait instead of interleaving read-modify-writes."""
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def _row(self, where: str, *args: object) -> Canvas:
        row = self.db.execute(f"SELECT {_COLUMNS} FROM canvases WHERE {where}", args).fetchone()
        if row is None:
            raise CanvasNotFound(str(args[-1]))
        return _canvas(row)

    def _forget(self, canvas_id: str) -> None:
        self._edit_log.pop(canvas_id, None)
        self._edit_seq.pop(canvas_id, None)
        self._known_versions.pop(canvas_id, None)

    def _count(self) -> int:
        return self.db.execute("SELECT count(*) FROM canvases").fetchone()[0]

    @serialized
    def limits_status(self) -> dict:
        """Configured limits and current standing, reported to whoever asks."""
        now = time.time()
        recent = [t for t in self._recent_creates if now - t < RATE_LIMIT_WINDOW_S]
        return {
            "max_age_days": self.max_age_days,
            "create_rate_limit": self.create_rate_limit,
            "create_rate_window_s": RATE_LIMIT_WINDOW_S,
            "creates_in_current_window": len(recent),
            "max_canvases": self.max_canvases,
            "current_canvas_count": self._count(),
        }

    def create_named(self, owner: str, name: str) -> tuple[str, str]:
        """Create a canvas owned by `owner` (a Pond, the verified MCP caller)
        under `name`, unique per owner. Returns (canvas_id, plaintext_token);
        raises ValueError (bad owner or name), NameTaken, RateLimited or
        TooManyCanvases. The only way a canvas is created.

        The plaintext token is returned exactly once; only its hash is stored.

        Sweeps expired canvases first, in the same transaction: creating is
        what happens whenever the tool is used at all, so a forgotten canvas
        is caught the next time anything is created (no timer, no cron). The
        sweep commits even when the create is then refused; the limits are
        checked after it, so an expired canvas does not count against the
        ceiling."""
        if not isinstance(owner, str) or not owner:
            raise ValueError("owner must be a non-empty string")
        if not _NAME_RE.fullmatch(name):
            raise ValueError(f"name does not match {NAME_PATTERN}")
        self.sweep_expired(self.max_age_days * 86400)
        with self._write():
            if self.db.execute("SELECT 1 FROM canvases WHERE owner = ? AND name = ?",
                               (owner, name)).fetchone():
                raise NameTaken(name)
            recent = self._recent_creates
            if self.create_rate_limit:
                now = time.time()
                recent[:] = [t for t in recent if now - t < RATE_LIMIT_WINDOW_S]
                if len(recent) >= self.create_rate_limit:
                    raise RateLimited(f"{self.create_rate_limit} canvases already created "
                                      f"in the last {RATE_LIMIT_WINDOW_S}s")
            if self.max_canvases and self._count() >= self.max_canvases:
                raise TooManyCanvases(f"{self.max_canvases} canvases already exist")
            canvas_id = uuid.uuid4().hex
            token = secrets.token_urlsafe(32)
            now = time.time()
            self.db.execute(f"INSERT INTO canvases ({_COLUMNS}, modified_s) "
                            "VALUES (?, ?, NULL, NULL, ?, ?, ?, ?)",
                            (canvas_id, _hash_token(token), now, owner, name, now))
            if self.create_rate_limit:
                recent.append(now)
        return canvas_id, token

    @serialized
    def read(self, canvas_id: str) -> Canvas:
        return self._row("id = ?", canvas_id)

    @serialized
    def find_named(self, owner: str, name: str) -> Canvas:
        """The canvas `owner` created under `name`; another owner's canvas of
        the same name is not found."""
        return self._row("owner = ? AND name = ?", owner, name)

    @serialized
    def list_named(self, owner: str) -> list[dict]:
        """The owner's canvases by name: {name, canvas_id, created_s}."""
        return [{"name": name, "canvas_id": canvas_id, "created_s": created_s}
                for name, canvas_id, created_s in self.db.execute(
                    "SELECT name, id, created_s FROM canvases WHERE owner = ? ORDER BY name",
                    (owner,))]

    def verify_token(self, canvas_id: str, token: str) -> bool:
        canvas = self.read(canvas_id)  # raises CanvasNotFound if absent
        return secrets.compare_digest(canvas.token_hash, _hash_token(token))

    def write_named_scene(self, owner: str, name: str, scene_svg: str) -> None:
        """Replace the pushed drawing of the owner's canvas `name` (the Pond is
        proven by the control plane). Raises CanvasNotFound, InvalidSVG."""
        clean = _sanitize_svg(scene_svg)  # raises InvalidSVG; never stores unsanitized
        with self._write():
            if not self.db.execute("UPDATE canvases SET scene_svg = ?, modified_s = ? "
                                   "WHERE owner = ? AND name = ?",
                                   (clean, time.time(), owner, name)).rowcount:
                raise CanvasNotFound(name)

    def write_elements(self, canvas_id: str, token: str, elements_json: str) -> None:
        """Store the browser's current Excalidraw scene, opaquely: only the
        browser's Excalidraw needs to understand it. Validates only that it
        is a JSON list, so a caller's mistake is caught here rather than
        stored and discovered later."""
        def finite_only(constant: str) -> object:
            raise InvalidElements(f"not valid JSON: {constant} is not a number")
        try:
            parsed = json.loads(elements_json, parse_constant=finite_only)
        except json.JSONDecodeError as exc:
            raise InvalidElements(f"not valid JSON: {exc}") from None
        if not isinstance(parsed, list):
            raise InvalidElements("elements must be a JSON list")
        with self._write():
            try:
                canvas = self._row("id = ?", canvas_id)
            except CanvasNotFound:
                self._forget(canvas_id)  # deleted, possibly by the admin in another process
                raise
            if not secrets.compare_digest(canvas.token_hash, _hash_token(token)):
                raise InvalidToken(canvas_id)
            self.db.execute("UPDATE canvases SET elements_json = ?, modified_s = ? WHERE id = ?",
                            (elements_json, time.time(), canvas_id))
            self._record_edits(canvas_id, parsed)

    def _record_edits(self, canvas_id: str, elements: list) -> None:
        """Log elements that are new or whose Excalidraw-assigned `version`
        increased since the last write seen (Excalidraw already versions each
        element; redoing that would own it twice). Skips anything without id
        or version. The pushed drawing is re-inserted by every page load with
        a fresh version, so for that one element an edit is a change of its
        geometry or deletion. The markers kept are those of this scene; the
        log keeps the newest EDIT_LOG_ENTRIES entries."""
        known: dict[str, object] = self._known_versions.setdefault(canvas_id, {})
        present: set[str] = set()
        entries = self._edit_log.setdefault(canvas_id, [])
        received_at = time.time()
        for el in elements:
            if not isinstance(el, dict):
                continue
            el_id, version = el.get("id"), el.get("version")
            if not isinstance(el_id, str) or not el_id or version is None:
                continue
            present.add(el_id)
            pushed = el_id == PUSHED_SCENE_ELEMENT_ID
            marker: object = (tuple(el.get(key) for key in _PUSHED_GEOMETRY) if pushed
                              else version)
            if known.get(el_id) != marker:
                known[el_id] = marker
                seq = self._edit_seq[canvas_id] = self._edit_seq.get(canvas_id, 0) + 1
                entry = {
                    "seq": seq,  # this process's order, 1 upward; get_edits pages by it
                    "element_id": el_id,
                    "type": el.get("type"),
                    "version": version,
                    "is_deleted": bool(el.get("isDeleted", False)),
                    "client_updated_ms": el.get("updated"),  # Excalidraw's own clock
                    "server_received_s": received_at,  # our clock; the two can disagree
                }
                if pushed:
                    entry["pushed_scene"] = True  # the agent's drawing, moved/resized/deleted
                entries.append(entry)
        for gone in known.keys() - present:
            del known[gone]
        if len(entries) > EDIT_LOG_ENTRIES:
            del entries[:-EDIT_LOG_ENTRIES]

    @serialized
    def get_edits(self, canvas_id: str) -> list[dict]:
        """Edits recorded since this CanvasStore was constructed, not since the
        canvas was created."""
        return list(self._edit_log.get(canvas_id, []))

    def scene_version(self, canvas_id: str) -> str:
        """Change-detection token for polling, derived from scene_svg on every
        read. Excludes elements_json: a browser saving its own drawing must
        not see that as a change and reload itself mid-stroke."""
        return scene_version_of(self.read(canvas_id).scene_svg)

    @serialized
    def list_canvases(self) -> list[dict]:
        """Every canvas, for `boring-canvas-admin list` inside the container
        (never HTTP or MCP); never a token or its hash."""
        results = []
        for canvas_id, has_scene, has_elements, modified_s, owner, name in self.db.execute(
                "SELECT id, scene_svg IS NOT NULL, elements_json IS NOT NULL, modified_s, "
                "owner, name FROM canvases ORDER BY id"):
            entry = {"canvas_id": canvas_id, "has_scene": bool(has_scene),
                     "has_elements": bool(has_elements), "modified_s": modified_s}
            if owner is not None:
                entry["owner"], entry["name"] = owner, name
            results.append(entry)
        return results

    def delete(self, canvas_id: str) -> None:
        """Remove a canvas (admin; no token). An unknown id raises
        CanvasNotFound, so a mistyped delete is caught."""
        with self._write():
            if not self.db.execute("DELETE FROM canvases WHERE id = ?", (canvas_id,)).rowcount:
                raise CanvasNotFound(canvas_id)
            self._forget(canvas_id)

    def delete_named(self, owner: str, name: str) -> None:
        """delete for MCP: the owner's canvas by name."""
        with self._write():
            canvas = self._row("owner = ? AND name = ?", owner, name)
            self.db.execute("DELETE FROM canvases WHERE id = ?", (canvas.canvas_id,))
            self._forget(canvas.canvas_id)

    def reissue_named_token(self, owner: str, name: str) -> tuple[str, str]:
        """A fresh browser token for the owner's canvas `name`; the old one
        stops working (open browsers are signed out). Returns (canvas_id,
        token). Raises CanvasNotFound."""
        new_token = secrets.token_urlsafe(32)
        with self._write():
            canvas = self._row("owner = ? AND name = ?", owner, name)
            self.db.execute("UPDATE canvases SET token_hash = ?, modified_s = ? WHERE id = ?",
                            (_hash_token(new_token), time.time(), canvas.canvas_id))
        return canvas.canvas_id, new_token

    def sweep_expired(self, max_age_s: float) -> list[str]:
        """Delete every canvas older than max_age_s (from creation); the ids
        removed. create_named runs it; reads stay pure."""
        with self._write():
            return self._sweep(max_age_s)

    def _sweep(self, max_age_s: float) -> list[str]:
        cutoff = time.time() - max_age_s
        expired = [row[0] for row in self.db.execute(
            "SELECT id FROM canvases WHERE created_s < ? ORDER BY id", (cutoff,))]
        self.db.execute("DELETE FROM canvases WHERE created_s < ?", (cutoff,))
        for canvas_id in expired:
            self._forget(canvas_id)
        return expired
