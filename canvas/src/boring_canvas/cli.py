"""boring-canvas CLI: push an SVG file to an existing canvas, read it back,
rotate its token.

    boring-canvas draw --canvas <id> --token <token> --file rose.svg
    boring-canvas read --canvas <id> --token <token>
    boring-canvas rotate --canvas <id> --token <token>

Canvases are created over MCP only (create_canvas, by a verified caller);
the canvas id and token come from its open_url (with --unsafe-url-auth).

Listing every canvas, deleting one and the limits are admin operations:
`sudo boring-canvas-admin list|delete <id>|limits` (admin.py, inside the
container), never HTTP routes.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# The web port as a default install publishes it (deploy --web-bind 127.0.0.1).
DEFAULT_BASE_URL = "http://127.0.0.1:18510"


def _post(url: str, *, data: bytes, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"{exc.code}: {body}", file=sys.stderr)
        raise SystemExit(1) from exc


def _get(url: str, *, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"{exc.code}: {body}", file=sys.stderr)
        raise SystemExit(1) from exc


def cmd_draw(args: argparse.Namespace) -> None:
    svg = args.file.read_text(encoding="utf-8")
    url = f"{args.base_url}/canvas/{args.canvas}/scene"
    result = _post(url, data=svg.encode("utf-8"), headers={
        "Authorization": f"Bearer {args.token}",
        "Content-Type": "image/svg+xml",
    })
    print(json.dumps(result, indent=2))


def cmd_read(args: argparse.Namespace) -> None:
    url = f"{args.base_url}/canvas/{args.canvas}/elements"
    result = _get(url, headers={"Authorization": f"Bearer {args.token}"})
    if args.out:
        args.out.write_text(json.dumps(result["elements"], indent=2), encoding="utf-8")
        print(f"wrote {len(result['elements'])} elements to {args.out}")
    else:
        print(json.dumps(result, indent=2))


def cmd_rotate(args: argparse.Namespace) -> None:
    url = f"{args.base_url}/canvas/{args.canvas}/rotate"
    result = _post(url, data=b"", headers={"Authorization": f"Bearer {args.token}"})
    print(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="boring-canvas")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    sub = parser.add_subparsers(dest="command", required=True)

    p_draw = sub.add_parser("draw", help="push an SVG file to a canvas")
    p_draw.add_argument("--canvas", required=True)
    p_draw.add_argument("--token", required=True)
    p_draw.add_argument("--file", required=True, type=Path)
    p_draw.set_defaults(func=cmd_draw)

    p_read = sub.add_parser("read", help="read back the drawn scene elements")
    p_read.add_argument("--canvas", required=True)
    p_read.add_argument("--token", required=True)
    p_read.add_argument("--out", type=Path, default=None,
                         help="write elements JSON here instead of stdout")
    p_read.set_defaults(func=cmd_read)

    p_rotate = sub.add_parser("rotate", help="invalidate the current token, issue a new one")
    p_rotate.add_argument("--canvas", required=True)
    p_rotate.add_argument("--token", required=True, help="the current token -- proves you have access")
    p_rotate.set_defaults(func=cmd_rotate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
