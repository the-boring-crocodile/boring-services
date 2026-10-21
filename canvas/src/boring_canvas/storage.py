"""Canvas state: one JSON file per canvas, atomic writes, hashed tokens.

The choices: HTTP not Pirate-IPC, one file per canvas not a database, token
stored as a hash and never round-tripped back out.
"""
from __future__ import annotations

import hashlib
import functools
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from boring_service_kit.filelock import file_lock

log = logging.getLogger("boring_canvas.storage")

_SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", _SVG_NS)

# 42 days: good hygiene in principle, not a response to a specific
# incident -- an absolute TTL from creation,
# not a sliding idle-window, because canvases here get revisited across
# multi-day working sessions (the architecture-diagram canvas, still
# actively drawn on a day after it was made) -- a short idle timeout
# would punish exactly that normal use. Configurable per CanvasStore
# instance (below) and via server.py's --max-age-days -- this is only
# the fallback when nothing else specifies a value.
DEFAULT_MAX_AGE_DAYS = 42

# A chat can create canvases, so creation is limited. Two different failure
# modes, two different limits:
# RATE_LIMIT catches a tight-loop burst (a bug, a retry-without-backoff,
# a misunderstanding); MAX_CANVASES catches slow accumulation even when
# the rate limit is respected. Both configurable, 0 means unlimited on
# either.
DEFAULT_CREATE_RATE_LIMIT = 10  # per RATE_LIMIT_WINDOW_S; 0 = unlimited
RATE_LIMIT_WINDOW_S = 60
DEFAULT_MAX_CANVASES = 100  # total existing at once; 0 = unlimited

# Named canvases: a canvas created over MCP belongs
# to the Pond that created it (`owner`, the introspected token's `sub`) and
# has a `name` unique per owner. The pattern makes a name a safe file name,
# so it is used as one in the name index (see _bind_name).
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
_NAME_RE = re.compile(NAME_PATTERN)


# The id canvas.html gives the pushed drawing (PUSHED_IMAGE_ID there; a test
# keeps the two equal) and the fields that say a person moved it.
PUSHED_SCENE_ELEMENT_ID = "boring-canvas-pushed-scene"
_PUSHED_GEOMETRY = ("x", "y", "width", "height", "angle", "isDeleted")


class CanvasNotFound(Exception):
    """No canvas exists at this id, or the id was not one we ever issued."""


class CorruptCanvas(Exception):
    """A canvas file exists but cannot be used: unreadable, not JSON, or not
    a canvas record. A failure to report, never a canvas to delete."""


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


class NameIndexBroken(Exception):
    """The name index disagrees with the canvas files (a name bound to a
    canvas that is gone, or to one with another owner/name). A failure to
    report, never repaired here: names/ was edited or a file removed by hand."""


@dataclass(frozen=True)
class Canvas:
    canvas_id: str
    token_hash: str
    scene_svg: str | None
    elements_json: str | None
    created_s: float | None  # None only in a malformed file; the sweep reports it
    owner: str | None = None  # Pond (token sub) of an MCP-created canvas, else None
    name: str | None = None  # set exactly when owner is


def _record(canvas: Canvas, **changes: object) -> dict:
    """The file content of `canvas` with `changes` applied: every write
    keeps the fields it is not about, owner and name included."""
    data: dict = {
        "token_hash": canvas.token_hash,
        "scene_svg": canvas.scene_svg,
        "elements_json": canvas.elements_json,
        "created_s": canvas.created_s,
    }
    if canvas.owner is not None:
        data["owner"], data["name"] = canvas.owner, canvas.name
    data.update(changes)
    return data


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


def _is_safe_id(canvas_id: str) -> bool:
    # canvas_id always comes from uuid.uuid4().hex, which we generate
    # ourselves in _create(). Enforcing that exact shape here means a
    # caller-supplied id can never become a path outside state_dir, even
    # though the only caller today is our own route matching (which already
    # constrains it via the ^/canvas/([0-9a-f]{32})$ regex) -- defense in
    # depth costs nothing here and matches how the Crocodile's file tools
    # treats any path derived from caller input.
    return len(canvas_id) == 32 and all(c in "0123456789abcdef" for c in canvas_id)


def _load(path: Path) -> dict:
    """Read one canvas file. FileNotFoundError passes through; anything
    else that makes the file unusable raises CorruptCanvas with the reason."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise CorruptCanvas(f"unreadable: {exc}") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CorruptCanvas(f"not valid JSON: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("token_hash"), str):
        raise CorruptCanvas("not a canvas record (no token_hash)")
    return data


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically and durably: temp file next to the target,
    flushed and fsynced, then renamed over it; on POSIX the directory is
    fsynced too, so the new name survives a crash.

    os.replace() is atomic for the final swap, so a reader never sees a
    half-written file. On any error the temp file is removed and the error
    propagates; the target keeps its previous content unless the rename
    already happened. This function takes no lock itself: every caller
    in CanvasStore holds the store's records lock (CanvasStore._records),
    which serializes all record writes, creates, deletes and the expiry
    sweep across threads and across processes on the same state directory
    (the server and `boring-canvas-admin`).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        with open(tmp, "x", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


EDIT_LOG_ENTRIES = 500  # per canvas, newest kept (get_edits)
RECORDS_LOCK_FILE = ".records.lock"  # in state_dir; never removed (waiters share its inode)


def _with_records_lock(method):
    """Run a CanvasStore record write under the store's records lock."""
    @functools.wraps(method)
    def locked(self, *args, **kwargs):
        with self._records():
            return method(self, *args, **kwargs)
    return locked


class CanvasStore:
    """One JSON file per canvas under state_dir."""

    def __init__(self, state_dir: Path, max_age_days: float = DEFAULT_MAX_AGE_DAYS,
                 create_rate_limit: int = DEFAULT_CREATE_RATE_LIMIT,
                 max_canvases: int = DEFAULT_MAX_CANVASES) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.max_age_days = max_age_days
        self.create_rate_limit = create_rate_limit
        self.max_canvases = max_canvases
        # In-memory only, deliberately: per-canvas edit log and the element
        # versions already seen, used to detect what's new in write_elements.
        # Does not need to survive a restart yet; resets
        # to empty each time a fresh CanvasStore is constructed, i.e. every
        # server restart. If that stops being acceptable, this is the one
        # place to add persistence -- not scattered elsewhere.
        self._edit_log: dict[str, list[dict]] = {}
        # Element id -> its version (the pushed drawing: its geometry), for
        # the elements of the last scene stored per canvas only: bounded by
        # that scene (itself bounded by the body cap), not by every id ever
        # seen. The edit log is bounded separately (EDIT_LOG_ENTRIES).
        self._known_versions: dict[str, dict[str, object]] = {}
        # Same in-memory-only reasoning applies to the rate-limit window --
        # a restart resetting it is a feature here, not a gap: it means a
        # burst right after a restart gets the same fresh allowance as any
        # other window, not a stale grudge carried across process lifetimes.
        # Every create is owned (MCP, a verified caller): one window.
        self._recent_creates: list[float] = []
        # Canvas files found unusable by _scan(), id -> reason. Only so each
        # one is logged once per process; what is reported comes from a
        # fresh scan, never from this.
        self._corrupt: dict[str, str] = {}
        # Serializes check-then-unlink of a name binding (_unbind) within
        # this process, so two deleters of one canvas cannot remove a
        # binding someone made for a new canvas in between. Creating a
        # binding needs no lock: os.link is atomic (see _bind_name). Taken
        # only inside _unbind, never while waiting for _records_lock, so
        # the order is always records lock, then names lock.
        self._names_lock = threading.Lock()
        # Serializes every read-modify-write of a canvas record, and create,
        # delete and the expiry sweep. The web server and the MCP service
        # run in threads of one process; without it a browser save and an
        # agent's push (or a token change) racing on the same record would
        # silently undo one of them, and concurrent creates could pass the
        # rate and count checks together and exceed the limits. Reentrant:
        # create runs the sweep, and the sweep runs delete. Held together
        # with the file lock (_records), which extends it to other processes.
        self._records_lock = threading.RLock()
        self._records_depth = 0  # nesting of _records in the thread holding _records_lock

    @contextmanager
    def _records(self):
        """The records lock: this process's threads (_records_lock) and every
        process on state_dir (an exclusive lock on state_dir/.records.lock,
        the kit's file_lock: flock, or msvcrt on Windows). `boring-canvas-admin`
        runs beside the server; without the file lock a write that read a
        record before an admin delete or token reissue would publish the old
        record afterwards, undoing it. The file lock is taken once, by the
        outermost holder: a second lock of the file from the same process
        would wait for itself."""
        with self._records_lock:
            if self._records_depth:
                self._records_depth += 1
                try:
                    yield
                finally:
                    self._records_depth -= 1
                return
            with file_lock(self.state_dir / RECORDS_LOCK_FILE):
                self._records_depth = 1
                try:
                    yield
                finally:
                    self._records_depth = 0

    # ---- name index: state_dir/names/<sha256(owner)>/<name>, content canvas_id

    def _owner_dir(self, owner: str) -> Path:
        # The owner is the control plane's `sub`, whose format is not ours;
        # its digest is a safe directory name whatever it contains.
        return self.state_dir / "names" / hashlib.sha256(owner.encode("utf-8")).hexdigest()

    def _marker(self, owner: str, name: str) -> Path:
        # ".name-" keeps every name a portable file name (Windows reserves con, nul, ...).
        return self._owner_dir(owner) / (".name-" + name)

    def _bind_name(self, owner: str, name: str, canvas_id: str) -> None:
        """Bind name -> canvas_id for owner, or raise NameTaken. The marker
        is written to a temp file and hard-linked into place: link() fails
        atomically if the name exists (also across processes), and a reader
        never sees a marker without its content."""
        marker = self._marker(owner, name)
        marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_name(f".{name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
        tmp.write_text(canvas_id, encoding="ascii")
        try:
            os.link(tmp, marker)
        except FileExistsError:
            raise NameTaken(name) from None
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _marker_target(marker: Path) -> str | None:
        try:
            return marker.read_text(encoding="ascii")
        except FileNotFoundError:
            return None

    def _unbind(self, marker: Path, canvas_id: str) -> None:
        """Remove the binding at marker if (and only if) it is canvas_id's."""
        with self._names_lock:
            if self._marker_target(marker) == canvas_id:
                marker.unlink(missing_ok=True)

    def _unbind_everywhere(self, canvas_id: str) -> None:
        """For a canvas whose file cannot tell its owner/name (corrupt):
        remove any binding to it."""
        for marker in self.state_dir.glob("names/*/*"):
            if marker.name.startswith(".name-") and _NAME_RE.fullmatch(marker.name[6:]):
                self._unbind(marker, canvas_id)

    def _path(self, canvas_id: str) -> Path:
        if not _is_safe_id(canvas_id):
            raise CanvasNotFound(canvas_id)
        return self.state_dir / f"{canvas_id}.json"

    def _canvas_count(self) -> int:
        """Cheap count -- filenames only, no content read -- reused by both
        the max_canvases check and limits_status()'s reporting, so the two
        can never disagree about what "current count" means."""
        return sum(1 for p in self.state_dir.glob("*.json") if _is_safe_id(p.stem))

    def _scan(self) -> Iterator[tuple[str, Path, dict | None, str | None]]:
        """Yield (canvas_id, path, data, problem) for every canvas file.
        A file that cannot be used comes with data None and the reason in
        `problem` -- reported (logged once per file per process), never
        skipped and never deleted. So does a record without created_s:
        it could never expire, which an operator should see."""
        for path in sorted(self.state_dir.glob("*.json")):
            canvas_id = path.stem
            if not _is_safe_id(canvas_id):
                continue  # a stray/foreign file; not one of ours, skip it
            try:
                data = _load(path)
                created_s = data.get("created_s")
                if not isinstance(created_s, (int, float)) or isinstance(created_s, bool):
                    raise CorruptCanvas("no numeric created_s, so it never expires")
            except FileNotFoundError:
                continue  # deleted between glob and read
            except CorruptCanvas as exc:
                if canvas_id not in self._corrupt:
                    log.warning("canvas %s is corrupt and left in place: %s", canvas_id, exc)
                self._corrupt[canvas_id] = str(exc)
                yield canvas_id, path, None, str(exc)
                continue
            self._corrupt.pop(canvas_id, None)
            yield canvas_id, path, data, None

    def limits_status(self) -> dict:
        """Current configured limits plus current standing against them --
        meant to be reported to whoever's calling (MCP included, once that
        exists), not just silently enforced: set by the admin, reported
        over MCP. A caller that can see it's
        close to a limit can behave sensibly instead of just hitting a
        wall and guessing why."""
        now = time.time()
        recent = [t for t in self._recent_creates if now - t < RATE_LIMIT_WINDOW_S]
        return {
            "max_age_days": self.max_age_days,
            "create_rate_limit": self.create_rate_limit,
            "create_rate_window_s": RATE_LIMIT_WINDOW_S,
            "creates_in_current_window": len(recent),
            "max_canvases": self.max_canvases,
            "current_canvas_count": self._canvas_count(),
            # Counted in current_canvas_count, never swept: an operator
            # inspects and deletes them (`boring-canvas-admin delete <id>`).
            "corrupt_canvases": {cid: problem for cid, _, _, problem in self._scan() if problem},
        }

    def create_named(self, owner: str, name: str) -> tuple[str, str]:
        """Create a canvas owned by `owner` (a Pond, the verified MCP caller)
        under `name`, unique per owner. Returns (canvas_id, plaintext_token);
        raises ValueError (bad owner or name), NameTaken, RateLimited or
        TooManyCanvases. The only way a canvas is created (canvases are
        created over MCP only).

        The plaintext token is returned exactly once, here, and never
        again -- only its hash is ever stored or re-read.

        Sweeps expired canvases first, as a side effect:
        "common usage triggered" cleanup, no separate
        scheduling mechanism (no timer, no cron, no standalone sweep
        command to remember to run). Creating is the one operation that
        happens whenever this tool is actually being used, which makes it
        a reasonable proxy for "periodically enough in practice" without
        adding new infrastructure -- and critically, the trigger is "the
        tool got used at all," not "someone happened to revisit this
        specific old canvas," so a forgotten canvas still gets caught the
        next time anything is created, not only if it's individually
        visited again. (A systemd timer was the original plan;
        deliberately dropped for this simpler shape.)

        Raises RateLimited or TooManyCanvases before anything is created
        if either configured cap (0 = unlimited) is hit -- checked after
        the sweep, so a canvas that should already have expired doesn't
        wrongly count against the ceiling.

        Order, chosen so a crash leaves nothing that blocks a name: the
        canvas file is written first, then the name is bound (_bind_name,
        atomic). If the bind loses a race, the file is removed and NameTaken
        raised. A crash between the two leaves an unbound file, invisible by
        name, that expires like any other."""
        if not isinstance(owner, str) or not owner:
            raise ValueError("owner must be a non-empty string")
        if not _NAME_RE.fullmatch(name):
            raise ValueError(f"name does not match {NAME_PATTERN}")
        return self._create(owner, name)

    @_with_records_lock
    def _create(self, owner: str, name: str) -> tuple[str, str]:
        self.sweep_expired(self.max_age_days * 86400)

        # Early answer; the authoritative check is the bind below. A binding
        # to a canvas that is gone is a broken index (find_named raises
        # NameIndexBroken), reported as such, not as "name taken".
        if self._marker(owner, name).exists():
            try:
                self.find_named(owner, name)
            except CanvasNotFound:
                pass  # unbound meanwhile; the bind below decides
            except CorruptCanvas:
                raise NameTaken(name) from None  # it exists, it is just unreadable
            else:
                raise NameTaken(name)

        recent = self._recent_creates
        if self.create_rate_limit:
            now = time.time()
            recent[:] = [t for t in recent if now - t < RATE_LIMIT_WINDOW_S]
            if len(recent) >= self.create_rate_limit:
                raise RateLimited(
                    f"{self.create_rate_limit} canvases "
                    f"already created in the last {RATE_LIMIT_WINDOW_S}s")

        if self.max_canvases and self._canvas_count() >= self.max_canvases:
            raise TooManyCanvases(f"{self.max_canvases} canvases already exist")

        canvas_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        path = self._path(canvas_id)
        _atomic_write_json(path, _record(
            Canvas(canvas_id, _hash_token(token), None, None, time.time(), owner, name)))
        try:
            self._bind_name(owner, name, canvas_id)
        except NameTaken:
            path.unlink()  # ours, never bound: nobody can have seen it by name
            raise
        if self.create_rate_limit:
            recent.append(time.time())
        return canvas_id, token

    def read(self, canvas_id: str) -> Canvas:
        path = self._path(canvas_id)
        try:
            data = _load(path)  # raises CorruptCanvas: a failure, not "not found"
        except FileNotFoundError:
            raise CanvasNotFound(canvas_id) from None
        return Canvas(
            canvas_id=canvas_id,
            token_hash=data["token_hash"],
            scene_svg=data.get("scene_svg"),
            elements_json=data.get("elements_json"),
            created_s=data.get("created_s"),
            owner=data.get("owner") if isinstance(data.get("owner"), str) else None,
            name=data.get("name") if isinstance(data.get("name"), str) else None,
        )

    def find_named(self, owner: str, name: str) -> Canvas:
        """The canvas `owner` created under `name`. Raises CanvasNotFound
        when this owner has no such name (another owner's canvas of the
        same name is not found either), NameIndexBroken when the index
        and the files disagree."""
        if not _NAME_RE.fullmatch(name):
            raise CanvasNotFound(name)
        marker = self._marker(owner, name)
        canvas_id = self._marker_target(marker)
        if canvas_id is None:
            raise CanvasNotFound(name)
        if not _is_safe_id(canvas_id):
            raise self._broken(f"name {name!r} is bound to {canvas_id!r}, not a canvas id")
        try:
            canvas = self.read(canvas_id)
        except CanvasNotFound:
            if self._marker_target(marker) == canvas_id:  # still bound: file gone under it
                raise self._broken(f"name {name!r} is bound to canvas {canvas_id}, "
                                   "which does not exist") from None
            raise CanvasNotFound(name) from None  # deleted meanwhile
        if canvas.owner != owner or canvas.name != name:
            raise self._broken(f"name {name!r} is bound to canvas {canvas_id}, "
                               "whose file names another owner or name")
        return canvas

    @staticmethod
    def _broken(message: str) -> NameIndexBroken:
        log.error("name index: %s", message)
        return NameIndexBroken(message)

    def list_named(self, owner: str) -> list[dict]:
        """The owner's named canvases, by name: {name, canvas_id, created_s},
        or {name, problem} for one that cannot be used (reported, not hidden).
        Bounded by max_canvases: every binding has its own canvas file."""
        try:
            names = sorted({p.name[6:] if p.name.startswith(".name-") else p.name
                            for p in self._owner_dir(owner).iterdir()
                            if _NAME_RE.fullmatch(p.name[6:] if p.name.startswith(".name-") else p.name)})
        except FileNotFoundError:
            return []
        results = []
        for name in names:
            try:
                canvas = self.find_named(owner, name)
            except CanvasNotFound:
                continue  # deleted between listing and reading
            except (NameIndexBroken, CorruptCanvas) as exc:
                results.append({"name": name, "problem": str(exc)})
                continue
            created_s = canvas.created_s
            if not isinstance(created_s, (int, float)) or isinstance(created_s, bool):
                results.append({"name": name, "problem": "no numeric created_s"})
                continue
            results.append({"name": name, "canvas_id": canvas.canvas_id,
                            "created_s": created_s})
        return results

    def verify_token(self, canvas_id: str, token: str) -> bool:
        canvas = self.read(canvas_id)  # raises CanvasNotFound if absent
        return secrets.compare_digest(canvas.token_hash, _hash_token(token))

    @_with_records_lock
    def write_scene(self, canvas_id: str, token: str, scene_svg: str) -> None:
        canvas = self.read(canvas_id)
        if not secrets.compare_digest(canvas.token_hash, _hash_token(token)):
            raise InvalidToken(canvas_id)
        self._store_scene(canvas, scene_svg)

    @_with_records_lock
    def write_named_scene(self, owner: str, name: str, scene_svg: str) -> None:
        """write_scene for MCP: the owner's canvas by name, no canvas token
        (the Pond is proven by the control plane). Raises CanvasNotFound,
        NameIndexBroken, InvalidSVG."""
        self._store_scene(self.find_named(owner, name), scene_svg)

    def _store_scene(self, canvas: Canvas, scene_svg: str) -> None:
        clean = _sanitize_svg(scene_svg)  # raises InvalidSVG; never stores unsanitized
        # elements_json, owner and name preserved: this write isn't about them
        _atomic_write_json(self._path(canvas.canvas_id), _record(canvas, scene_svg=clean))

    @_with_records_lock
    def write_elements(self, canvas_id: str, token: str, elements_json: str) -> None:
        """Store the browser's current Excalidraw scene, opaquely.

        Deliberately does not parse or understand Excalidraw's element
        schema -- only the browser's own Excalidraw component needs to,
        and guessing at that schema server-side was already rejected once
        (a pushed SVG is a background image). Validates
        only that it is JSON at all, so a caller's mistake is caught here
        rather than stored as an unreadable string and discovered later.
        """
        try:
            canvas = self.read(canvas_id)
        except CanvasNotFound:
            # Deleted, possibly by boring-canvas-admin in another process:
            # this process's bookkeeping for it goes too.
            self._edit_log.pop(canvas_id, None)
            self._known_versions.pop(canvas_id, None)
            raise
        if not secrets.compare_digest(canvas.token_hash, _hash_token(token)):
            raise InvalidToken(canvas_id)
        def finite_only(constant: str) -> object:
            raise InvalidElements(f"not valid JSON: {constant} is not a number")
        try:
            parsed = json.loads(elements_json, parse_constant=finite_only)
        except json.JSONDecodeError as exc:
            raise InvalidElements(f"not valid JSON: {exc}") from None
        if not isinstance(parsed, list):
            raise InvalidElements("elements must be a JSON list")
        self._record_edits(canvas_id, parsed)
        # scene_svg, owner and name preserved: this write isn't about them
        _atomic_write_json(self._path(canvas_id), _record(canvas, elements_json=elements_json))

    def _record_edits(self, canvas_id: str, elements: list[dict]) -> None:
        # The pushed drawing is re-inserted by every page load with a fresh
        # version (canvas.html, insertPushedImage), so its version says
        # nothing about a person's edit. For that one element, an edit is a
        # change of where it is, how big, its rotation or its deletion.
        """Log elements that are new or whose Excalidraw-assigned `version`
        increased since the last write we saw. Uses Excalidraw's own
        per-element versioning rather than diffing arrays ourselves --
        it already tracks this; redoing it would be the same responsibility
        owned twice. Skips anything missing id/version rather than guessing
        (a non-Excalidraw or malformed entry shouldn't corrupt the log).

        The browser posts its whole scene each time, so the markers kept are
        those of this scene: an id it no longer holds is forgotten, and
        logged as new if it ever comes back. The log keeps the newest
        EDIT_LOG_ENTRIES entries; older ones are dropped, never summarized.
        """
        known: dict[str, object] = self._known_versions.setdefault(canvas_id, {})
        present: set[str] = set()
        log = self._edit_log.setdefault(canvas_id, [])
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
                entry = {
                    "element_id": el_id,
                    "type": el.get("type"),
                    "version": version,
                    "is_deleted": bool(el.get("isDeleted", False)),
                    "client_updated_ms": el.get("updated"),  # Excalidraw's own clock
                    "server_received_s": received_at,  # our clock; the two can disagree
                }
                if pushed:
                    entry["pushed_scene"] = True  # the agent's drawing, moved/resized/deleted
                log.append(entry)
        for gone in known.keys() - present:
            del known[gone]
        if len(log) > EDIT_LOG_ENTRIES:  # bounded; in-memory only, this process's lifetime only
            del log[:-EDIT_LOG_ENTRIES]

    def get_edits(self, canvas_id: str) -> list[dict]:
        """Edits recorded since this CanvasStore was constructed -- not since
        the canvas itself was created. Callers needing 404-on-unknown-canvas
        get that from the verify_token/read they do before calling this."""
        return list(self._edit_log.get(canvas_id, []))

    def scene_version(self, canvas_id: str) -> str:
        """Cheap change-detection token for polling, not a stored field --
        derived from scene_svg on every read so there is nothing new to keep
        in sync with it. Deliberately excludes elements_json: a browser
        saving its own drawing must not see that as a change and reload
        itself mid-stroke."""
        canvas = self.read(canvas_id)
        return hashlib.sha256((canvas.scene_svg or "").encode("utf-8")).hexdigest()[:16]

    def list_canvases(self) -> list[dict]:
        """Enumerate every canvas that exists, for admin/cleanup use --
        never returns a token or its hash. This is deliberately the one
        operation in this class that doesn't take a token at all: the
        whole point is "help me see what exists" for someone who may not
        hold every token any more, which is a different kind of caller
        than everything else here. Reached only through
        `boring-canvas-admin list` inside the container (admin.py), i.e. by
        root -- never over HTTP or MCP.

        A corrupt file is listed with its `problem` instead of the content
        flags -- reported, not hidden, and not a reason to fail the list.
        """
        results = []
        for canvas_id, path, data, problem in self._scan():
            if problem:
                results.append({"canvas_id": canvas_id, "problem": problem,
                                "modified_s": path.stat().st_mtime})
                continue
            entry = {
                "canvas_id": canvas_id,
                "has_scene": data.get("scene_svg") is not None,
                "has_elements": data.get("elements_json") is not None,
                "modified_s": path.stat().st_mtime,
            }
            if isinstance(data.get("owner"), str):
                entry["owner"], entry["name"] = data["owner"], data.get("name")
            results.append(entry)
        return results

    @_with_records_lock
    def delete(self, canvas_id: str) -> None:
        """Remove a canvas entirely -- admin operation, no token check (see
        list_canvases' docstring for why that's deliberate here). Raises
        CanvasNotFound rather than silently succeeding on an id that never
        existed, so a typo'd id in a delete command is caught, not
        silently ignored.

        A named canvas's name is unbound first, then the file removed: a
        crash in between leaves an unbound file (swept later), never a
        name bound to nothing.
        """
        path = self._path(canvas_id)
        try:
            data = _load(path)
        except FileNotFoundError:
            raise CanvasNotFound(canvas_id) from None
        except CorruptCanvas:
            self._unbind_everywhere(canvas_id)
        else:
            owner, name = data.get("owner"), data.get("name")
            if isinstance(owner, str) and isinstance(name, str) and _NAME_RE.fullmatch(name):
                self._unbind(self._marker(owner, name), canvas_id)
        try:
            path.unlink()
        except FileNotFoundError:
            raise CanvasNotFound(canvas_id) from None
        self._edit_log.pop(canvas_id, None)
        self._known_versions.pop(canvas_id, None)
        self._corrupt.pop(canvas_id, None)

    @_with_records_lock
    def delete_named(self, owner: str, name: str) -> None:
        """delete for MCP: the owner's canvas by name (the Pond is proven by
        the control plane). Another owner's canvas of the same name is not
        found. Raises CanvasNotFound, NameIndexBroken."""
        self.delete(self.find_named(owner, name).canvas_id)

    @_with_records_lock
    def reissue_named_token(self, owner: str, name: str) -> tuple[str, str]:
        """A fresh browser token for the owner's canvas `name`; the old one
        stops working (open browsers are signed out). For MCP, where the
        owner is proven by the control plane and only a hash of the old
        token is stored. Returns (canvas_id, token). Raises CanvasNotFound,
        NameIndexBroken."""
        canvas = self.find_named(owner, name)
        new_token = secrets.token_urlsafe(32)
        _atomic_write_json(self._path(canvas.canvas_id),
                           _record(canvas, token_hash=_hash_token(new_token)))
        return canvas.canvas_id, new_token

    @_with_records_lock
    def rotate_token(self, canvas_id: str, current_token: str) -> str:
        """Invalidate the current token and issue a fresh one, for a canvas
        whose token you still hold but want to stop using (an old shared
        link, a link that's served its purpose). Unlike list/delete, this
        DOES require the current token -- "prove you have access, get a
        replacement" is a different operation from "show me what exists
        regardless," and conflating the two would make rotation into an
        admin operation it was never meant to be. Content (scene_svg,
        elements_json) is preserved untouched; only the token changes.
        """
        canvas = self.read(canvas_id)  # raises CanvasNotFound
        if not secrets.compare_digest(canvas.token_hash, _hash_token(current_token)):
            raise InvalidToken(canvas_id)
        new_token = secrets.token_urlsafe(32)
        _atomic_write_json(self._path(canvas_id),
                           _record(canvas, token_hash=_hash_token(new_token)))
        return new_token

    @_with_records_lock
    def sweep_expired(self, max_age_s: float) -> list[str]:
        """Delete every canvas older than max_age_s (from creation) and
        return the ids that were removed. Called from create_named() as a
        "common usage triggered" side effect -- not
        from ordinary reads (list_canvases included), which stay pure.
        Deliberately not a check-on-access sweep tied to any one canvas's
        own visits: the trigger is "the tool got used at all," which
        means a forgotten, never-individually-revisited canvas still gets
        caught the next time anything at all is created, not only if
        someone happens to open that specific old link again.

        A corrupt file (see _scan) is never deleted here: its age is
        unknown and its content may still be wanted. It is logged and shown
        by list_canvases/limits_status for an operator to deal with. That
        includes a record without created_s.
        """
        deleted = []
        now = time.time()
        for canvas_id, _, data, problem in self._scan():
            if problem:
                continue  # reported by _scan; never swept
            if now - data["created_s"] > max_age_s:
                try:
                    self.delete(canvas_id)
                except CanvasNotFound:
                    continue  # already gone (race with a manual delete); fine
                deleted.append(canvas_id)
        return deleted
