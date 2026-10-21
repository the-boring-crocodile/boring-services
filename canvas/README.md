# Boring Canvas

A drawable canvas (Excalidraw) for agents and people: an agent creates a
canvas and pushes a drawing, a person opens its link and draws on it, the
agent reads the edits back. Agents reach it over MCP, people in the browser.

## Why

Talking about a design is easier with a picture, and neither side of the
chat can draw in it: the AI can describe or write SVG into a reply, the person
can only type. Canvas gives both a shared surface. The agent pushes a
drawing (an SVG it made, for example a diagram from the
[diagram guide](https://github.com/the-boring-crocodile/boring-pond/blob/main/guides/boring-diagrams/HOWTO.md));
the person opens the link, sees it and sketches on it (arrows, notes, boxes
crossed out); the agent reads those edits back as data and carries on. It
works the same from a Pond, from Copilot on Windows, or from any MCP client.

## How it works

The agent calls `create_canvas` and gets the page's address; `push_scene`
stores the SVG, cleaned of active content (scripts, embedded foreign
content, event handlers), as the
drawing. The page in the browser is the Excalidraw editor, shipped with the
service, and asks the server for changes every second. What the person draws
is saved as Excalidraw elements; `get_elements` and `get_edits` return them
as structured data (shapes, text, positions), so the agent reads what was
drawn, not a screenshot. Each canvas has its own browser token, separate from
the agent's MCP access, and expires after six weeks unless configured
otherwise.

## What it offers

- **MCP:** `create_canvas(name)`, `push_scene(name, svg)`, `get_elements(name)`,
  `get_edits(name)`, `list_canvases()`, `delete_canvas(name)` (no undo), and
  `open_canvas(name)` (only with `--unsafe-url-auth`). Canvases are named per
  caller; the owner is the verified caller, never an argument, and owners are
  isolated even among writers. No canvas IDs or tokens in tool arguments. A
  canvas is deleted `--max-age-days` (default 42) after creation;
  `list_canvases` gives each one's `expires`.
- **Browser:** `https://crocodile-url.example.test/canvas/<id>` (in the
  [example site](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/example-site.md)), signed in with the per-canvas
  browser token (then kept in a cookie). Over MCP the token appears only
  inside an `open_url` (with `--unsafe-url-auth`). By default the agent never
  sees it: `create_canvas` returns the clean URL and, in
  `token_from_operator`, the command the server's operator runs on the host
  (`sudo boring-canvas-admin token NAME --owner CALLER`) to give the person a
  fresh token; the person opens the URL and enters it. The page polls its
  canvas every second.
  Limit: the page saves Excalidraw's elements, not its separate image files,
  so a raster image a person pastes or imports is kept as an element without
  its picture and shows empty after a reload. Draw, or have the agent push an
  SVG (rebuilt from the stored scene on every load), instead.
- **CLI / HTTP:** `boring-canvas draw|read|rotate` against the web port, with
  canvas ID and token (from `open_url`). Canvases are created over MCP only:
  the web port and the CLI create none.
- **Admin:** `sudo boring-canvas-admin list | delete <id> | token NAME
  [--owner CALLER] | limits` (on Docker installs a wrapper runs it in the
  container). `token` replaces the canvas's stored token hash (the old token
  and its browsers are signed out) and prints the new token once, with the
  canvas URL, for the operator to hand to the person. Admin writes take the
  server's records lock (a file lock in `state/`), so they need no stop.
  On a native install run `boring-canvas-admin` as the service account
  (`sudo -u SERVICE-ACCOUNT boring-canvas-admin --state-dir ...`), never as
  root: a `state/.records.lock` that root creates is root's, and the server
  can then no longer take the lock. On Docker the wrapper does that: it runs
  the command in the container as the service's user.

Not goals: pushing changes from the canvas to the chat, WebSocket relays,
several producers broadcasting.

## How it runs

One process, two listeners: the web port (stdlib `http.server`, routes under
`/canvas/` only, bodies need `Content-Length` and are capped at 8 MiB) and the
MCP port (Streamable HTTP, stateless). The reverse proxy routes `/canvas/*` to
the web port (`handle`, not `handle_path`) and the MCP paths to the MCP port.
The editor is unmodified Excalidraw 0.18, bundled with the service (with the
licences of everything bundled, `frontend/notices/`); the page loads nothing
from elsewhere, and its Content-Security-Policy allows only its own server. Configuration: [CONFIGURATION](../docs/CONFIGURATION.md). Install
settings (`--public-url`, `--web-bind`, `--unsafe-url-auth`):
[README](../README.md#installing-on-linux).

## State and backup

One JSON file per canvas in `state/`, written atomically, its token stored as
SHA-256; a name index (`state/names/<sha256(owner)>/<name>`) keeps names
unique per owner. Writes, creation (with its limits), deletion and the expiry
sweep (run on creation) share one lock, `state/.records.lock`, taken by the
server and by `boring-canvas-admin` alike. Corrupt files are reported (`admin
list`, `limits`), never swept.

Everything to back up is the service's home (default `/opt/boring-canvas`):
`state/`, `config/` and `credentials/`. How you back it up is yours; for
a consistent copy stop the service first, since a live copy can catch files
and the name index at different moments.

Moving state from Linux to Windows: new name markers are `.name-<name>`
(Windows reserves `con`, `aux`, `nul`, ...); stop the service, back up, rename
old plain markers to `.name-<name>` refusing collisions, then copy.

## Tests

`tests/test_storage.py`, `tests/test_server.py`, `tests/test_mcp_service.py`
(against a fake control plane), with `PYTHONPATH=src` in a venv from
`requirements.lock`.
