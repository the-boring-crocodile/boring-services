"""boring-canvas backend: HTTP routes on stdlib http.server, no framework,
plus an optional MCP service on a second, internal port in the same process
(see mcp_service.py; the web routes below do not depend on it).

    POST   /canvas/<id>/session        exchange token for a session cookie
    GET    /canvas/<id>                serve the page (Excalidraw shell)
    GET    /canvas/<id>/version        cheap change-detection hash, for polling
    POST   /canvas/<id>/elements       browser saves its own scene (cookie)
    GET    /canvas/assets/...          the page's bundled scripts and fonts

Pushing a drawing and reading back what was drawn are MCP tools
(mcp_service.py), addressed by name for the verified caller; the web port
only serves the person's browser.

No web route creates a canvas: canvases are created over MCP only
(create_canvas, by a verified caller); `POST /canvas` is an unknown route
(404).

Admin operations (list every canvas, delete one, limits) are not HTTP routes:
they run inside the container as `boring-canvas-admin` (admin.py), so only
root can use them: this port must stay reachable from the reverse proxy
through the Docker host gateway, so it is open to the LAN and every local
account, and nothing on it may be an admin operation.

See ../../README.md for why these and not more.
"""
from __future__ import annotations

from typing import Any
from boring_service_kit.tools import default_state_dir

import argparse
import http.server
import socketserver
import threading
from pathlib import Path

from .storage import (
    CanvasStore, DEFAULT_CREATE_RATE_LIMIT, DEFAULT_MAX_AGE_DAYS,
    DEFAULT_MAX_CANVASES,
)

from .web_handler import CanvasHandler
from .web_views import (UNSAFE_URL_AUTH_PARAM)


def make_handler(store: CanvasStore, public_base_url: str | None) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(CanvasHandler):
        timeout = 30  # seconds a client may stall while sending or receiving
    Handler.store = store
    Handler.public_base_url = public_base_url
    return Handler


def serve(state_dir: Path, host: str, port: int, public_base_url: str | None,
          unsafe_url_auth: bool = False, max_age_days: float = DEFAULT_MAX_AGE_DAYS,
          create_rate_limit: int = DEFAULT_CREATE_RATE_LIMIT,
          max_canvases: int = DEFAULT_MAX_CANVASES, service_config: Any = None) -> None:
    """Run the web server; with `service_config` (boring-service-kit's
    ServiceConfig, from --config) also the MCP service."""
    store = CanvasStore(state_dir, max_age_days=max_age_days,
                         create_rate_limit=create_rate_limit, max_canvases=max_canvases)
    # Resolve once, used by both listeners. HTTP has a second option (the
    # request's own Host header) that MCP doesn't -- an MCP tool call
    # carries no browser-facing Host header -- so MCP always needs this
    # resolved value, falling back to a best-effort guess from
    # --host/--port when --public-base-url isn't given, same spirit as the
    # HTTP fallback but necessarily less precise (no per-request signal to
    # work from).
    resolved_base_url = public_base_url or f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}"
    handler = make_handler(store, public_base_url)
    print(f"boring-canvas: canvas expiry {max_age_days} days from creation, "
          f"swept when a canvas is created (no scheduler)")
    print("boring-canvas: canvases are created over MCP only; the web port "
          "serves existing canvases")
    print(f"boring-canvas: create rate limit "
          f"{'unlimited' if not create_rate_limit else f'{create_rate_limit}/60s'}, "
          f"max canvases {'unlimited' if not max_canvases else max_canvases} "
          f"(reportable with `boring-canvas-admin limits`)")
    if unsafe_url_auth:
        print(f"boring-canvas: --unsafe-url-auth is ON -- open_url carries the token "
              f"in the fragment (#{UNSAFE_URL_AUTH_PARAM}=...); the page signs in with it.")

    class _Server(socketserver.ThreadingTCPServer):
        # Default is False; a restart right after stopping the previous
        # listener then fails to bind while the old socket sits in
        # TIME_WAIT (OSError: Address already in use) -- hit exactly this
        # while restarting for a fix, not a hypothetical.
        allow_reuse_address = True

    with _Server((host, port), handler) as httpd:
        print(f"boring-canvas listening on http://{host}:{port} (state: {state_dir})")
        if service_config is None:
            httpd.serve_forever()
            return
        # MCP: uvicorn owns the main thread (it installs the SIGINT/SIGTERM
        # handlers); the web server runs beside it in a thread. On SIGTERM
        # uvicorn shuts MCP down (lifespan: session manager, watchdog task
        # cancelled in boring_service_kit.pond.run) and then re-raises the signal, which
        # ends the process and the web thread with it, exit status 143 --
        # the same abrupt end the web server always had (writes are SQLite
        # transactions, see storage.py). If uvicorn returns any other way (e.g.
        # the MCP port cannot be bound), the web server is shut down here.
        # Imported here so the web-only mode needs no third-party package.
        import asyncio
        import logging

        from boring_service_kit.pond import run

        from . import mcp_service

        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
        app = service_config.app(mcp_service.build_server(
            store, resolved_base_url, service_config.auth, service_config.on_discover, unsafe_url_auth))
        mcp_host, mcp_port = service_config.bind, service_config.port
        web = threading.Thread(target=httpd.serve_forever, name="boring-canvas-web")
        web.start()
        print(f"boring-canvas MCP on {mcp_host}:{mcp_port}")
        try:
            asyncio.run(run(app, service_config.watchdog, mcp_host, mcp_port))
        finally:
            httpd.shutdown()
            web.join()


def build_parser() -> argparse.ArgumentParser:
    """The server's command line; admin.py parses the running server's
    arguments with it, so both read the limits the same way."""
    parser = argparse.ArgumentParser(description="boring-canvas backend")
    parser.add_argument("--config", type=Path,
                        help="the MCP service's configuration (boring-service-kit ServiceConfig: its own "
                             "issuer, an external one, access keys); without it, the web server only")
    parser.add_argument("--state-dir", type=Path,
                         default=default_state_dir("boring-canvas"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18510)
    parser.add_argument("--public-base-url", default=None,
                         help="e.g. https://crocodile-url.example.test (scheme+domain "
                              "ONLY -- canvas always appends /canvas/<id> itself, "
                              "so a value that already ends in /canvas doubles "
                              "up). Canvas's own belief about its address. Omit "
                              "for LAN testing; falls back to the request's own "
                              "Host header.")
    parser.add_argument("--unsafe-url-auth", action="store_true",
                         help="this-is-unsafe-dont-do-this-auth: a freshly-created "
                              "canvas link with ?this-is-unsafe-dont-do-this-auth="
                              "<token> in it auto-authenticates on click, no "
                              "separate token paste. Off by default: it puts the "
                              "token back in the URL, where anyone who sees the "
                              "link can use it. The query-param name is the "
                              "warning.")
    parser.add_argument("--max-age-days", type=float, default=DEFAULT_MAX_AGE_DAYS,
                         help=f"A canvas older than this (from creation) is deleted "
                              f"-- swept as a side effect of creating a canvas, "
                              f"no scheduler. "
                              f"Default: {DEFAULT_MAX_AGE_DAYS} days.")
    parser.add_argument("--create-rate-limit", type=int, default=DEFAULT_CREATE_RATE_LIMIT,
                         help=f"Max canvases created per rolling 60-second window before "
                              f"create_canvas starts refusing with a 429/rate_limited. 0 "
                              f"means unlimited. Default: {DEFAULT_CREATE_RATE_LIMIT}. "
                              f"Stops a runaway loop from creating canvases unnoticed.")
    parser.add_argument("--max-canvases", type=int, default=DEFAULT_MAX_CANVASES,
                         help=f"Hard ceiling on total canvases that can exist at once; "
                              f"create_canvas refuses with 429/too_many_canvases once hit. "
                              f"0 means unlimited. Default: {DEFAULT_MAX_CANVASES}. "
                              f"Complements --create-rate-limit -- this catches slow "
                              f"accumulation, the rate limit catches a burst.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config = None
    if args.config:
        from boring_service_kit.http import ServiceConfig
        config = ServiceConfig(args.config)
    serve(args.state_dir, args.host, args.port, args.public_base_url,
          args.unsafe_url_auth, args.max_age_days,
          args.create_rate_limit, args.max_canvases, config)


if __name__ == "__main__":
    main()
