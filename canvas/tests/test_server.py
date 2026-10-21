"""Unit tests for small pure helpers in server.py."""
from __future__ import annotations

import unittest
from importlib.resources import files

import http.client
import json
import re
import socketserver
import tempfile
import threading
from pathlib import Path

from boring_canvas.server import make_handler
from boring_canvas.web_views import MAX_BODY_BYTES, page_title, render_page, script_json
from boring_canvas.storage import CanvasStore

from canvas_fixtures import legacy_ownerless_canvas, new_canvas

TEMPLATE = files("boring_canvas").joinpath("templates/canvas.html").read_text(encoding="utf-8")
CANVAS_ID = "0" * 32
BREAKOUT = "</script><script>alert(document.domain)</script>"


class PageTitleTests(unittest.TestCase):
    def test_signed_in_named_canvas_shows_its_name(self) -> None:
        self.assertEqual(page_title("arch-monet", True), "arch-monet \u00b7 Boring Canvas")

    def test_not_signed_in_shows_nothing_about_the_canvas(self) -> None:
        self.assertEqual(page_title("arch-monet", False), "Boring Canvas")

    def test_unnamed_canvas(self) -> None:
        self.assertEqual(page_title(None, True), "Boring Canvas")

    def test_name_is_escaped(self) -> None:
        self.assertEqual(page_title("<b>", True), "&lt;b&gt; \u00b7 Boring Canvas")


def _script_value(page: str, name: str):
    """The JSON value the page assigns to window.<name>, as the browser reads it."""
    match = re.search(r"window\.%s = (.*);\n" % re.escape(name), page)
    assert match, name
    return json.loads(match.group(1))


def _render(**overrides) -> str:
    args = dict(
        name="arch-monet",
        canvas_id=CANVAS_ID,
        authed=True,
        scene_svg="<svg xmlns='http://www.w3.org/2000/svg'/>",
        elements_json="[]",
    )
    args.update(overrides)
    return render_page(TEMPLATE, **args)


class ScriptJsonTests(unittest.TestCase):
    def test_markup_characters_are_escaped_and_the_value_is_unchanged(self) -> None:
        text = json.dumps([{"text": BREAKOUT + " & \u2028\u2029"}])
        safe = script_json(text)
        for raw in ("<", ">", "&", "\u2028", "\u2029"):
            self.assertNotIn(raw, safe)
        self.assertEqual(json.loads(safe), json.loads(text))

    def test_escaped_backslash_before_markup_stays_correct(self) -> None:
        text = json.dumps(["\\</script>"])
        self.assertEqual(json.loads(script_json(text)), ["\\</script>"])


class RenderPageTests(unittest.TestCase):
    def test_stored_element_text_cannot_end_the_script(self) -> None:
        elements = json.dumps([{"type": "text", "text": BREAKOUT}])
        page = _render(elements_json=elements)
        self.assertEqual(page.count("</script>"), TEMPLATE.count("</script>"))
        self.assertEqual(_script_value(page, "__ELEMENTS__"), json.loads(elements))

    def test_slot_names_inside_values_stay_text(self) -> None:
        marker = "%%ELEMENTS_JSON%%"
        elements = json.dumps([{"text": "kept"}])
        page = _render(
            name=marker,
            scene_svg="<svg><text>" + marker + "</text></svg>",
            elements_json=elements,
        )
        self.assertIn("<title>" + marker + " \u00b7 Boring Canvas</title>", page)
        self.assertEqual(
            _script_value(page, "__SCENE_SVG__"), "<svg><text>" + marker + "</text></svg>"
        )
        self.assertEqual(_script_value(page, "__ELEMENTS__"), [{"text": "kept"}])

    def test_signed_out_page_carries_no_canvas_data(self) -> None:
        page = _render(authed=False, elements_json=json.dumps([{"text": "secret"}]))
        self.assertIsNone(_script_value(page, "__SCENE_SVG__"))
        self.assertIsNone(_script_value(page, "__ELEMENTS__"))
        self.assertNotIn("secret", page)
        self.assertIn("<title>Boring Canvas</title>", page)

    def test_every_template_slot_is_filled(self) -> None:
        self.assertNotRegex(_render(), r"%%[A-Z_]+%%")


class LiveServerTestCase(unittest.TestCase):
    """The real handler on 127.0.0.1, an ephemeral port and a temp state dir."""

    public_base_url: str | None = None
    unsafe_url_auth = False

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanvasStore(Path(self._tmp.name))
        handler = make_handler(self.store, self.public_base_url, self.unsafe_url_auth)
        handler.log_message = lambda *args: None  # type: ignore[method-assign]
        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self._thread = threading.Thread(target=self.httpd.serve_forever, args=(0.05,),
                                        daemon=True)
        self._thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join()
        self._tmp.cleanup()

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict[str, str] | None = None
                ) -> tuple[int, http.client.HTTPMessage, bytes]:
        """One request with exactly the given headers (no Content-Length
        is added); the body, if any, is sent as is."""
        conn = http.client.HTTPConnection(*self.httpd.server_address, timeout=5)
        try:
            conn.putrequest(method, path, skip_accept_encoding=True)
            for key, value in (headers or {}).items():
                conn.putheader(key, value)
            conn.endheaders(body)
            response = conn.getresponse()
            return response.status, response.headers, response.read()
        finally:
            conn.close()


SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>'


class MalformedInputTests(LiveServerTestCase):
    def post(self, canvas_id, route, body, **headers):
        return self.request("POST", f"/canvas/{canvas_id}/{route}", body,
                            {"Content-Length": str(len(body)), **headers})

    def test_session_refuses_wrong_shapes_and_unknown_canvas(self):
        canvas_id, _ = new_canvas(self.store)
        for body, expected in [(b"[]", 400), (b"null", 400),
                               (b'{"token": 123}', 401), (b"\xff", 400)]:
            with self.subTest(body=body):
                status, _, _ = self.post(canvas_id, "session", body)
                self.assertEqual(status, expected)
        status, _, _ = self.post("f" * 32, "session", b'{"token":"wrong"}')
        self.assertEqual(status, 404)

    def test_invalid_utf8_is_refused_without_changing_scene(self):
        canvas_id, token = new_canvas(self.store)
        for route, headers in [
            ("scene", {"Authorization": f"Bearer {token}"}),
            ("elements", {"Cookie": f"canvas_token={token}"}),
        ]:
            with self.subTest(route=route):
                status, _, _ = self.post(canvas_id, route, b"\xff", **headers)
                self.assertEqual(status, 400)
        canvas = self.store.read(canvas_id)
        self.assertIsNone(canvas.scene_svg)
        self.assertIsNone(canvas.elements_json)

    def test_opaque_elements_with_malformed_ids_do_not_break_edit_tracking(self):
        canvas_id, token = new_canvas(self.store)
        elements = [{"id": {"unexpected": "object"}, "version": 1},
                    {"id": ["unexpected"], "version": 2},
                    {"id": "valid", "version": 1}]
        body = json.dumps(elements).encode()
        status, _, _ = self.post(canvas_id, "elements", body,
                                Cookie=f"canvas_token={token}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(self.store.read(canvas_id).elements_json), elements)
        self.assertEqual([r["element_id"] for r in self.store.get_edits(canvas_id)],
                         ["valid"])


class RequestBodyTests(LiveServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.canvas_id, self.token = new_canvas(self.store)
        self.auth = {"Authorization": f"Bearer {self.token}"}

    def push(self, headers: dict[str, str], body: bytes | None = None):
        return self.request("POST", f"/canvas/{self.canvas_id}/scene", body,
                            dict(self.auth, **headers))

    def test_body_within_the_limit_is_read(self) -> None:
        status, _, _ = self.push({"Content-Length": str(len(SVG))}, SVG)
        self.assertEqual(status, 200)
        self.assertIn("circle", self.store.read(self.canvas_id).scene_svg)

    def test_oversized_body_is_refused_without_reading_it(self) -> None:
        # No body is sent at all: a server that tried to read it would wait
        # until the client timeout instead of answering.
        status, _, body = self.push({"Content-Length": str(MAX_BODY_BYTES + 1)})
        self.assertEqual(status, 413)
        self.assertIn("limit", json.loads(body)["error"])
        self.assertIsNone(self.store.read(self.canvas_id).scene_svg)

    def test_missing_content_length_is_411(self) -> None:
        status, _, body = self.push({})
        self.assertEqual(status, 411)
        self.assertIn("Content-Length", json.loads(body)["error"])

    def test_chunked_body_is_411(self) -> None:
        # Headers only, as above: the refusal never reads a body, and on Windows
        # a body left unread makes the server's close a reset before the 411.
        status, _, _ = self.push({"Transfer-Encoding": "chunked"})
        self.assertEqual(status, 411)
        self.assertIsNone(self.store.read(self.canvas_id).scene_svg)

    def test_invalid_content_length_is_400(self) -> None:
        for value in ("abc", "-1", "1e3", "²"):
            with self.subTest(value=value):
                status, _, body = self.push({"Content-Length": value})
                self.assertEqual(status, 400)
                self.assertIn("Content-Length", json.loads(body)["error"])

    def test_limit_applies_to_every_body_route(self) -> None:
        big = {"Content-Length": str(MAX_BODY_BYTES + 1)}
        cookie = {"Cookie": f"canvas_token={self.token}"}
        for path, headers in ((f"/canvas/{self.canvas_id}/session", big),
                              (f"/canvas/{self.canvas_id}/elements", dict(big, **cookie))):
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", path, None, headers)[0], 413)


class CorruptCanvasTests(LiveServerTestCase):
    """Every web route on a corrupt canvas file answers a clean JSON 500."""

    def setUp(self) -> None:
        super().setUp()
        self.canvas_id, self.token = new_canvas(self.store)
        (self.store.state_dir / f"{self.canvas_id}.json").write_text("{not json")

    def test_every_route_answers_500_with_a_pointer_to_the_admin_list(self) -> None:
        cid, body = self.canvas_id, SVG
        bearer = {"Authorization": f"Bearer {self.token}"}
        cookie = {"Cookie": f"canvas_token={self.token}"}
        length = {"Content-Length": str(len(body))}
        session = json.dumps({"token": self.token}).encode()
        cases = [
            ("GET", f"/canvas/{cid}", None, cookie),
            ("GET", f"/canvas/{cid}/version", None, cookie),
            ("GET", f"/canvas/{cid}/elements", None, bearer),
            ("GET", f"/canvas/{cid}/edits", None, bearer),
            ("POST", f"/canvas/{cid}/session", session, {"Content-Length": str(len(session))}),
            ("POST", f"/canvas/{cid}/scene", body, dict(bearer, **length)),
            ("POST", f"/canvas/{cid}/elements", b"[]", dict(cookie, **{"Content-Length": "2"})),
            ("POST", f"/canvas/{cid}/rotate", None, bearer),
        ]
        for method, path, payload, headers in cases:
            with self.subTest(method=method, path=path):
                status, response_headers, raw = self.request(method, path, payload, headers)
                self.assertEqual(status, 500)
                self.assertEqual(response_headers["Content-Type"], "application/json")
                error = json.loads(raw)["error"]
                self.assertIn("corrupt", error)
                self.assertIn("boring-canvas-admin list", error)
                self.assertNotIn("Traceback", error)
                self.assertNotIn("JSON", error)  # the reason stays in the log
        # No route repaired, replaced or removed the corrupt file: it waits for the operator.
        self.assertEqual((self.store.state_dir / f"{cid}.json").read_text(), "{not json")

    def test_session_for_unknown_canvas_is_404(self) -> None:
        payload = json.dumps({"token": "x"}).encode()
        status, _, _ = self.request("POST", f"/canvas/{'1' * 32}/session", payload,
                                    {"Content-Length": str(len(payload))})
        self.assertEqual(status, 404)


class UrlAuthTests(LiveServerTestCase):
    """--unsafe-url-auth: links carry the token in the fragment; old query links still sign in (a 303 swaps the token for the cookie) for one release."""

    unsafe_url_auth = True

    def setUp(self) -> None:
        super().setUp()
        self.canvas_id, self.token = new_canvas(self.store)
        self.page = f"/canvas/{self.canvas_id}"

    def test_the_page_signs_in_from_the_link_fragment_and_clears_it(self) -> None:
        """open_url carries the token in the fragment (never sent to the server); the page
        posts it to /session like the form and removes it from the address bar first."""
        status, _, body = self.request("GET", self.page, None, {})
        self.assertEqual(status, 200)
        page = body.decode()
        self.assertIn('fragment.get("this-is-unsafe-dont-do-this-auth")', page)
        self.assertIn("history.replaceState", page)
        self.assertLess(page.index("history.replaceState"), page.index("JSON.stringify({ token: linked })"))

    def open(self, token: str, cookie: str | None = None):
        headers = {"Cookie": f"canvas_token={cookie}"} if cookie else {}
        return self.request("GET", f"{self.page}?this-is-unsafe-dont-do-this-auth={token}",
                            None, headers)

    def test_valid_url_token_redirects_to_the_bare_path_with_the_cookie(self) -> None:
        status, headers, body = self.open(self.token)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], self.page)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(body, b"")
        cookie = headers["Set-Cookie"]
        self.assertTrue(cookie.startswith(f"canvas_token={self.token};"), cookie)
        for attribute in ("HttpOnly", f"Path={self.page}", "SameSite=Lax"):
            self.assertIn(attribute, cookie)
        self.assertNotIn("Secure", cookie)  # plain http base URL
        # Following the redirect with that cookie: signed in, token not in the URL.
        status, headers, page = self.request("GET", headers["Location"], None,
                                             {"Cookie": f"canvas_token={self.token}"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("window.__AUTHED__ = true;", page.decode())

    def test_other_query_parameters_survive_the_redirect(self) -> None:
        status, headers, _ = self.request(
            "GET", f"{self.page}?debug=1&this-is-unsafe-dont-do-this-auth={self.token}")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], f"{self.page}?debug=1")

    def test_valid_url_token_replaces_a_stale_cookie(self) -> None:
        status, headers, _ = self.open(self.token, cookie="stale")
        self.assertEqual(status, 303)
        self.assertIn(f"canvas_token={self.token};", headers["Set-Cookie"])

    def test_invalid_url_token_serves_the_signed_out_page(self) -> None:
        status, headers, page = self.open("wrong")
        self.assertEqual(status, 200)
        self.assertIsNone(headers["Set-Cookie"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("window.__AUTHED__ = false;", page.decode())

    def test_invalid_url_token_with_valid_cookie_stays_signed_in(self) -> None:
        status, headers, page = self.open("wrong", cookie=self.token)
        self.assertEqual(status, 200)
        self.assertIsNone(headers["Set-Cookie"])
        self.assertIn("window.__AUTHED__ = true;", page.decode())


class PagePolicyTests(LiveServerTestCase):
    """A per-response nonce for our scripts, no framing.
    The browser side (no violations while loading, drawing, pushing,
    exporting) was checked with Firefox via Playwright."""

    def test_every_script_carries_this_responses_nonce(self) -> None:
        canvas_id, token = new_canvas(self.store)
        seen = set()
        for _ in range(2):
            status, headers, page = self.request("GET", f"/canvas/{canvas_id}", None,
                                                 {"Cookie": f"canvas_token={token}"})
            self.assertEqual(status, 200)
            policy = headers["Content-Security-Policy"]
            nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", policy).group(1)
            seen.add(nonce)
            text = page.decode()
            self.assertEqual(text.count("<script"), text.count(f'<script nonce="{nonce}"'))
            self.assertNotIn("%%NONCE%%", text)
            for directive in ("default-src 'none'", "frame-ancestors 'none'",
                              "base-uri 'none'", "object-src 'none'"):
                self.assertIn(directive, policy)
            self.assertNotIn("unsafe-eval", policy)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(len(seen), 2)  # a fresh nonce per response


class UrlAuthHttpsTests(LiveServerTestCase):
    unsafe_url_auth = True
    public_base_url = "https://canvas.example"

    def test_cookie_on_the_redirect_keeps_secure(self) -> None:
        canvas_id, token = new_canvas(self.store)
        status, headers, _ = self.request(
            "GET", f"/canvas/{canvas_id}?this-is-unsafe-dont-do-this-auth={token}")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], f"/canvas/{canvas_id}")
        self.assertIn("Secure", headers["Set-Cookie"])


class UrlAuthOffTests(LiveServerTestCase):
    def test_url_token_is_ignored_without_the_flag(self) -> None:
        canvas_id, token = new_canvas(self.store)
        status, headers, page = self.request(
            "GET", f"/canvas/{canvas_id}?this-is-unsafe-dont-do-this-auth={token}")
        self.assertEqual(status, 200)
        self.assertIsNone(headers["Set-Cookie"])
        self.assertIn("window.__AUTHED__ = false;", page.decode())


class NoWebCreateTests(LiveServerTestCase):
    """Canvases are created over MCP only."""

    def state_files(self) -> list[str]:
        return sorted(p.name for p in self.store.state_dir.rglob("*") if p.is_file())

    def test_post_canvas_creates_nothing_and_answers_404(self) -> None:
        new_canvas(self.store)
        before_files, before_count = self.state_files(), self.store._canvas_count()
        for path in ("/canvas", "/canvas/", "/canvas?x=1"):
            with self.subTest(path=path):
                status, headers, body = self.request("POST", path, b"",
                                                     {"Content-Length": "0"})
                self.assertEqual(status, 404)
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertEqual(json.loads(body), {"error": "not found"})
        self.assertEqual(self.state_files(), before_files)
        self.assertEqual(self.store._canvas_count(), before_count)
        self.assertEqual(self.store.limits_status()["creates_in_current_window"], 1)

    def test_get_canvas_is_404_too(self) -> None:
        status, _, body = self.request("GET", "/canvas")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not found"})

    def test_an_old_ownerless_canvas_still_works_on_the_web_port(self) -> None:
        canvas_id, token = legacy_ownerless_canvas(self.store)
        status, _, page = self.request("GET", f"/canvas/{canvas_id}", None,
                                       {"Cookie": f"canvas_token={token}"})
        self.assertEqual(status, 200)
        self.assertIn("window.__AUTHED__ = true;", page.decode())
        status, _, _ = self.request("POST", f"/canvas/{canvas_id}/scene", SVG, {
            "Authorization": f"Bearer {token}", "Content-Length": str(len(SVG))})
        self.assertEqual(status, 200)
        self.assertIn("circle", self.store.read(canvas_id).scene_svg)


class PushedDrawingTest(unittest.TestCase):
    """The pushed drawing is inserted locked, so hand drawing cannot drag it."""

    def test_pushed_image_element_is_locked(self):
        start = TEMPLATE.index("const imageElement = {")
        element = TEMPLATE[start:TEMPLATE.index("};", start)]
        self.assertIn('id: PUSHED_IMAGE_ID', element)
        self.assertIn("locked: true,", element)
        self.assertNotIn("locked: false", element)


if __name__ == "__main__":
    unittest.main()


class LogTests(unittest.TestCase):
    def test_the_log_never_shows_a_query_string(self) -> None:
        from boring_canvas.web_views import without_query
        line = '"GET /canvas/0123?this-is-unsafe-dont-do-this-auth=SECRET HTTP/1.1" 200 -'
        self.assertEqual(without_query(line), '"GET /canvas/0123?… HTTP/1.1" 200 -')
        self.assertNotIn("SECRET", without_query(line))
        self.assertEqual(without_query('"GET /canvas/0123 HTTP/1.1" 200 -'), '"GET /canvas/0123 HTTP/1.1" 200 -')
        self.assertNotIn("SECRET", without_query('"GET /c?a"b&this-is-unsafe-dont-do-this-auth=SECRET HTTP/1.1"'))
