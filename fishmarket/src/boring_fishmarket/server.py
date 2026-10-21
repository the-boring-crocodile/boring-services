"""One ASGI process: browser streams and the unchanged service-kit MCP app."""

from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import unquote, urlsplit, quote
from starlette.applications import Starlette
from starlette.responses import Response, JSONResponse, HTMLResponse, StreamingResponse
from starlette.routing import Route
from boring_service_kit.outcomes import Outcome, Refused, Failed
from .storage import CHUNK, ID, transfer_window
from .service import build_server
from .upload_io import disk_call, append, durable

log = logging.getLogger("boring_fishmarket")
COOKIE = "fishmarket-browser"
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


async def small_json(request, limit=4096):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise Refused("limit", "request body too large")
        data.extend(chunk)
    try:
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeDecodeError):
        raise Refused("invalid_request", "expected JSON object") from None


def byte_range(header, size):
    if not header:
        return 0, size, 200
    match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", header)
    if not match or not any(match.groups()) or size == 0:
        raise Refused("range", "unsupported byte range")
    left, right = match.groups()
    if not left:
        length = int(right)
        if length == 0:
            raise Refused("range", "unsupported byte range")
        start = max(0, size - length)
        end = size
    else:
        start = int(left)
        end = min(size, int(right) + 1) if right else size
        if start >= size or end <= start:
            raise Refused("range", "unsupported byte range")
    return start, end, 206


class FileResponse(StreamingResponse):
    """A payload stream that ends at its transfer's deadline; CLOSE (blocking:
    it closes the payload and releases the transfer under the store lock) runs
    in a thread once the stream ends, however it ends."""

    def __init__(self, *args, close, deadline, clock, **kwargs):
        self.close = close
        self.deadline, self.clock = deadline, clock
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            # Includes stalled socket sends: expiry cannot retain an open payload indefinitely.
            remaining = max(0, self.deadline - self.clock())
            await asyncio.wait_for(super().__call__(scope, receive, send), remaining)
        finally:
            await disk_call(self.close)


class App:
    def __init__(self, store, config, unsafe_url_auth=False):
        self.store, self.config = store, config
        if not config.url.endswith("/mcp"):
            raise ValueError("resource URL must end with /mcp")
        base = config.url[:-4]
        parsed = urlsplit(base)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid browser base")
        if parsed.scheme == "http" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("browser base needs HTTPS except loopback fixtures")
        self.base = base
        self.prefix = parsed.path
        if not re.fullmatch(r"/[a-z0-9-]+(?:/[a-z0-9-]+)*", self.prefix):
            raise ValueError("browser base needs a safe path prefix")
        self.origin = parsed.scheme + "://" + parsed.netloc
        self.secure = parsed.scheme == "https"
        self.server = build_server(store, self.base, config, unsafe_url_auth)
        self.mcp = config.app(self.server)
        # A server that is its own issuer serves its sign-in endpoints too (under the issuer's path).
        self.sdk_paths = config.issuer_paths
        self.sdk = (
            self.mcp.authenticated if hasattr(self.mcp, "authenticated") else self.mcp
        )
        routes = [
            Route(
                self.prefix + "/download/{transfer_id}",
                self.download_page,
                methods=["GET", "HEAD"],
            ),
            Route(
                self.prefix + "/upload/{transfer_id}",
                self.upload_page,
                methods=["GET", "HEAD"],
            ),
            Route(self.prefix + "/web/{asset}", self.asset, methods=["GET", "HEAD"]),
            Route(
                self.prefix + "/api/{transfer_id}/claim", self.claim, methods=["POST"]
            ),
            Route(
                self.prefix + "/api/{transfer_id}/upload", self.upload, methods=["PUT"]
            ),
            Route(
                self.prefix + "/api/{transfer_id}/download",
                self.download,
                methods=["GET", "HEAD"],
            ),
            Route(
                self.prefix + "/api/{transfer_id}/collect",
                self.collect,
                methods=["GET", "HEAD"],
            ),
            Route(self.prefix + "/health", self.health, methods=["GET"]),
        ]

        @asynccontextmanager
        async def lifespan(app):
            async with self.sdk.router.lifespan_context(self.sdk):
                sweeper = asyncio.create_task(self.sweep())
                watchdog = (
                    asyncio.create_task(config.watchdog.run())
                    if config.watchdog
                    else None
                )
                try:
                    yield
                finally:
                    for task in (sweeper, watchdog):
                        if task:
                            task.cancel()
                            with suppress(asyncio.CancelledError):
                                await task

        self.web = Starlette(routes=routes, lifespan=lifespan)

    async def sweep(self):
        while True:
            await asyncio.sleep(5)
            try:
                await disk_call(self.store.cleanup)
            except Exception as exc:
                log.error(
                    "expiry cleanup failed (%s); retained quota/state needs inspection",
                    type(exc).__name__,
                )

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (
            scope["path"] == urlsplit(self.config.url).path
            or scope["path"].startswith("/.well-known/")
            or scope["path"] in self.sdk_paths
        ):
            await self.mcp(scope, receive, send)
            return

        # No raw query/token/body access log; uvicorn access logging is disabled by main.
        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] += [
                    (k.lower().encode(), v.encode()) for k, v in HEADERS.items()
                ]
            await send(message)

        await self.web(scope, receive, secure_send)

    def browser_origin(self, request):
        if request.headers.get("origin") != self.origin:
            raise Refused("forbidden", "same-origin browser action required")

    async def caller(self, request):
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise Refused("forbidden", "service bearer required")
        access = await self.config.token_verifier.verify_token(authorization[7:])
        if access is None or not isinstance(access.claims, dict):
            raise Refused("forbidden", "service bearer invalid")
        # The same check as the MCP path (boring_service_kit.access.calling_caller):
        # a verified token without a service identity is refused, not a KeyError.
        data = access.claims.get("boring_caller")
        if not isinstance(data, dict) or not isinstance(data.get("author"), str) or not data["author"]:
            raise Refused("forbidden", "this call has no verified service identity")
        return data["author"]

    def problem(self, exc):
        codes = {
            "forbidden": 403,
            "not_found": 404,
            "busy": 409,
            "limit": 413,
            "range": 416,
        }
        return JSONResponse(
            exc.as_dict(),
            status_code=codes.get(exc.code, 400) if exc.status == "refused" else 500,
        )

    async def health(self, request):
        return JSONResponse({"service": "boring-fishmarket", "status": "ready"})

    async def asset(self, request):
        asset = request.path_params["asset"]
        if asset not in {"market.js", "market.css", "icon.svg"}:
            return Response(status_code=404)
        data = (Path(__file__).parent / "web" / asset).read_bytes()
        return Response(
            data,
            media_type={
                "market.js": "text/javascript",
                "market.css": "text/css",
                "icon.svg": "image/svg+xml",
            }[asset],
        )

    async def download_page(self, request):
        return await self.page(request, "download")

    async def upload_page(self, request):
        return await self.page(request, "upload")

    async def page(self, request, direction):
        transfer_id = request.path_params["transfer_id"]
        if not ID.fullmatch(transfer_id):
            return Response(status_code=404)
        # Direction comes from the public link, never a private-state lookup.
        # Generic page even for unknown IDs: no filename, existence or state disclosure.
        browser = request.cookies.get(COOKIE)
        if not browser or not re.fullmatch(r"[A-Za-z0-9_-]{43}", browser):
            browser = secrets.token_urlsafe(32)
        web = Path(__file__).parent / "web"
        text = (web / "page.html").read_text(encoding="utf-8")
        content = (web / (direction + ".html")).read_text(encoding="utf-8")
        text = text.replace("@TITLE@", direction.title() + " a file").replace(
            "@CONTENT@", content
        )
        response = HTMLResponse(
            text.replace("@PREFIX@", self.prefix).replace("@ID@", transfer_id)
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.set_cookie(
            COOKIE,
            browser,
            path=self.prefix,
            httponly=True,
            secure=self.secure,
            samesite="strict",
            max_age=86400,
        )
        return response

    async def claim(self, request):
        try:
            self.browser_origin(request)
            body = await small_json(request)
            if (
                set(body) != {"token"}
                or not isinstance(body["token"], str)
                or len(body["token"]) > 128
            ):
                raise Refused("invalid_request", "claim needs token")
            browser = request.cookies.get(COOKIE, "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", browser):
                raise Refused("forbidden", "open transfer page first")
            until = await disk_call(
                self.store.claim, request.path_params["transfer_id"], body["token"], browser
            )
            return JSONResponse(
                {
                    "download_url": self.prefix
                    + "/api/"
                    + request.path_params["transfer_id"]
                    + "/download",
                    "retry_until": until,
                }
            )
        except Outcome as exc:
            return self.problem(exc)

    async def upload(self, request):
        transfer_id = request.path_params["transfer_id"]
        started = False
        try:
            # Browser must have our Origin; nonbrowser helper uses scoped capability only.
            if request.headers.get("origin") is not None:
                self.browser_origin(request)
            token = request.headers.get("x-fishmarket-token", "")
            if len(token) > 128:
                raise Refused("forbidden", "invalid upload capability")
            raw_length = request.headers.get("content-length", "")
            if not re.fullmatch(r"[0-9]{1,12}", raw_length):
                raise Refused("invalid_request", "exact Content-Length required")
            declared = int(raw_length)
            name = unquote(
                request.headers.get("x-fishmarket-filename", ""),
                encoding="utf-8",
                errors="strict",
            )
            def begin():
                nonlocal started
                row = self.store.upload_start(transfer_id, token, name, declared)
                started = True
                return row
            row = await disk_call(begin)

            async def write():
                count = 0
                digest = hashlib.sha256()
                with self.store.path(transfer_id, ".part").open("xb") as handle:
                    async for chunk in request.stream():
                        if not await disk_call(self.store.valid_stream, transfer_id):
                            raise Failed("expired", "upload expired; no publication")
                        if count + len(chunk) > declared:
                            raise Failed(
                                "size",
                                "upload exceeded declared length; no publication",
                            )
                        for at in range(0, len(chunk), CHUNK):
                            piece = chunk[at : at + CHUNK]
                            await disk_call(append, handle, digest, piece)
                        count += len(chunk)
                    if count != declared:
                        raise Failed("size", "upload incomplete; no publication")
                    await disk_call(durable, handle)
                await disk_call(self.store.upload_finish, transfer_id, count, digest.hexdigest())
                return {"uploaded": True, "size": count, "sha": digest.hexdigest()}

            try:
                return JSONResponse(
                    await asyncio.wait_for(
                        write(), min(transfer_window(declared),
                                     max(0, row["expires"] - self.store.clock()))
                    )
                )
            except BaseException:
                await disk_call(self.store.upload_abort, transfer_id)
                raise
        except asyncio.CancelledError:
            if started:
                await disk_call(self.store.upload_abort, transfer_id)
            raise
        except Outcome as exc:
            return self.problem(exc)
        except asyncio.TimeoutError:
            # The deadline can expire while publication is already committing:
            # report what actually happened.
            published = await disk_call(self.store.published, transfer_id)
            if published is not None:
                return JSONResponse({"uploaded": True, "size": published[0], "sha": published[1]})
            return self.problem(
                Failed(
                    "timeout",
                    "upload reached its transfer expiry or its time window "
                    "(ten minutes, longer for large files)",
                )
            )
        except (ValueError, UnicodeDecodeError):
            return self.problem(Refused("invalid_request", "invalid upload headers"))
        except Exception as exc:
            log.error(
                "upload failed (%s); no partial file published", type(exc).__name__
            )
            return self.problem(
                Failed("upload", "upload interrupted; inspect transfer status")
            )
        finally:
            if started:
                await disk_call(self.store.release, transfer_id)

    async def file(self, request, *, caller=None):
        # Every store and payload call here runs in a thread through disk_call,
        # as uploads do: the store lock is also taken by uploads, the MCP tools
        # and the expiry sweep, and the event loop must never wait for it (nor
        # for a slow disk read). disk_call also keeps a read in flight from
        # racing the close that follows a cancelled stream.
        browser = None if caller is not None else request.cookies.get(COOKIE, "")
        context = self.store.reader(
            request.path_params["transfer_id"], caller=caller, browser=browser
        )
        opened = []
        closed = False

        def enter():
            opened.append(context.__enter__())

        def close():
            nonlocal closed
            if opened and not closed:
                closed = True
                context.__exit__(None, None, None)

        try:
            try:
                await disk_call(enter)
                row, handle = opened[0]
                start, end, status = byte_range(
                    request.headers.get("range"), row["size"]
                )
                headers = {
                    "Content-Length": str(end - start),
                    "Accept-Ranges": "bytes",
                    "Content-Disposition": "attachment; filename=\"download\"; filename*=UTF-8''"
                    + quote(row["name"], safe=""),
                    "ETag": '"' + row["sha"] + '"',
                }
                if status == 206:
                    headers["Content-Range"] = f"bytes {start}-{end - 1}/{row['size']}"
                if request.method == "HEAD":
                    await disk_call(close)
                    return Response(
                        status_code=status,
                        headers=headers,
                        media_type="application/octet-stream",
                    )
                await disk_call(handle.seek, start)

                def still_valid():
                    return self.store.valid_stream(row["id"], caller=caller, browser=browser)

                async def stream():
                    remaining = end - start
                    while remaining:
                        if not await disk_call(still_valid):
                            raise RuntimeError(
                                "transfer expired or access revoked during stream"
                            )
                        chunk = await disk_call(handle.read, min(CHUNK, remaining))
                        if not chunk:
                            raise RuntimeError("published file incomplete")
                        remaining -= len(chunk)
                        yield chunk
                        await asyncio.sleep(0)

                return FileResponse(
                    stream(),
                    close=close,
                    deadline=row["claim_until"]
                    if row["state"] == "claimed"
                    else row["expires"],
                    clock=self.store.clock,
                    status_code=status,
                    headers=headers,
                    media_type="application/octet-stream",
                )
            except BaseException:
                await disk_call(close)
                raise
        except Outcome as exc:
            return self.problem(exc)

    async def download(self, request):
        return await self.file(request)

    async def collect(self, request):
        try:
            caller = await self.caller(request)
        except Outcome as exc:
            return self.problem(exc)
        return await self.file(request, caller=caller)
