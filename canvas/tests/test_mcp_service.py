"""MCP service tests: real SDK app over httpx2's ASGI transport, real HTTP to
a fake control plane (a stdlib server in a thread). Needs mcp 2.2.0 and the
vendored boring-registry on the path:

    PYTHONPATH=src python -m pytest tests   # with requirements.lock installed
"""
from __future__ import annotations

import asyncio
import base64
import http.server
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx2
from boring_registry import Watchdog

from boring_service_kit.http import ServiceConfig
from boring_service_kit.pond import ControlPlane, Registration

from boring_canvas.mcp_service import MAX_ANSWER_BYTES, build_server, compact_elements
from boring_canvas.storage import CanvasStore

from canvas_fixtures import legacy_ownerless_canvas

OWN_URL = "http://127.0.0.1:18511/mcp"
PUBLIC = "https://canvas.example"
CLIENT_ID, SECRET = "canvas-client", "canvas-secret"
VERSION = "2026-07-28"


class FakeControlPlane:
    """/introspect answers from `tokens`; /services/register with `register`."""

    def __init__(self) -> None:
        self.tokens: dict[str, dict[str, Any]] = {}
        self.introspect_status = 200
        self.register: tuple[int, bytes] = (200, b"{}")
        self.requests: list[dict[str, Any]] = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                fake.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                      "body": body})
                if self.path == "/introspect":
                    token = parse_qs(body.decode())["token"][0]
                    status = fake.introspect_status
                    payload = json.dumps(fake.tokens.get(token, {"active": False})).encode()
                elif self.path == "/services/register":
                    status, payload = fake.register
                else:
                    status, payload = 404, b"{}"
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def paths(self) -> list[str]:
        return [r["path"] for r in self.requests]


def _basic() -> str:
    return "Basic " + base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()


def _meta(client: str = "tester") -> dict[str, Any]:
    return {"io.modelcontextprotocol/protocolVersion": VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": client, "version": "1"}}


def _headers(method: str, name: str | None = None, token: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "MCP-Protocol-Version": VERSION, "Mcp-Method": method}
    if name is not None:
        headers["Mcp-Name"] = name
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _call_body(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments, "_meta": _meta()}}


def _discover_body(client: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "server/discover",
            "params": {"_meta": _meta(client)}}


def _element(id_: str, type_: str, **fields: Any) -> dict[str, Any]:
    """An Excalidraw 0.18 element as the browser saves it, internals included."""
    base = {"id": id_, "type": type_, "x": 100.4, "y": 200.6, "width": 180.2, "height": 80.7,
            "angle": 0, "strokeColor": "#1e1e1e", "backgroundColor": "transparent",
            "fillStyle": "solid", "strokeWidth": 2, "strokeStyle": "solid", "roughness": 1,
            "opacity": 100, "groupIds": [], "frameId": None, "index": "a0",
            "roundness": {"type": 3}, "seed": 1234567, "version": 7, "versionNonce": 7654321,
            "isDeleted": False, "boundElements": None, "updated": 1759320000000,
            "link": None, "locked": False}
    base.update(fields)
    return base


def typical_scene() -> list[dict[str, Any]]:
    """A hand-drawn architecture sketch: 8 labelled boxes, 7 bound arrows,
    5 freehand strokes, one pushed image, two deleted elements."""
    scene: list[dict[str, Any]] = [_element("boring-canvas-pushed-scene", "image",
                                            fileId="boring-canvas-pushed-scene-file",
                                            status="saved", scale=[1, 1], crop=None)]
    for i in range(8):
        scene.append(_element(f"box{i}", "rectangle", x=i * 220.3, backgroundColor="#a5d8ff",
                              boundElements=[{"type": "text", "id": f"label{i}"},
                                             {"type": "arrow", "id": f"arrow{i}"}]))
        scene.append(_element(f"label{i}", "text", text=f"service {i}", originalText=f"service {i}",
                              fontSize=20, fontFamily=5, textAlign="center",
                              verticalAlign="middle", containerId=f"box{i}", autoResize=True,
                              lineHeight=1.25, roundness=None))
    for i in range(7):
        scene.append(_element(f"arrow{i}", "arrow", points=[[0, 0], [110.5, 3.25], [220.1, 0]],
                              lastCommittedPoint=None, startArrowhead=None, endArrowhead="arrow",
                              elbowed=False,
                              startBinding={"elementId": f"box{i}", "focus": 0.01, "gap": 4.2},
                              endBinding={"elementId": f"box{i + 1}", "focus": -0.02, "gap": 5.1}))
    for i in range(5):
        points = [[j * 1.37, (j % 7) * 2.11] for j in range(120)]
        scene.append(_element(f"stroke{i}", "freedraw", points=points, pressures=[],
                              simulatePressure=True, lastCommittedPoint=points[-1]))
    scene.append(_element("gone1", "ellipse", isDeleted=True))
    scene.append(_element("gone2", "text", text="old", isDeleted=True))
    return scene


class CompactElementsTests(unittest.TestCase):
    def test_compact_shape(self) -> None:
        compact = {el["id"]: el for el in compact_elements(typical_scene())}
        self.assertNotIn("gone1", compact)
        self.assertNotIn("gone2", compact)
        self.assertEqual(compact["box1"], {
            "id": "box1", "type": "rectangle", "x": 220, "y": 201, "width": 180, "height": 81,
            "strokeColor": "#1e1e1e", "backgroundColor": "#a5d8ff"})
        self.assertEqual(compact["label1"]["text"], "service 1")
        self.assertEqual(compact["label1"]["containerId"], "box1")
        self.assertEqual(compact["arrow1"]["start"], "box1")
        self.assertEqual(compact["arrow1"]["end"], "box2")
        self.assertEqual(compact["arrow1"]["points"], [[0, 0], [110, 3], [220, 0]])
        self.assertEqual(compact["arrow1"]["endArrowhead"], "arrow")
        self.assertNotIn("startArrowhead", compact["arrow1"])
        self.assertNotIn("points", compact["stroke0"])
        for el in compact.values():
            for internal in ("seed", "version", "versionNonce", "updated", "roundness",
                             "boundElements", "frameId", "isDeleted", "originalText"):
                self.assertNotIn(internal, el)

    def test_typical_scene_is_well_under_the_bound(self) -> None:
        raw = len(json.dumps(typical_scene()).encode())
        compact = len(json.dumps(compact_elements(typical_scene()), separators=(",", ":")).encode())
        self.assertGreater(raw, 30_000)
        self.assertLess(compact, raw / 5)
        self.assertLess(compact, MAX_ANSWER_BYTES / 2)

    def test_not_a_list_is_a_failure(self) -> None:
        with self.assertRaises(ValueError):
            compact_elements({"elements": []})


class Lifespan:
    """Drive the app's ASGI lifespan (starts the SDK session manager)."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def __aenter__(self) -> None:
        self.task = asyncio.create_task(self.app(
            {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
            self.inbox.get, self.outbox.put))
        await self.inbox.put({"type": "lifespan.startup"})
        assert (await self.outbox.get())["type"] == "lifespan.startup.complete"

    async def __aexit__(self, *exc: object) -> None:
        await self.inbox.put({"type": "lifespan.shutdown"})
        assert (await self.outbox.get())["type"] == "lifespan.shutdown.complete"
        await self.task


class McpServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.cp = FakeControlPlane()
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanvasStore(Path(self._tmp.name))
        self.now = 1000.0
        control_plane = ControlPlane(self.cp.url, CLIENT_ID, SECRET)
        self.watchdog = Watchdog(Registration(control_plane), clock=lambda: self.now)
        # As a family installs canvas: the Crocodile's introspection and registration, one grant per Pond.
        secret = Path(self._tmp.name) / "secret"
        secret.write_text(SECRET)
        self.config = Path(self._tmp.name) / "config.json"
        self.config.write_text(json.dumps({
            "url": OWN_URL,
            "oauth": {"issuer": self.cp.url, "introspection_url": self.cp.url + "/introspect",
                      "client_id": CLIENT_ID, "secret_file": str(secret)},
            "pond": {"url": self.cp.url, "client_id": CLIENT_ID, "secret_file": str(secret)},
            "access": [{"subject": "pondA", "role": "writer", "author": "pondA"},
                       {"subject": "pondB", "role": "writer", "author": "pondB"}]}))
        self.app = self.app_for()
        self.lifespan = Lifespan(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app, raise_app_exceptions=False),
            base_url="http://127.0.0.1:18511")
        self.cp.tokens["good"] = {"active": True, "aud": OWN_URL, "exp": int(time.time()) + 300,
                                  "sub": "pondA", "client_id": "control-plane"}
        self.cp.tokens["goodB"] = dict(self.cp.tokens["good"], sub="pondB")

    def app_for(self, unsafe_url_auth: bool = False) -> Any:
        config = ServiceConfig(self.config)
        return config.app(build_server(self.store, PUBLIC, config.auth, self.watchdog.pinged, unsafe_url_auth))

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.cp.close()
        self._tmp.cleanup()

    async def call(self, tool: str, arguments: dict[str, Any], token: str | None = "good",
                   ) -> httpx2.Response:
        return await self.client.post("/mcp", json=_call_body(tool, arguments),
                                      headers=_headers("tools/call", tool, token))

    async def call_ok(self, tool: str, arguments: dict[str, Any], token: str = "good",
                      ) -> dict[str, Any]:
        response = await self.call(tool, arguments, token)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertFalse(result.get("isError"), result)
        return json.loads(result["content"][0]["text"])  # the kit's one compact text block

    async def call_error(self, tool: str, arguments: dict[str, Any], token: str = "good") -> str:
        response = await self.call(tool, arguments, token)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertTrue(result["isError"], result)
        text = result["content"][0]["text"]
        return text.removeprefix(f"Error executing tool {tool}: ")  # the SDK's prefix on argument errors

    async def call_refused(self, tool: str, arguments: dict[str, Any], token: str = "good",
                           ) -> dict[str, Any]:
        outcome = json.loads(await self.call_error(tool, arguments, token))
        self.assertEqual(outcome.pop("status"), "refused")
        details = outcome.pop("details", {})
        return {**outcome, **details}

    async def tools(self) -> list[dict[str, Any]]:
        response = await self.client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {"_meta": _meta()}},
            headers=_headers("tools/list", None, "good"))
        return response.json()["result"]["tools"]

    async def tool_description(self, tool: str) -> str:
        return next(t["description"] for t in await self.tools() if t["name"] == tool)

    def named(self, owner: str, name: str) -> tuple[str, str]:
        """(canvas_id, token) of a canvas created as `owner` would over MCP."""
        return self.store.create_named(owner, name)

    # ---- the tool surface: names, no ids or tokens

    async def test_tool_surface_is_names_only(self) -> None:
        tools = {t["name"]: t for t in await self.tools()}
        self.assertEqual(set(tools), {"create_canvas", "push_scene", "get_elements",
                                      "get_edits", "list_canvases", "delete_canvas"})
        for name, tool in tools.items():
            properties = set(tool["inputSchema"].get("properties", {}))
            self.assertFalse(properties & {"canvas_id", "token", "owner", "pond"}, name)
        self.assertEqual(set(tools["push_scene"]["inputSchema"]["properties"]), {"name", "svg"})
        self.assertEqual(tools["list_canvases"]["inputSchema"].get("properties", {}), {})
        self.assertIn("per caller", tools["create_canvas"]["description"])
        self.assertIn("once a browser has opened the canvas", tools["get_elements"]["description"])

    async def test_unknown_argument_is_refused_by_name(self) -> None:
        response = await self.call("list_canvases", {"canvas_id": "0" * 32})
        result = response.json()["result"]
        self.assertTrue(result["isError"], result)
        self.assertIn("canvas_id", result["content"][0]["text"])
        self.assertIn("unknown: canvas_id", result["content"][0]["text"])  # the kit's one-line errors

    # ---- open_url: only with --unsafe-url-auth, and the description warns

    async def test_no_open_url_by_default(self) -> None:
        """Safe mode: no token reaches the agent; the answer names the operator's command
        that issues one (the command itself is exercised in test_admin)."""
        created = await self.call_ok("create_canvas", {"name": "canvas1"})
        canvas_id = self.store.find_named("pondA", "canvas1").canvas_id
        self.assertEqual(created, {"name": "canvas1", "url": f"{PUBLIC}/canvas/{canvas_id}",
                                   "token_from_operator": "sudo boring-canvas-admin token canvas1 --owner pondA"})
        description = await self.tool_description("create_canvas")
        self.assertNotIn("open_url", description)
        self.assertIn("ask the operator of the canvas server for the token", description)

    async def test_open_url_with_unsafe_url_auth(self) -> None:
        control_plane = ControlPlane(self.cp.url, CLIENT_ID, SECRET)
        app = self.app_for(unsafe_url_auth=True)
        async with Lifespan(app):
            self.client, plain = httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://127.0.0.1:18511"), self.client
            try:
                created = await self.call_ok("create_canvas", {"name": "canvas1"})
                description = await self.tool_description("create_canvas")
                listed = await self.call_ok("list_canvases", {})
                reopened = await self.call_ok("open_canvas", {"name": "canvas1"})
                missing = await self.call_refused("open_canvas", {"name": "nope"})
            finally:
                await self.client.aclose()
                self.client = plain
        canvas_id = self.store.find_named("pondA", "canvas1").canvas_id
        self.assertEqual(set(created), {"name", "url", "open_url"})
        prefix = f"{PUBLIC}/canvas/{canvas_id}#this-is-unsafe-dont-do-this-auth="  # the token in the fragment
        self.assertTrue(created["open_url"].startswith(prefix), created)
        self.assertIn("anyone with it can edit the canvas", description)
        self.assertNotIn("open_url", listed["canvases"][0])  # the plaintext token is not kept
        # open_canvas issues a fresh link; the old one stops working.
        new_token = reopened["open_url"][len(prefix):]
        self.assertTrue(self.store.verify_token(canvas_id, new_token))
        self.assertFalse(self.store.verify_token(canvas_id, created["open_url"][len(prefix):]))
        self.assertEqual(missing["code"], "not_found")

    # ---- tools with an active token for our own URL

    async def test_tools_round_trip_by_name(self) -> None:
        await self.call_ok("create_canvas", {"name": "canvas1"})
        svg = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>'
        self.assertEqual(await self.call_ok("push_scene", {"name": "canvas1", "svg": svg}),
                         {"ok": True})
        self.assertEqual(await self.call_ok("get_elements", {"name": "canvas1"}), {"elements": []})
        self.assertEqual(await self.call_ok("get_edits", {"name": "canvas1"}),
                         {"edits": [], "omitted": 0})
        self.assertIn(svg[:20], self.store.find_named("pondA", "canvas1").scene_svg)

    async def test_introspection_request_shape_and_cache(self) -> None:
        await self.call_ok("create_canvas", {"name": "one"})
        await self.call_ok("create_canvas", {"name": "two"})
        introspections = [r for r in self.cp.requests if r["path"] == "/introspect"]
        self.assertEqual(len(introspections), 1)  # second call served from the cache
        self.assertEqual(introspections[0]["auth"], _basic())
        self.assertEqual(parse_qs(introspections[0]["body"].decode()), {"token": ["good"]})

    async def test_outcomes_are_tool_errors_with_codes(self) -> None:
        await self.call_ok("create_canvas", {"name": "canvas1"})
        for tool, args in (("get_elements", {"name": "nope"}), ("get_edits", {"name": "nope"}),
                           ("push_scene", {"name": "nope", "svg": "<svg/>"})):
            with self.subTest(tool=tool):
                self.assertEqual(await self.call_refused(tool, args),
                                 {"code": "not_found", "message": "no canvas named nope"})
        bad_svg = {"name": "canvas1", "svg": "<svg"}
        self.assertEqual((await self.call_refused("push_scene", bad_svg))["code"], "invalid_request")

    async def test_delete_canvas_only_the_callers_own(self) -> None:
        await self.call_ok("create_canvas", {"name": "mine"})
        await self.call_ok("create_canvas", {"name": "mine"}, token="goodB")
        mine_a = self.store.find_named("pondA", "mine").canvas_id
        mine_b = self.store.find_named("pondB", "mine").canvas_id
        # Another Pond's canvas of the same name is not found, and stays.
        self.assertEqual(await self.call_ok("delete_canvas", {"name": "mine"}), {"deleted": True})
        self.assertEqual(len(self.store.list_canvases()), 1)
        self.assertFalse((self.store.state_dir / f"{mine_a}.json").exists())
        self.assertTrue((self.store.state_dir / f"{mine_b}.json").exists())
        self.assertEqual(self.store.find_named("pondB", "mine").canvas_id, mine_b)
        self.assertEqual((await self.call_refused("delete_canvas", {"name": "mine"}))["code"],
                         "not_found")
        self.assertEqual((await self.call_refused("push_scene", {"name": "mine", "svg": "<svg/>"}))["code"],
                         "not_found")
        # The name is free again.
        await self.call_ok("create_canvas", {"name": "mine"})
        self.assertEqual((await self.call_ok("list_canvases", {}, token="goodB"))["canvases"][0]["name"],
                         "mine")

    async def test_duplicate_name_is_refused(self) -> None:
        await self.call_ok("create_canvas", {"name": "canvas1"})
        self.assertEqual(await self.call_refused("create_canvas", {"name": "canvas1"}),
                         {"code": "invalid_request", "reason": "name_taken",
                          "message": "a canvas named canvas1 already exists"})
        self.assertEqual(len(self.store.list_canvases()), 1)

    async def test_concurrent_same_name_creates_exactly_one_wins(self) -> None:
        responses = await asyncio.gather(*(self.call("create_canvas", {"name": "canvas1"})
                                           for _ in range(8)))
        results = [r.json()["result"] for r in responses]
        self.assertEqual(sum(1 for r in results if not r.get("isError")), 1)
        self.assertEqual(sum(1 for r in results if r.get("isError")
                             and "already exists" in r["content"][0]["text"]), 7)
        self.assertEqual(len(self.store.list_canvases()), 1)

    async def test_name_pattern_is_refused_by_schema(self) -> None:
        for name in ("Canvas1", "-x", "a b", "a_b", "a" * 64, "../etc", ""):
            with self.subTest(name=name):
                text = await self.call_error("create_canvas", {"name": name})
                self.assertIn("should match pattern", text)
                text = await self.call_error("get_elements", {"name": name})
                self.assertIn("should match pattern", text)
        self.assertEqual(self.store.list_canvases(), [])

    async def test_two_ponds_have_separate_names(self) -> None:
        a = await self.call_ok("create_canvas", {"name": "canvas1"}, token="good")
        b = await self.call_ok("create_canvas", {"name": "canvas1"}, token="goodB")
        self.assertNotEqual(a["url"], b["url"])
        svg_a = '<svg xmlns="http://www.w3.org/2000/svg"><rect id="from-a"/></svg>'
        svg_b = '<svg xmlns="http://www.w3.org/2000/svg"><rect id="from-b"/></svg>'
        await self.call_ok("push_scene", {"name": "canvas1", "svg": svg_a}, token="good")
        await self.call_ok("push_scene", {"name": "canvas1", "svg": svg_b}, token="goodB")
        self.assertIn("from-a", self.store.find_named("pondA", "canvas1").scene_svg)
        self.assertIn("from-b", self.store.find_named("pondB", "canvas1").scene_svg)
        # pondA's own canvas, invisible to pondB: not_found, nothing written
        await self.call_ok("create_canvas", {"name": "only-a"}, token="good")
        for tool, args in (("push_scene", {"name": "only-a", "svg": svg_b}),
                           ("get_elements", {"name": "only-a"}),
                           ("get_edits", {"name": "only-a"})):
            with self.subTest(tool=tool):
                self.assertEqual((await self.call_refused(tool, args, token="goodB"))["code"],
                                 "not_found")
        self.assertIsNone(self.store.find_named("pondA", "only-a").scene_svg)
        listed_b = await self.call_ok("list_canvases", {}, token="goodB")
        self.assertEqual([c["url"] for c in listed_b["canvases"]], [b["url"]])

    async def test_list_canvases_shows_only_own(self) -> None:
        legacy_ownerless_canvas(self.store)  # no owner, no name
        await self.call_ok("create_canvas", {"name": "zeta"})
        alpha = await self.call_ok("create_canvas", {"name": "alpha"})
        await self.call_ok("create_canvas", {"name": "other"}, token="goodB")
        listed = (await self.call_ok("list_canvases", {}))["canvases"]
        self.assertEqual([c["name"] for c in listed], ["alpha", "zeta"])
        self.assertEqual(listed[0]["url"], alpha["url"])
        self.assertRegex(listed[0]["created"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(set(listed[0]), {"name", "url", "created", "expires"})

    async def test_list_canvases_shows_expiry_and_the_descriptions_say_so(self) -> None:
        await self.call_ok("create_canvas", {"name": "canvas1"})
        created_s = self.store.find_named("pondA", "canvas1").created_s
        entry = (await self.call_ok("list_canvases", {}))["canvases"][0]
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        created = datetime.strptime(entry["created"], fmt).replace(tzinfo=timezone.utc)
        expires = datetime.strptime(entry["expires"], fmt).replace(tzinfo=timezone.utc)
        self.assertEqual(int(created.timestamp()), int(created_s))
        self.assertEqual(expires - created, timedelta(days=self.store.max_age_days))
        days = f"{self.store.max_age_days:g} days after it was created"
        for tool in ("create_canvas", "list_canvases"):
            with self.subTest(tool=tool):
                description = await self.tool_description(tool)
                self.assertIn(days, description)
                self.assertIn("no warning", description)
        self.assertIn("expires", await self.tool_description("list_canvases"))

    async def test_old_ownerless_canvas_is_invisible(self) -> None:
        canvas_id, _ = legacy_ownerless_canvas(self.store)
        self.assertEqual(await self.call_ok("list_canvases", {}), {"canvases": []})
        self.assertEqual((await self.call_refused("get_elements", {"name": canvas_id}))["code"],
                         "not_found")

    async def test_expiry_frees_the_name(self) -> None:
        old = await self.call_ok("create_canvas", {"name": "canvas1"})
        old_id = self.store.find_named("pondA", "canvas1").canvas_id
        path = self.store.state_dir / f"{old_id}.json"
        data = json.loads(path.read_text())
        data["created_s"] = time.time() - (self.store.max_age_days + 1) * 86400
        path.write_text(json.dumps(data))
        new = await self.call_ok("create_canvas", {"name": "canvas1"})  # sweeps first
        self.assertNotEqual(new["url"], old["url"])
        self.assertFalse(path.exists())
        listed = (await self.call_ok("list_canvases", {}))["canvases"]
        self.assertEqual([c["url"] for c in listed], [new["url"]])

    async def test_get_elements_is_compact(self) -> None:
        canvas_id, token = self.named("pondA", "canvas1")
        self.store.write_elements(canvas_id, token, json.dumps(typical_scene()))
        response = await self.call("get_elements", {"name": "canvas1"})
        elements = json.loads(response.json()["result"]["content"][0]["text"])["elements"]
        self.assertEqual(elements, compact_elements(typical_scene()))
        self.assertLess(len(response.content), 64 * 1024)

    async def test_get_elements_over_the_bound_is_a_limit_refusal(self) -> None:
        canvas_id, token = self.named("pondA", "canvas1")
        texts = [_element(f"t{i}", "text", text="x" * 200) for i in range(150)]
        self.store.write_elements(canvas_id, token, json.dumps(texts))
        refusal = await self.call_refused("get_elements", {"name": "canvas1"})
        self.assertEqual(refusal["code"], "limit")
        self.assertIn(str(MAX_ANSWER_BYTES), refusal["message"])

    async def test_get_edits_keeps_the_newest_that_fit(self) -> None:
        canvas_id, token = self.named("pondA", "canvas1")
        for version in range(1, 4):
            batch = [_element(f"e{i}", "rectangle", version=version) for i in range(100)]
            self.store.write_elements(canvas_id, token, json.dumps(batch))
        answer = await self.call_ok("get_edits", {"name": "canvas1"})
        edits = self.store.get_edits(canvas_id)
        self.assertEqual(len(edits), 300)
        self.assertGreater(answer["omitted"], 0)
        self.assertEqual(answer["omitted"] + len(answer["edits"]), 300)
        self.assertEqual(answer["edits"], edits[answer["omitted"]:])
        self.assertLessEqual(len(json.dumps(answer["edits"], separators=(",", ":"))),
                             MAX_ANSWER_BYTES)

    async def test_create_limits_are_tool_errors(self) -> None:
        self.store.create_rate_limit = 1
        await self.call_ok("create_canvas", {"name": "one"})
        self.assertEqual((await self.call_refused("create_canvas", {"name": "two"}))["code"], "busy")
        self.store.create_rate_limit, self.store.max_canvases = 0, 1
        self.assertEqual((await self.call_refused("create_canvas", {"name": "two"}))["code"],
                         "limit")
        # Neither refusal left a canvas behind: only the first exists, in the listing and on disk.
        listed = (await self.call_ok("list_canvases", {}))["canvases"]
        self.assertEqual([c["name"] for c in listed], ["one"])
        one = self.store.find_named("pondA", "one").canvas_id
        self.assertEqual([c["canvas_id"] for c in self.store.list_canvases()], [one])
        canvas_files = [p.name for p in self.store.state_dir.glob("*.json") if p.name != "config.json"]
        self.assertEqual(canvas_files, [f"{one}.json"])  # config.json is this test's own config

    # ---- tokens that must not pass

    async def assert_unauthorized(self, token: str | None) -> None:
        response = await self.call("create_canvas", {"name": "canvas1"}, token=token)
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self.store.list_canvases(), [])

    async def test_missing_token_is_401(self) -> None:
        await self.assert_unauthorized(None)
        self.assertEqual(self.cp.paths(), [])

    async def test_inactive_token_is_401(self) -> None:
        await self.assert_unauthorized("unknown")

    async def test_wrong_audience_is_401(self) -> None:
        self.cp.tokens["other"] = dict(self.cp.tokens["good"], aud="http://127.0.0.1:18599/mcp")
        await self.assert_unauthorized("other")

    async def test_token_without_sub_is_401(self) -> None:
        for sub in (None, "", 7):
            token = dict(self.cp.tokens["good"])
            if sub is None:
                del token["sub"]
            else:
                token["sub"] = sub
            self.cp.tokens[f"nosub{sub}"] = token
            with self.subTest(sub=sub), self.assertLogs("boring_service_kit.pond", "WARNING"):
                await self.assert_unauthorized(f"nosub{sub}")

    async def test_expired_token_is_401(self) -> None:
        self.cp.tokens["old"] = dict(self.cp.tokens["good"], exp=int(time.time()) - 1)
        await self.assert_unauthorized("old")

    async def test_unreachable_introspection_is_a_failure_not_a_401(self) -> None:
        self.cp.introspect_status = 500
        with self.assertLogs("boring_service_kit.pond", "ERROR"):
            response = await self.call("create_canvas", {"name": "canvas1"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.store.list_canvases(), [])

    # ---- the server/discover exemption

    async def register(self, sources: list[str]) -> None:
        self.cp.register = (200, json.dumps({"ping_interval_seconds": 3, "sources": sources}).encode())
        self.assertTrue(await self.watchdog.check())

    async def discover(self, client: str) -> httpx2.Response:
        return await self.client.post("/mcp", json=_discover_body(client),
                                      headers=_headers("server/discover"))

    async def test_discover_without_token_marks_the_source(self) -> None:
        await self.register(["pondA", "pondB"])
        self.now += 5
        response = await self.discover("pondA")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("supportedVersions", response.json()["result"])
        self.now += 2  # pondB silent for 7 s >= 2 intervals; pondA only 2 s
        self.assertTrue(self.watchdog.due())
        self.assertEqual((await self.discover("pondB")).status_code, 200)
        self.assertFalse(self.watchdog.due())
        self.assertNotIn("/introspect", self.cp.paths())

    async def test_discover_header_with_other_body_is_header_mismatch(self) -> None:
        response = await self.client.post("/mcp", json=_call_body("create_canvas", {}),
                                          headers=_headers("server/discover", "create_canvas"))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], -32020)  # HEADER_MISMATCH
        self.assertEqual(self.store.list_canvases(), [])
        self.assertEqual(self.cp.paths(), [])

    async def test_discover_header_without_modern_version_needs_a_token(self) -> None:
        # Without MCP-Protocol-Version the SDK takes the legacy path, which
        # does not compare Mcp-Method with the body: no exemption there.
        headers = _headers("server/discover", "create_canvas")
        del headers["MCP-Protocol-Version"]
        response = await self.client.post("/mcp", json=_call_body("create_canvas", {}),
                                          headers=headers)
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self.store.list_canvases(), [])

    async def test_duplicate_method_header_needs_a_token(self) -> None:
        headers = list(_headers("server/discover").items()) + [("Mcp-Method", "server/discover")]
        response = await self.client.post("/mcp", json=_discover_body("pondA"), headers=headers)
        self.assertEqual(response.status_code, 401, response.text)


if __name__ == "__main__":
    unittest.main()
