"""Canvases for tests, made the two ways a canvas exists today.

- new_canvas: created the one way canvases are created (CanvasStore.create_named,
  what MCP's create_canvas calls). Counts toward the create rate limit and
  max_canvases like any MCP create.
- legacy_ownerless_canvas: a canvas as the removed `POST /canvas` made it
  (no owner, no name). None are created any more; one on disk keeps working
  until it expires.
"""
from __future__ import annotations

import itertools
import secrets
import time
import uuid

from boring_canvas import storage
from boring_canvas.storage import CanvasStore

_names = itertools.count(1)


def new_canvas(store: CanvasStore, owner: str = "pond") -> tuple[str, str]:
    """(canvas_id, token) of a new canvas `owner` created under a fresh name."""
    return store.create_named(owner, f"canvas-{next(_names)}")


def legacy_ownerless_canvas(store: CanvasStore) -> tuple[str, str]:
    """(canvas_id, token) of an ownerless canvas file as the removed web
    create wrote it: the same record, without owner and name."""
    canvas_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    storage._atomic_write_json(store._path(canvas_id), storage._record(storage.Canvas(
        canvas_id, storage._hash_token(token), None, None, time.time(), None, None)))
    return canvas_id, token
