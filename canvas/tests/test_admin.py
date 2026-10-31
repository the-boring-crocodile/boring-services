"""Admin operations run inside the container (admin.py), not over HTTP."""
from __future__ import annotations

import http.server
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from boring_canvas.admin import run, server_args, store_for
from boring_canvas.server import make_handler
from boring_canvas.storage import CanvasNotFound, CanvasStore

from canvas_fixtures import ClosesStores, new_canvas

SERVER_CMDLINE = b"\0".join([
    b"python3", b"-m", b"boring_canvas.server",
    b"--state-dir=/data/state", b"--host=0.0.0.0", b"--port=18510",
    b"--max-canvases=7", b"--create-rate-limit=3", b"--max-age-days=9.5", b"",
])


class ServerArgsTests(ClosesStores):
    def test_limits_come_from_the_server_command_line(self) -> None:
        args = server_args(SERVER_CMDLINE)
        self.assertEqual(args.state_dir, Path("/data/state"))
        self.assertEqual((args.max_canvases, args.create_rate_limit, args.max_age_days),
                         (7, 3, 9.5))

    def test_the_images_console_script_is_the_server_too(self) -> None:
        """As in production: the image runs the console script, not python -m."""
        live = b"/usr/local/bin/python\0/usr/local/bin/boring-canvas-server\0--state-dir=/data/state\0"
        self.assertEqual(server_args(live).state_dir, Path("/data/state"))
        self.assertEqual(server_args(b"/usr/local/bin/boring-canvas-server\0--max-canvases=7\0").max_canvases, 7)

    def test_another_process_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            server_args(b"/bin/sh\0-c\0sleep 1\0")


class RunTests(ClosesStores):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        argv = SERVER_CMDLINE.replace(b"/data/state", self.tmp.name.encode())
        self.store = store_for(server_args(argv))
        self.addCleanup(self.store.close)

    def test_list_shows_every_canvas(self) -> None:
        first, _ = new_canvas(self.store)
        second, _ = new_canvas(self.store)
        status, result = run("list", None, self.store)
        self.assertEqual(status, 0)
        self.assertEqual({c["canvas_id"] for c in result["canvases"]}, {first, second})

    def test_delete_removes_the_canvas(self) -> None:
        canvas_id, _ = new_canvas(self.store)
        kept, _ = new_canvas(self.store)
        self.assertEqual(run("delete", canvas_id, self.store), (0, {"ok": True, "deleted": canvas_id}))
        self.assertEqual({c["canvas_id"] for c in run("list", None, self.store)[1]["canvases"]}, {kept})
        self.assertEqual(run("delete", kept, self.store), (0, {"ok": True, "deleted": kept}))
        self.assertEqual(run("list", None, self.store)[1], {"canvases": []})

    def test_delete_of_an_unknown_id_says_so(self) -> None:
        status, result = run("delete", "f" * 32, self.store)
        self.assertEqual(status, 1)
        self.assertEqual(result["error"], "no such canvas")

    def test_limits_are_the_servers_and_the_window_is_not_guessed(self) -> None:
        new_canvas(self.store)
        status, result = run("limits", None, self.store)
        self.assertEqual(status, 0)
        self.assertEqual((result["max_canvases"], result["create_rate_limit"]), (7, 3))
        self.assertEqual(result["current_canvas_count"], 1)
        self.assertIsNone(result["creates_in_current_window"])


class NoAdminOverHttpTests(ClosesStores):
    def test_old_admin_routes_answer_not_found(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = self.open_store(Path(tmp.name))
        canvas_id, _ = new_canvas(store)
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, None))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        for method, path in [("GET", "/admin/canvases"), ("GET", "/admin/limits"),
                             ("DELETE", f"/admin/canvas/{canvas_id}")]:
            with self.subTest(method=method, path=path):
                request = urllib.request.Request(base + path, method=method)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
                self.assertIn(caught.exception.code, (404, 501))
                caught.exception.close()
        self.assertEqual(len(store.list_canvases()), 1)


class InFlightWrite:
    """A server write (browser save) held inside its transaction, after it
    has read the record -- the window the review's D7 interleaving used."""

    def __init__(self, store: CanvasStore, canvas_id: str, token: str) -> None:
        self.paused, self.go = threading.Event(), threading.Event()
        self.error: Exception | None = None
        real = store._record_edits

        def held(canvas_id, elements):
            if threading.current_thread() is self.thread:
                self.paused.set()
                self.go.wait(10)
            real(canvas_id, elements)

        def write() -> None:
            try:
                store.write_elements(canvas_id, token, "[]")
            except Exception as exc:  # reported by the test
                self.error = exc

        self.patch = mock.patch.object(store, "_record_edits", held)
        self.thread = threading.Thread(target=write, daemon=True)

    def __enter__(self) -> "InFlightWrite":
        self.patch.start()
        self.thread.start()
        assert self.paused.wait(5), "the write never reached its publish"
        return self

    def finish(self) -> None:
        self.go.set()
        self.thread.join(10)

    def __exit__(self, *exc: object) -> None:
        self.finish()
        self.patch.stop()


def admin_process(state_dir: str, *argv: str) -> subprocess.Popen:
    """boring-canvas-admin in its own process, as the container wrapper runs it."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
           "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.Popen([sys.executable, "-m", "boring_canvas.admin", "--state-dir", state_dir, *argv],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)


class AdminBesideTheServerTests(ClosesStores):
    """Admin writes and server writes are transactions on one database, across processes."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = self.open_store(Path(self.tmp.name))
        self.canvas_id, self.token = new_canvas(self.server)

    def assert_gone(self) -> None:
        with self.assertRaises(CanvasNotFound):
            self.server.verify_token(self.canvas_id, self.token)
        self.assertEqual(self.server.list_canvases(), [])

    def test_delete_in_another_store_waits_for_the_write_and_stays_done(self) -> None:
        admin_store = self.open_store(Path(self.tmp.name))
        outcome: list = []
        with InFlightWrite(self.server, self.canvas_id, self.token) as write:
            deleter = threading.Thread(target=lambda: outcome.append(run("delete", self.canvas_id, admin_store)))
            deleter.start()
            deleter.join(0.3)
            self.assertTrue(deleter.is_alive(), "delete did not wait for the write in flight")
            write.finish()
            deleter.join(10)
        self.assertIsNone(write.error)
        self.assertEqual(outcome, [(0, {"ok": True, "deleted": self.canvas_id})])
        self.assert_gone()

    def test_admin_process_delete_waits_for_the_write_and_stays_done(self) -> None:
        with InFlightWrite(self.server, self.canvas_id, self.token) as write:
            admin = admin_process(self.tmp.name, "delete", self.canvas_id)
            with self.assertRaises(subprocess.TimeoutExpired):
                admin.wait(timeout=1.5)  # waiting for the write's transaction
            write.finish()
            out, err = admin.communicate(timeout=30)
        self.assertEqual(admin.returncode, 0, err)
        self.assertEqual(json.loads(out), {"ok": True, "deleted": self.canvas_id})
        self.assertIsNone(write.error)
        self.assert_gone()

    def test_a_write_after_the_delete_finds_nothing(self) -> None:
        admin = admin_process(self.tmp.name, "delete", self.canvas_id)
        out, err = admin.communicate(timeout=30)
        self.assertEqual(admin.returncode, 0, err)
        with self.assertRaises(CanvasNotFound):
            self.server.write_elements(self.canvas_id, self.token, "[]")
        self.assert_gone()

    def test_token_reissue_is_not_undone_by_a_write_in_flight(self) -> None:
        name = self.server.read(self.canvas_id).name  # the server's reads wait for its write
        with InFlightWrite(self.server, self.canvas_id, self.token) as write:
            admin = admin_process(self.tmp.name, "token", name)
            with self.assertRaises(subprocess.TimeoutExpired):
                admin.wait(timeout=1.5)
            write.finish()
            out, err = admin.communicate(timeout=30)
        self.assertEqual(admin.returncode, 0, err)
        new = json.loads(out)["token"]
        self.assertIsNone(write.error)
        self.assertTrue(self.server.verify_token(self.canvas_id, new))
        self.assertFalse(self.server.verify_token(self.canvas_id, self.token))


OWNER = json.dumps(["https://crocodile-url.example.test", "pond"], separators=(",", ":"))  # an author as the kit forms it
PUBLIC = "https://crocodile-url.example.test"


class OperatorTokenTests(ClosesStores):
    """In safe mode the person gets the browser token from the operator."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = self.open_store(Path(self.tmp.name))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store, PUBLIC))
        self.httpd.RequestHandlerClass.log_message = lambda *args: None
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def operator(self, *argv: str) -> tuple[int, dict]:
        admin = admin_process(self.tmp.name, "--public-base-url", PUBLIC, *argv)
        out, err = admin.communicate(timeout=30)
        return admin.returncode, json.loads(out) if out.strip() else {"stderr": err}

    def sign_in(self, canvas_id: str, token: str) -> tuple[int, str | None]:
        """The page's form: POST the token to the clean URL's /session."""
        body = json.dumps({"token": token}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.httpd.server_address[1]}/canvas/{canvas_id}/session", data=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))}, method="POST")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.headers["Set-Cookie"]
        except urllib.error.HTTPError as error:
            error.close()
            return error.code, None

    def page(self, canvas_id: str, cookie: str) -> str:
        request = urllib.request.Request(f"http://127.0.0.1:{self.httpd.server_address[1]}/canvas/{canvas_id}",
                                         headers={"Cookie": cookie.split(";", 1)[0]})
        with urllib.request.urlopen(request) as response:
            return response.read().decode()

    def test_the_command_create_canvas_names_issues_a_token_that_opens_the_canvas(self) -> None:
        from boring_canvas.mcp_service import operator_token_command
        canvas_id, created_token = self.store.create_named(OWNER, "plan")  # what create_canvas does
        command = shlex.split(operator_token_command(OWNER, "plan"))
        self.assertEqual(command[:2], ["sudo", "boring-canvas-admin"])
        status, issued = self.operator(*command[2:])
        self.assertEqual(status, 0, issued)
        self.assertEqual(issued["url"], f"{PUBLIC}/canvas/{canvas_id}")  # the clean URL, no token in it
        self.assertNotIn(issued["token"], issued["url"])
        status, cookie = self.sign_in(canvas_id, issued["token"])
        self.assertEqual(status, 200)
        self.assertIn("window.__AUTHED__ = true;", self.page(canvas_id, cookie))
        self.assertEqual(self.sign_in(canvas_id, created_token)[0], 401)  # the discarded one never worked
        # Reissue: the new token works, the previous one is refused.
        status, again = self.operator("token", "plan")  # one owner uses the name: --owner may be left out
        self.assertEqual(status, 0, again)
        self.assertEqual(self.sign_in(canvas_id, again["token"])[0], 200)
        self.assertEqual(self.sign_in(canvas_id, issued["token"])[0], 401)
        # No plaintext token anywhere in the state.
        for path in Path(self.tmp.name).rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                for token in (created_token, issued["token"], again["token"]):
                    self.assertNotIn(token.encode(), content, path)

    def test_a_name_several_owners_use_needs_the_owner(self) -> None:
        self.store.create_named(OWNER, "plan")
        other, _ = self.store.create_named("garden", "plan")
        status, answer = self.operator("token", "plan")
        self.assertEqual(status, 2)
        self.assertEqual(answer["owners"], sorted([OWNER, "garden"]))
        status, answer = self.operator("token", "plan", "--owner", "garden")
        self.assertEqual((status, answer["canvas_id"]), (0, other))

    def test_an_unknown_name_says_so(self) -> None:
        self.assertEqual(self.operator("token", "nope")[0], 1)
        self.store.create_named(OWNER, "plan")
        self.assertEqual(self.operator("token", "plan", "--owner", "garden")[0], 1)


if __name__ == "__main__":
    unittest.main()
