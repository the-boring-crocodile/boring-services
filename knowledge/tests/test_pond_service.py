"""boring-knowledge as a family service (the Crocodile's introspection and registration, by config).

Real SDK app over httpx2's ASGI transport; a fake control plane (a stdlib HTTP
server in a thread) answers /introspect and /services/register. Modelled on
boring-canvas's tests/test_mcp_service.py.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest

from boring_knowledge.server import create_server
from boring_service_kit.access import calling_author
from boring_service_kit.http import ServiceConfig
from boring_knowledge.store import Store

OWN_URL = "http://127.0.0.1:18512/mcp"
CLIENT_ID, SECRET = "knowledge-client", "knowledge-secret"
VERSION = "2026-07-28"


class FakeControlPlane:
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
                fake.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                if self.path == "/introspect":
                    token = parse_qs(body.decode())["token"][0]
                    status, payload = fake.introspect_status, json.dumps(fake.tokens.get(token, {"active": False}))
                elif self.path == "/services/register":
                    status, payload = fake.register[0], fake.register[1].decode()
                else:
                    status, payload = 404, "{}"
                data = payload.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class RecordingWatchdog:
    def __init__(self) -> None:
        self.pings: list[str] = []

    def pinged(self, source: str = "") -> None:
        self.pings.append(source)


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


class Service:
    """The app with its lifespan running, and an HTTP client into it."""

    def __init__(self, tmp_path: Path) -> None:
        self.cp = FakeControlPlane()
        now = int(time.time())
        good = {"active": True, "aud": OWN_URL, "exp": now + 300, "sub": "pond", "client_id": "control-plane"}
        self.cp.tokens.update({
            "pond": good, "garden": dict(good, sub="garden"),
            "other-service": dict(good, aud="http://127.0.0.1:18511/mcp"),
            "expired": dict(good, exp=now - 1), "no-sub": {k: v for k, v in good.items() if k != "sub"}})
        self.watchdog = RecordingWatchdog()
        # As a family installs it: the Crocodile's introspection, its registration, one grant per Pond.
        (tmp_path / "secret").write_text(SECRET)
        (tmp_path / "config.json").write_text(json.dumps({
            "url": OWN_URL,
            "oauth": {"issuer": self.cp.url, "introspection_url": self.cp.url + "/introspect",
                      "client_id": CLIENT_ID, "secret_file": "secret"},
            "pond": {"url": self.cp.url, "client_id": CLIENT_ID, "secret_file": "secret"},
            "access": [{"subject": "pond", "role": "writer", "author": "pond"},
                       {"subject": "garden", "role": "writer", "author": "garden"}]}))
        config = ServiceConfig(tmp_path / "config.json")
        self.app = config.app(create_server(Store(str(tmp_path / "notebook.db")), calling_author,
                                            on_discover=self.watchdog.pinged, enforce_access=True, **config.auth))

    async def __aenter__(self) -> "Service":
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.task = asyncio.create_task(self.app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
                                                 self.inbox.get, self.outbox.put))
        await self.inbox.put({"type": "lifespan.startup"})
        assert (await self.outbox.get())["type"] == "lifespan.startup.complete"
        self.client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app, raise_app_exceptions=False),
                                         base_url="http://127.0.0.1:18512")
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client.aclose()
        await self.inbox.put({"type": "lifespan.shutdown"})
        assert (await self.outbox.get())["type"] == "lifespan.shutdown.complete"
        await self.task
        self.cp.close()

    async def call(self, tool: str, arguments: dict[str, Any], token: str | None = "pond") -> httpx2.Response:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments, "_meta": _meta()}}
        return await self.client.post("/mcp", json=body, headers=_headers("tools/call", tool, token))

    async def result(self, tool: str, arguments: dict[str, Any], token: str = "pond") -> tuple[bool, dict[str, Any]]:
        response = await self.call(tool, arguments, token)
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        return bool(result.get("isError")), json.loads(result["content"][0]["text"])


def run(coro: Any) -> Any:
    return asyncio.run(coro)


ENTRY = {"title": "Job timeouts default to 120 s", "body": "Set timeout_seconds for long installs.",
         "tags": ["pond", "jobs"]}


def test_the_author_is_the_calling_pond(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            is_error, entry = await service.result("kb_add", ENTRY, token="pond")
            assert not is_error and entry["author"] == "pond" and entry["created_by"] == "pond"
            _, revised = await service.result("kb_revise", {"id": entry["id"], "expected_version": 1,
                                                            "reason": "seen again on Windows",
                                                            "context": "Pond jobs"}, token="garden")
            assert revised["author"] == "garden" and revised["created_by"] == "pond"
            _, found = await service.result("kb_search", {"query": "timeout"}, token="garden")
            assert found["total"] == 1
    run(scenario())


@pytest.mark.parametrize("token", [None, "unknown", "other-service", "expired", "no-sub"])
def test_tokens_that_are_not_ours_are_refused(tmp_path, token):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            response = await service.call("kb_recent", {}, token=token)
            assert response.status_code == 401, (token, response.status_code)
    run(scenario())


def test_discover_needs_no_token_and_feeds_the_watchdog(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            body = {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": _meta("orchard")}}
            response = await service.client.post("/mcp", json=body, headers=_headers("server/discover"))
            assert response.status_code == 200, response.text
            assert service.watchdog.pings == ["orchard"]
            assert not [r for r in service.cp.requests if r["path"] == "/introspect"]  # no token was checked
    run(scenario())


def test_a_mismatched_method_header_does_not_bypass_auth(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "kb_recent", "arguments": {}, "_meta": _meta()}}
            response = await service.client.post("/mcp", json=body, headers=_headers("server/discover"))
            assert response.status_code != 200 or "result" not in response.json()
    run(scenario())


def test_refusals_read_as_the_worker_reads_them(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            is_error, refusal = await service.result("kb_get", {"id": 999})
            # The worker's rule (services.py, _tool_refusal): JSON with a code and a message.
            assert is_error and refusal["code"] == "not_found" and isinstance(refusal["message"], str)
    run(scenario())


def test_an_unreachable_control_plane_is_a_failure_not_a_bad_token(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            service.cp.introspect_status = 503
            response = await service.call("kb_recent", {})
            assert response.status_code == 500
    run(scenario())
