#!/bin/bash
# Browser checks that unit tests cannot make: a push within the 1 s save
# debounce keeps the person's strokes, and the page runs under its
# Content-Security-Policy without violations, export included.
# Runs a throwaway canvas server from this checkout on a spare port; never
# touches the live one. Needs node and Playwright with Firefox:
#   (cd DIR && npm install playwright@1.55.0 && PLAYWRIGHT_BROWSERS_PATH=DIR/browsers npx playwright install firefox)
# Usage: tests/browser/run.sh DIR PYTHON   (DIR holds node_modules and browsers)
set -euo pipefail
DIR=$1; PY=$2; REPO=$(cd "$(dirname "$0")/../.." && pwd); PORT=${PORT:-18592}
STATE=$(mktemp -d); trap 'kill $SRV 2>/dev/null; rm -rf "$STATE"' EXIT
(cd "$REPO" && PYTHONPATH=src exec "$PY" -m boring_canvas.server --state-dir "$STATE" --host 127.0.0.1 --port "$PORT" --unsafe-url-auth >"$STATE/server.log" 2>&1) & SRV=$!
sleep 2
# Each check gets its own canvas in CANVAS ({canvas_id, token}); no web route
# creates one or takes a drawing (those are MCP tools), so the checks use the
# store, as MCP does (canvas_store.py, a second process like the admin).
export CANVAS_PY=$PY CANVAS_STORE=$REPO/tests/browser/canvas_store.py CANVAS_STATE=$STATE PYTHONPATH=$REPO/src
new_canvas() { "$PY" "$CANVAS_STORE" "$STATE" new "$1"; }
export PLAYWRIGHT_BROWSERS_PATH=$DIR/browsers NODE_PATH=$DIR/node_modules
export PLAYWRIGHT_MODULE=$DIR/node_modules/playwright
cp "$REPO"/tests/browser/*.mjs "$DIR"/
cd "$DIR"
echo "== push keeps strokes"; CANVAS=$(new_canvas push-keeps-strokes) node push_keeps_strokes.mjs "http://127.0.0.1:$PORT" push
echo "== export under CSP"; CANVAS=$(new_canvas export-under-csp) node export_under_csp.mjs "http://127.0.0.1:$PORT"
echo "== strokes stay on top of the pushed drawing"; CANVAS=$(new_canvas strokes-stay-on-top) node strokes_stay_on_top.mjs "http://127.0.0.1:$PORT"
echo "== offline assets"; CANVAS=$(new_canvas offline-assets) node offline_assets.mjs "http://127.0.0.1:$PORT"
echo "== placement identity"; CANVAS=$(new_canvas placement-identity) node scene_placement.mjs "http://127.0.0.1:$PORT"
