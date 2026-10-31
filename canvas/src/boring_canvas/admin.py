"""boring-canvas admin: list, delete, token and limits, run inside the container.

    boring-canvas-admin list             every canvas
    boring-canvas-admin delete <id>      remove one canvas
    boring-canvas-admin token NAME [--owner CALLER]
                                         a fresh browser token for a named canvas
    boring-canvas-admin limits           configured limits and current standing

`token` is how the person gets a canvas's browser token when the server runs
without --unsafe-url-auth (the default): create_canvas keeps only the token's
hash and tells the agent to send the person to the operator. The operator
runs `token` with the canvas's name (and, when several callers use that name,
the owner `list` shows); it replaces the stored hash, so the previous token
and every browser signed in with it stop working, and prints the new token
once, beside the canvas's clean URL. The person opens the URL and enters the
token in the page's form. The token is never stored, logged or put in a URL.

`boring-canvas-admin` is deploy/canvas-admin, put in place by the first install: it runs
this module with `docker compose exec` in the running container. Authority is
whoever may use Docker on the host, i.e. root -- the same people who own the
state files anyway. They are not HTTP routes on purpose: the web port must
stay reachable from the reverse proxy through the Docker host gateway, so it
is open to the LAN and the other local accounts.

This is a second process next to the server, on the same state directory:

- The limits are the server's own: read from its command line
  (/proc/1/cmdline, PID 1 of the container) and parsed by the server's own
  argument parser, so this reports what the server enforces.
- `creates_in_current_window` lives only in the server's memory and is not
  visible from here; it is reported as null, not as a made-up 0.
- `delete` and `token` are CanvasStore writes: one SQLite IMMEDIATE
  transaction each on the database the server uses, so a server write in
  flight finishes first, and none that read the record before can publish
  it again afterwards or restore a replaced hash. The server's in-memory
  edit log for a deleted id stays until its next restart (harmless).

Output is JSON on stdout. Exit status: 0 done, 1 no such canvas, 2 usage or
a server command line that cannot be read.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .server import build_parser
from .storage import CanvasNotFound, CanvasStore

SERVER_CMDLINE = Path("/proc/1/cmdline")
SERVER_MODULE = "boring_canvas.server"
SERVER_SCRIPT = "boring-canvas-server"  # the console script the image runs


def server_args(cmdline: bytes) -> argparse.Namespace:
    """The server's parsed arguments from a NUL-separated /proc cmdline: started as
    `python -m boring_canvas.server ...` or as the console script
    (`python /usr/local/bin/boring-canvas-server ...`, the image's command)."""
    argv = [part.decode("utf-8") for part in cmdline.split(b"\0") if part]
    for index, part in enumerate(argv[:3]):
        if part == SERVER_MODULE or Path(part).name == SERVER_SCRIPT:
            return build_parser().parse_args(argv[index + 1:])
    raise ValueError(f"not the canvas server: {argv[:3]}")


def store_for(args: argparse.Namespace) -> CanvasStore:
    return CanvasStore(args.state_dir, max_age_days=args.max_age_days,
                       create_rate_limit=args.create_rate_limit,
                       max_canvases=args.max_canvases)


def issue_token(store: CanvasStore, name: str, owner: str | None,
                public_base_url: str | None) -> tuple[int, dict]:
    """A fresh browser token for the canvas `name` (of `owner`, or of the one
    owner using that name); the old token stops working."""
    if owner is None:
        owners = sorted({c["owner"] for c in store.list_canvases() if c.get("name") == name and "owner" in c})
        if len(owners) > 1:
            return 2, {"error": "several owners have a canvas of this name; pass --owner",
                       "name": name, "owners": owners}
        if not owners:
            return 1, {"error": "no such canvas", "name": name}
        owner = owners[0]
    try:
        canvas_id, token = store.reissue_named_token(owner, name)
    except CanvasNotFound:
        return 1, {"error": "no such canvas", "name": name, "owner": owner}
    path = f"/canvas/{canvas_id}"
    answer = {"name": name, "owner": owner, "canvas_id": canvas_id, "token": token,
              "url": f"{public_base_url.rstrip('/')}{path}" if public_base_url else path,
              "note": "shown once, for the person only: they open the url and enter the token; "
                      "the previous token no longer works"}
    return 0, answer


def run(command: str, canvas_id: str | None, store: CanvasStore, owner: str | None = None,
        public_base_url: str | None = None) -> tuple[int, dict]:
    """One admin command; `canvas_id` is the canvas id (delete) or name (token)."""
    if command == "list":
        return 0, {"canvases": store.list_canvases()}
    if command == "limits":
        status = store.limits_status()
        status["creates_in_current_window"] = None  # server memory only, see module docstring
        return 0, status
    if command == "token":
        return issue_token(store, canvas_id, owner, public_base_url)
    try:
        store.delete(canvas_id)
    except CanvasNotFound:
        return 1, {"error": "no such canvas", "canvas_id": canvas_id}
    return 0, {"ok": True, "deleted": canvas_id}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="boring-canvas-admin",
                                     description="boring-canvas admin (inside the container)")
    parser.add_argument("--state-dir", type=Path, help="native local state directory (the server's database)")
    parser.add_argument("--max-age-days", type=float, default=42)
    parser.add_argument("--create-rate-limit", type=int, default=10)
    parser.add_argument("--max-canvases", type=int, default=100)
    parser.add_argument("--public-base-url", default=None, help="native: the URL `token` prints the canvas under")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="every canvas")
    p_delete = sub.add_parser("delete", help="remove one canvas")
    p_delete.add_argument("canvas_id")
    p_token = sub.add_parser("token", help="a fresh browser token for a named canvas (the old one stops working)")
    p_token.add_argument("name")
    p_token.add_argument("--owner", help="the canvas's owner as `list` shows it, when several use the name")
    sub.add_parser("limits", help="configured limits and current standing")
    args = parser.parse_args(argv)
    if args.state_dir is not None:
        server = args
    else:
        try:
            server = server_args(SERVER_CMDLINE.read_bytes())
        except (OSError, ValueError) as exc:
            print(f"cannot read the canvas server's command line: {exc}", file=sys.stderr)
            return 2
    target = args.name if args.command == "token" else getattr(args, "canvas_id", None)
    status, result = run(args.command, target, store_for(server), getattr(args, "owner", None),
                         server.public_base_url)
    print(json.dumps(result, indent=2, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
