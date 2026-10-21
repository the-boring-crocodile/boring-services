"""Print {"canvas_id", "token"} of a new canvas in a throwaway state directory.

Canvases are created over MCP only (no web route creates one), so the
browser checks create theirs the way MCP's create_canvas
does: CanvasStore.create_named on the test server's state directory.
Usage: python new_canvas.py STATE_DIR NAME   (with PYTHONPATH=src)
"""
import json
import sys
from pathlib import Path

from boring_canvas.storage import CanvasStore

state_dir, name = Path(sys.argv[1]), sys.argv[2]
canvas_id, token = CanvasStore(state_dir).create_named("browser-test", name)
print(json.dumps({"canvas_id": canvas_id, "token": token}))
