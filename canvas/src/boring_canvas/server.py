"""Canvas: one ASGI app and listener for MCP, issuer and browser routes."""
from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.routing import Route
from boring_service_kit.tools import default_state_dir
from boring_service_kit.http import ServiceConfig
from boring_service_kit.pond import run

from .storage import (CanvasStore, DEFAULT_CREATE_RATE_LIMIT,
    DEFAULT_MAX_AGE_DAYS, DEFAULT_MAX_CANVASES)
from .web_handler import CanvasWeb
from . import mcp_service


class App:
    """Route protocol endpoints to the kit and browser routes to Canvas."""
    def __init__(self, store, config, unsafe_url_auth=False):
        if not config.url.endswith("/mcp"):
            raise ValueError("Canvas resource URL must end with /mcp")
        self.config = config
        self.base = config.url[:-4]
        self.mcp = config.app(mcp_service.build_server(
            store, self.base, config.auth, config.on_discover, unsafe_url_auth))
        self.sdk = getattr(self.mcp, "authenticated", self.mcp)
        browser = CanvasWeb(store, self.base)

        @asynccontextmanager
        async def lifespan(app):
            async with self.sdk.router.lifespan_context(self.sdk):
                yield

        async def web(request):
            return await browser(request)
        self.web = Starlette(routes=[Route("/{path:path}", web,
            methods=["GET", "POST", "HEAD", "PUT", "DELETE", "OPTIONS"])], lifespan=lifespan)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (
            scope["path"] == urlsplit(self.config.url).path
            or scope["path"].startswith("/.well-known/")
            or scope["path"] in self.config.issuer_paths):
            await self.mcp(scope, receive, send)
        else:
            await self.web(scope, receive, send)


def serve(state_dir, config, unsafe_url_auth=False,
          max_age_days=DEFAULT_MAX_AGE_DAYS,
          create_rate_limit=DEFAULT_CREATE_RATE_LIMIT,
          max_canvases=DEFAULT_MAX_CANVASES):
    store = CanvasStore(state_dir, max_age_days=max_age_days,
        create_rate_limit=create_rate_limit, max_canvases=max_canvases)
    try:
        app = App(store, config, unsafe_url_auth)
        asyncio.run(run(app, config.watchdog, config.bind, config.port, access_log=False))
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    """The server's command line; admin.py parses the running server's
    arguments with it, so both read the limits the same way."""
    parser = argparse.ArgumentParser(description="boring-canvas backend")
    parser.add_argument("--config", type=Path,
                        help="the service configuration (MCP and browser share one listener)")
    parser.add_argument("--state-dir", type=Path,
                         default=default_state_dir("boring-canvas"))
    parser.add_argument("--unsafe-url-auth", action="store_true",
                         help="this-is-unsafe-dont-do-this-auth: a freshly-created "
                              "canvas link with #this-is-unsafe-dont-do-this-auth="
                              "<token> in it auto-authenticates on click, no "
                              "separate token paste. Off by default: it puts the "
                              "token back in the URL, where anyone who sees the "
                              "link can use it. The fragment name is the "
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


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.config is None:
        parser.error("--config is required; Canvas serves MCP and browser routes together")
    config = ServiceConfig(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    serve(args.state_dir, config, args.unsafe_url_auth, args.max_age_days,
          args.create_rate_limit, args.max_canvases)


if __name__ == "__main__":
    main()
