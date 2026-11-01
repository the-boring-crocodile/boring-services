# Boring Canvas

A drawable canvas ([Excalidraw](https://excalidraw.com/)) for agents and people: an agent creates a
canvas and pushes a drawing, a person opens its link and draws on it, the
agent reads the edits back. Agents reach it over MCP, people in the browser.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../docs/canvas-dark.svg">
  <img alt="Canvas, one drawing between an agent and a person, in seven steps: the AI agent calls create_canvas and push_scene on Canvas's MCP tools (1); the drawing is cleaned of scripts and stored (2); the operator on the host runs boring-canvas-admin token (3) and hands the person the link with the token (4); the person opens the Canvas page in a browser and draws (5); the page saves the person's marks (6); the agent calls get_edits and gets them back as data, shapes, text and positions, not a screenshot (7). The MCP tools, the page and the store run in one container with its own account; the store keeps a canvas 42 days." src="../docs/canvas.svg">
</picture>

## Why

A diagram in a chat explains a design, but discussing changes still means
referring to boxes and arrows in text. Canvas gives the agent and the person
a shared surface: the person can mark the part they mean, and the agent can
read those marks back. The agent pushes a
drawing (an SVG it made, for example a diagram from the
[diagram guide](https://github.com/the-boring-crocodile/boring-pond/blob/main/guides/boring-diagrams/HOWTO.md));
the person opens the link, sees it and sketches on it (arrows, notes, boxes
crossed out); the agent reads those edits back as data and carries on. It
works the same from a Pond, from Copilot on Windows, or from any MCP client.

For a first drawing, read [How it works](#how-it-works); for access and
lifetime, [What it offers](#what-it-offers); for retaining work,
[State and backup](#state-and-backup).

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
  - `push_scene` through a Pond can take the SVG as a file reference,
    `"svg": {"$file": {"root": ..., "path": ...}}`: the Pond reads the file
    and sends its text (up to 512 KiB per call), so a 10-20 KB drawing does
    not pass through the agent.
  - `get_elements` also gives each mark's box in the pushed SVG's own
    coordinates (`svg`, with the viewBox in `drawing`), following how the
    browser draws the picture (its intrinsic size stretched to where the
    person placed it, the viewBox fitted by `preserveAspectRatio`). A rotated,
    flipped or cropped picture, or an SVG whose size the browser had to guess,
    gives `svg_coordinates` with the reason instead of numbers. Placement must carry the same
    SVG version as the current drawing; after a changed push or an old tab's delayed save,
    coordinates stay unavailable until a matching placement is saved. Legacy placements need
    one reload/save with the current page. Annotations are retained; unchanged SVG content
    keeps its identity.
  - `get_edits` returns the newest edits that fit one answer; `before` (the
    `seq` the answer gives) pages back through the 500 the server keeps per
    canvas, since its last restart.
- **Browser:** `https://crocodile-url.example.test/canvas/<id>` (in the
  [example site](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/example-site.md)), signed in with the per-canvas
  browser token (then kept in a cookie). Over MCP the token appears only
  inside an `open_url` (with `--unsafe-url-auth`), in the link's fragment,
  which never reaches the server; the page signs in with it and clears it
  from the address bar. A token in the query signs nobody in. By default the agent never
  sees it: `create_canvas` returns the clean URL and, in
  `token_from_operator`, the command the server's operator runs on the host
  (`sudo boring-canvas-admin token NAME --owner CALLER`) to give the person a
  fresh token; the person opens the URL and enters it. The page polls its
  canvas every second.
  Limit: the page saves Excalidraw's elements, not its separate image files,
  so a raster image a person pastes or imports is kept as an element without
  its picture and shows empty after a reload. Draw, or have the agent push an
  SVG (rebuilt from the stored scene on every load), instead.
- **Web port:** serves only the person's browser (page, session, saves,
  polling). Pushing, reading back and creating are MCP tools; the web port
  creates no canvas and accepts no pushed drawing.
- **Admin:** `sudo boring-canvas-admin list | delete <id> | token NAME
  [--owner CALLER] | limits` (on Docker installs a wrapper runs it in the
  container). `token` replaces the canvas's stored token hash (the old token
  and its browsers are signed out) and prints the new token once, with the
  canvas URL, for the operator to hand to the person. Admin writes are
  transactions on the server's database, so they need no stop.
  On a native install run `boring-canvas-admin` as the service account
  (`sudo -u SERVICE-ACCOUNT boring-canvas-admin --state-dir ...`), never as
  root: SQLite files (`-wal`, `-shm`) that root creates are root's, and the
  server can then no longer write. On Docker the wrapper does that: it runs
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

Canvas is a working surface with an expiry date. Keep the source drawing and
any decisions in the project; an annotation here does not update those files.
Back up Canvas as well when the live drawings themselves need to survive a
host failure.

One SQLite database, `state/canvas.sqlite` (WAL), tokens stored as SHA-256,
names unique per owner. Every write, creation with its limits, deletion and
the expiry sweep (run on creation) is one IMMEDIATE transaction, the same for
the server and `boring-canvas-admin`. A database of an unknown schema is
refused unchanged.

Upgrading from 0.2.17 or older: the first start finds no database and imports
the canvas files of `state/` (`<id>.json`; the `names/` index only repeated
them) in one transaction, then logs how many. A file that is not a complete
canvas record stops the start with its name, and nothing is imported; move it
aside and start again. The old files stay as they were: rolling back to 0.2.17
reads them, so it shows the canvases as they were at the upgrade (drawings,
pushes, new and deleted canvases since then are not in them). Rolling forward
again keeps the database; to re-import the files instead, remove
`state/canvas.sqlite*` first. Once you will not roll back, the old files can
be deleted.

Everything to back up is the service's home (default `/opt/boring-canvas`):
`state/`, `config/` and `credentials/`. How you back it up is yours; for a
consistent copy stop the service first (or use `sqlite3 .backup`).

## Tests

`tests/test_storage.py`, `tests/test_server.py`, `tests/test_mcp_service.py`
(against a fake control plane), with `PYTHONPATH=src` in a venv from
`requirements.lock`.
