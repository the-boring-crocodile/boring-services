"""Canvases for tests, made the two ways a canvas exists today.

- new_canvas: created the one way canvases are created (CanvasStore.create_named,
  what MCP's create_canvas calls). Counts toward the create rate limit and
  max_canvases like any MCP create.
- legacy_ownerless_canvas: a canvas as the removed `POST /canvas` made it
  (no owner, no name), as the import keeps one. None are created any more;
  one keeps working until it expires.
"""
from __future__ import annotations

import itertools
import secrets
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from boring_canvas import storage
from boring_canvas.storage import CanvasStore

_names = itertools.count(1)


class ClosesStores(unittest.TestCase):
    """Every store a test opens is closed before its temporary folder is removed.

    Windows cannot remove a folder that holds an open SQLite file. Cleanups run
    last-registered first, so a store opened after its folder closes before it.
    """

    def temp_dir(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def open_store(self, *args, **kwargs) -> CanvasStore:
        store = CanvasStore(*args, **kwargs)
        self.addCleanup(store.close)
        return store


def new_canvas(store: CanvasStore, owner: str = "pond") -> tuple[str, str]:
    """(canvas_id, token) of a new canvas `owner` created under a fresh name."""
    return store.create_named(owner, f"canvas-{next(_names)}")


def legacy_ownerless_canvas(store: CanvasStore) -> tuple[str, str]:
    """(canvas_id, token) of an ownerless canvas: the same row, without owner and name."""
    canvas_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    with store._write():
        store.db.execute("INSERT INTO canvases (id, token_hash, created_s, modified_s) "
                         "VALUES (?, ?, ?, ?)",
                         (canvas_id, storage._hash_token(token), time.time(), time.time()))
    return canvas_id, token


def push(store: CanvasStore, canvas_id: str, svg: str) -> None:
    """Push a drawing the way MCP's push_scene does, by the canvas's owner and
    name; an ownerless canvas takes the drawing directly (it has no name)."""
    canvas = store.read(canvas_id)
    if canvas.owner is not None:
        store.write_named_scene(canvas.owner, canvas.name, svg)
        return
    clean = storage._sanitize_svg(svg)
    with store._write():
        store.db.execute("UPDATE canvases SET scene_svg = ? WHERE id = ?", (clean, canvas_id))


def backdate(store: CanvasStore, canvas_id: str, age_s: float) -> None:
    """Make a canvas `age_s` old: create stamps the current time, and the
    sweep needs canvases that are genuinely old."""
    with store._write():
        store.db.execute("UPDATE canvases SET created_s = ? WHERE id = ?",
                         (time.time() - age_s, canvas_id))
