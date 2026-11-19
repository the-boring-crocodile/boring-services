"""Canvas browser routes on the service's single ASGI listener."""
from __future__ import annotations

from urllib.parse import urlsplit
import asyncio
import json

BODY_READ_SECONDS = 30
import mimetypes
import secrets

from starlette.responses import Response, JSONResponse
from starlette.concurrency import run_in_threadpool
from .storage import CanvasNotFound, InvalidElements, InvalidToken
from .web_views import (render_page, page_csp, _TEMPLATE_PATH, ASSETS,
    _ROUTE_CANVAS, _ROUTE_SESSION, _ROUTE_VERSION, _ROUTE_ELEMENTS,
    COOKIE_NAME, MAX_BODY_BYTES, _CONTENT_LENGTH)


class BodyRefused(Exception):
    def __init__(self, status, message):
        self.response = JSONResponse({"error": message}, status_code=status)


async def text_body(request):
    raw = request.headers.get("content-length")
    if raw is None or "transfer-encoding" in request.headers:
        raise BodyRefused(411, "Content-Length required (chunked bodies are not supported)")
    if not _CONTENT_LENGTH.fullmatch(raw.strip()):
        raise BodyRefused(400, f"invalid Content-Length {raw!r}")
    length = int(raw)
    if length > MAX_BODY_BYTES:
        raise BodyRefused(413, f"body of {length} bytes is over the limit of {MAX_BODY_BYTES} bytes")
    body = bytearray()
    try:
        async with asyncio.timeout(BODY_READ_SECONDS):
            async for chunk in request.stream():
                if len(body) + len(chunk) > length:
                    raise BodyRefused(400, "body exceeds Content-Length")
                body.extend(chunk)
    except TimeoutError:
        raise BodyRefused(408, "body read timed out") from None
    if len(body) != length:
        raise BodyRefused(400, "body shorter than Content-Length")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise BodyRefused(400, "body must be UTF-8") from None


class CanvasWeb:
    def __init__(self, store, public_base_url):
        self.store = store
        self.public_base_url = public_base_url
        self.prefix = urlsplit(public_base_url).path.rstrip("/")

    async def __call__(self, request):
        try:
            path = request.url.path
            if self.prefix:
                if not path.startswith(self.prefix + "/"):
                    return JSONResponse({"error": "not found"}, status_code=404)
                path = path[len(self.prefix):]
            if request.method == "GET":
                if path.startswith("/canvas/assets/"):
                    return await run_in_threadpool(self.asset, path[len("/canvas/assets/"):])
                match = _ROUTE_CANVAS.fullmatch(path)
                if match:
                    return await run_in_threadpool(self.page, match[1], request.cookies.get(COOKIE_NAME))
                match = _ROUTE_VERSION.fullmatch(path)
                if match:
                    return await run_in_threadpool(self.version, match[1], request.cookies.get(COOKIE_NAME))
            if request.method == "POST":
                match = _ROUTE_SESSION.fullmatch(path)
                if match:
                    return await self.session(request, match[1])
                match = _ROUTE_ELEMENTS.fullmatch(path)
                if match:
                    return await self.elements(request, match[1])
            return JSONResponse({"error": "not found"}, status_code=404)
        except CanvasNotFound:
            return JSONResponse({"error": "no such canvas"}, status_code=404)
        except BodyRefused as error:
            # Close instead of attempting to consume a rejected/unbounded body.
            error.response.headers["connection"] = "close"
            return error.response

    async def session(self, request, canvas_id):
        text = await text_body(request)
        try:
            payload = json.loads(text or "{}")
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        token = payload.get("token")
        if not isinstance(token, str) or not token or not await run_in_threadpool(self.store.verify_token, canvas_id, token):
            return JSONResponse({"error": "invalid token"}, status_code=401)
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE_NAME, token, path=f"{self.prefix}/canvas/{canvas_id}",
            httponly=True, samesite="Lax", secure=self.public_base_url.startswith("https://"))
        return response

    def page(self, canvas_id, token):
        canvas = self.store.read(canvas_id)
        authed = bool(token) and self.store.verify_token(canvas_id, token)
        nonce = secrets.token_urlsafe(18)
        body = render_page(_TEMPLATE_PATH.read_text(encoding="utf-8"),
            name=canvas.name, canvas_id=canvas_id, authed=authed,
            scene_svg=canvas.scene_svg, elements_json=canvas.elements_json, nonce=nonce,
            base_path=self.prefix)
        return Response(body, media_type="text/html", headers={
            "Referrer-Policy": "no-referrer", "Content-Security-Policy": page_csp(nonce),
            "X-Content-Type-Options": "nosniff"})

    def version(self, canvas_id, token):
        if not token or not self.store.verify_token(canvas_id, token):
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        return JSONResponse({"version": self.store.scene_version(canvas_id)})

    async def elements(self, request, canvas_id):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        if not await run_in_threadpool(self.store.verify_token, canvas_id, token):
            return JSONResponse({"error": "invalid token"}, status_code=401)
        # Authenticate before receiving an up-to-8MiB body.
        body = await text_body(request)
        try:
            await run_in_threadpool(self.store.write_elements, canvas_id, token, body)
        except InvalidToken:
            return JSONResponse({"error": "invalid token"}, status_code=401)
        except InvalidElements as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        return JSONResponse({"ok": True})

    def asset(self, name):
        try:
            path = (ASSETS / name).resolve()
            path.relative_to(ASSETS.resolve())
            if not path.is_file():
                raise ValueError("not an asset")
            body = path.read_bytes()
        except (OSError, ValueError):
            return JSONResponse({"error": "not found"}, status_code=404)
        return Response(body, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})
