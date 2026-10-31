"""Canvas HTTP request adapter with explicit store and deployment settings."""
from __future__ import annotations
import http.cookies
import http.server
import json
import secrets
import mimetypes
from .storage import CanvasNotFound, CanvasStore, InvalidElements, InvalidToken
from .web_views import (render_page, page_csp, without_query, _TEMPLATE_PATH, ASSETS, _ROUTE_CANVAS, _ROUTE_SESSION, _ROUTE_VERSION, _ROUTE_ELEMENTS, COOKIE_NAME, MAX_BODY_BYTES, _CONTENT_LENGTH)

class CanvasHandler(http.server.BaseHTTPRequestHandler):
    store: CanvasStore
    public_base_url: str | None
    server_version = "boring-canvas/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print("%s - %s" % (self.address_string(), without_query(fmt % args)))

    def _base_url(self) -> str:
        """Canvas's own belief about its externally-visible address.

        The one affected owns the information: this is
        canvas's own deploy-time setting, not something a caller (the
        MCP) has to know or compose -- same reasoning as
        Nextcloud's own overwrite.cli.url. When set explicitly
        (--public-base-url), that value wins outright, since it's the
        one case where Caddy/a domain sits in front and the request's
        own Host header can't be trusted to reflect that. Otherwise
        falls back to the incoming request's Host header -- correct for
        tonight's LAN testing, where --host is usually 0.0.0.0 (not
        itself a connectable address) and the right base genuinely
        depends on which interface someone actually connected through.
        """
        if self.public_base_url:
            return self.public_base_url
        host = self.headers.get("Host", self.server.server_address[0])
        return f"http://{host}"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes | None:
        """The request body, or None once a refusal has been sent: 411
        without Content-Length (chunked bodies are not supported), 400
        for one that is not a number, 413 above MAX_BODY_BYTES. A refused
        body is never read; the connection is closed instead."""
        raw = self.headers.get("Content-Length")
        if raw is None or "Transfer-Encoding" in self.headers:
            self._refuse_body(411, "Content-Length required (chunked bodies "
                                   "are not supported)")
            return None
        if not _CONTENT_LENGTH.fullmatch(raw.strip()):
            self._refuse_body(400, f"invalid Content-Length {raw!r}")
            return None
        length = int(raw)
        if length > MAX_BODY_BYTES:
            self._refuse_body(413, f"body of {length} bytes is over the limit "
                                   f"of {MAX_BODY_BYTES} bytes")
            return None
        return self.rfile.read(length) if length else b""

    def _read_text_body(self) -> str | None:
        raw = self._read_body()
        if raw is None:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            self._send_json(400, {"error": "body must be UTF-8"})
            return None

    def _refuse_body(self, status: int, message: str) -> None:
        # The unread body would otherwise be parsed as the next request.
        self.close_connection = True
        self._send_json(status, {"error": message})

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar: http.cookies.SimpleCookie = http.cookies.SimpleCookie()
        jar.load(raw)
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _set_session_cookie_header(self, canvas_id: str, token: str) -> None:
        cookie: http.cookies.SimpleCookie = http.cookies.SimpleCookie()
        cookie[COOKIE_NAME] = token
        cookie[COOKIE_NAME]["path"] = f"/canvas/{canvas_id}"
        cookie[COOKIE_NAME]["httponly"] = True
        # SameSite=Lax: the session cookie only needs to ride along on
        # top-level navigation to /canvas/<id> (typing/clicking the
        # link) and same-site requests from canvas.html's own JS; it
        # has no business being attached to a cross-site POST/fetch,
        # so Lax closes that off without breaking the normal flow.
        cookie[COOKIE_NAME]["samesite"] = "Lax"
        # Secure: canvas itself always speaks plain HTTP (TLS is
        # Caddy's job, terminated in front of it -- see _base_url()'s
        # docstring), so "are we HTTPS" isn't something this process
        # can read off its own socket. _base_url() is already the
        # single source of truth for canvas's externally-visible
        # scheme (explicit --public-base-url when given, else the
        # request's own Host header over plain http://), so asking it
        # is more honest than re-deriving the scheme here. This was
        # genuinely open (no Secure flag) during LAN-only testing;
        # Behind a TLS proxy --public-base-url is https://, and this
        # turns on.
        if self._base_url().startswith("https://"):
            cookie[COOKIE_NAME]["secure"] = True
        self.send_header("Set-Cookie", cookie[COOKIE_NAME].OutputString())

    # ---- POST /canvas/<id>/session ----
    def _create_session(self, canvas_id: str) -> None:
        body = self._read_text_body()
        if body is None:
            return
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "body must be a JSON object"})
            return
        token = payload.get("token")
        if not isinstance(token, str) or not token or not self.store.verify_token(canvas_id, token):
            self._send_json(401, {"error": "invalid token"})
            return
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._set_session_cookie_header(canvas_id, token)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- GET /canvas/<id> ----
    def _serve_page(self, canvas_id: str) -> None:
        try:
            canvas = self.store.read(canvas_id)
        except CanvasNotFound:
            self._send_json(404, {"error": "no such canvas"})
            return
        token = self._cookie_token()
        authed = bool(token) and self.store.verify_token(canvas_id, token)
        nonce = secrets.token_urlsafe(18)
        html = render_page(
            _TEMPLATE_PATH.read_text(encoding="utf-8"),
            name=canvas.name,
            canvas_id=canvas_id,
            authed=authed,
            scene_svg=canvas.scene_svg,
            elements_json=canvas.elements_json,
            nonce=nonce,
        )
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # The page loads its scripts from a CDN: no Referer carrying a
        # canvas URL (with or without query) to third parties.
        self.send_header("Referrer-Policy", "no-referrer")
        # Only our own nonce'd scripts and the pinned CDN run; the page
        # is never framed. Excalidraw needs inline styles,
        # data:/blob: images and its fonts from the CDN.
        self.send_header("Content-Security-Policy", page_csp(nonce))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- GET /canvas/<id>/version ----
    def _get_version(self, canvas_id: str) -> None:
        token = self._cookie_token()
        try:
            authed = bool(token) and self.store.verify_token(canvas_id, token)
        except CanvasNotFound:
            self._send_json(404, {"error": "no such canvas"})
            return
        if not authed:
            self._send_json(401, {"error": "not authenticated"})
            return
        self._send_json(200, {"version": self.store.scene_version(canvas_id)})

    # ---- POST /canvas/<id>/elements (cookie -- browser saves itself) ----
    def _post_elements(self, canvas_id: str) -> None:
        token = self._cookie_token()
        if not token:
            self._send_json(401, {"error": "not authenticated"})
            return
        try:  # the token is checked before an up-to-8-MiB body is read
            if not self.store.verify_token(canvas_id, token):
                self._send_json(401, {"error": "invalid token"})
                return
        except CanvasNotFound:
            self._send_json(404, {"error": "no such canvas"})
            return
        body = self._read_text_body()
        if body is None:
            return
        try:
            self.store.write_elements(canvas_id, token, body)
        except CanvasNotFound:
            self._send_json(404, {"error": "no such canvas"})
            return
        except InvalidToken:
            self._send_json(401, {"error": "invalid token"})
            return
        except InvalidElements as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, {"ok": True})

    def _answer(self, route) -> None:
        """Run a route; a canvas deleted between route match and read (or an
        unknown id on a route that does not check itself, /session) is 404
        like everywhere else."""
        try:
            route()
        except CanvasNotFound:
            self._send_json(404, {"error": "no such canvas"})

    def do_GET(self) -> None:
        self._answer(self._route_get)

    def do_POST(self) -> None:
        self._answer(self._route_post)

    def _asset(self, name: str) -> None:
        try:
            path = (ASSETS / name).resolve()
            path.relative_to(ASSETS.resolve())
            if not path.is_file():
                raise ValueError("not an asset")
            body = path.read_bytes()
        except (OSError, ValueError):
            self._send_json(404, {"error": "not found"})
            return
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _route_get(self) -> None:
        # self.path carries the query string verbatim (e.g.
        # "/canvas/<id>?token=...") -- every route regex below assumes
        # a bare path with no query string, so routing has to match
        # against the path component alone. A real bug, found live
        # the moment ?token=... existed for the first time tonight:
        # every route silently 404'd, since the regexes' trailing $
        # anchor failed the instant anything followed the id. Query
        # params that ARE needed (the unsafe-url-auth token) are
        # parsed separately, from the full self.path, inside the
        # handler that actually needs them (_serve_page).
        path = self.path.split("?", 1)[0]
        if path.startswith("/canvas/assets/"):
            self._asset(path[len("/canvas/assets/"):])
            return
        match = _ROUTE_VERSION.match(path)
        if match:
            self._get_version(match.group(1))
            return
        match = _ROUTE_CANVAS.match(path)
        if match:
            self._serve_page(match.group(1))
            return
        self._send_json(404, {"error": "not found"})

    def _route_post(self) -> None:
        path = self.path.split("?", 1)[0]  # same reasoning as _route_get
        # No route creates a canvas: canvases are created over MCP only, by
        # a verified caller. POST /canvas is an
        # unknown route like any other.
        match = _ROUTE_SESSION.match(path)
        if match:
            self._create_session(match.group(1))
            return
        match = _ROUTE_ELEMENTS.match(path)
        if match:
            self._post_elements(match.group(1))
            return
        self._send_json(404, {"error": "not found"})
