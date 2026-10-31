"""The browser checks' side door to the throwaway server's store.

No web route creates a canvas or takes a drawing: those are MCP tools. The
checks call the same store methods MCP's tools call, on the test server's
state directory (a second process, as boring-canvas-admin is):

    python canvas_store.py STATE_DIR new NAME     {"canvas_id", "token"}
    python canvas_store.py STATE_DIR push ID      SVG on stdin (push_scene)
    python canvas_store.py STATE_DIR elements ID  {"elements": [...]} as saved

(with PYTHONPATH=src)
"""
import json
import sys
from pathlib import Path

from boring_canvas.storage import CanvasStore

state_dir, command, arg = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
store = CanvasStore(state_dir)
if command == "new":
    canvas_id, token = store.create_named("browser-test", arg)
    print(json.dumps({"canvas_id": canvas_id, "token": token}))
elif command == "push":
    canvas = store.read(arg)
    store.write_named_scene(canvas.owner, canvas.name, sys.stdin.read())
elif command == "elements":
    saved = store.read(arg).elements_json
    print(json.dumps({"elements": json.loads(saved) if saved else []}))
else:
    raise SystemExit(f"unknown command {command}")
