"""One ASGI app owns Canvas browser, MCP and issuer lifecycle."""
import json
from types import SimpleNamespace
from contextlib import asynccontextmanager

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from boring_canvas import server
from boring_canvas.storage import CanvasStore


def test_stalled_browser_body_has_a_deadline(monkeypatch):
    import asyncio
    from starlette.requests import Request
    from boring_canvas import web_handler
    monkeypatch.setattr(web_handler, "BODY_READ_SECONDS", 0.001, raising=False)
    async def check():
        async def receive():
            await asyncio.Event().wait()
        request = Request({"type":"http", "headers":[(b"content-length", b"1")]}, receive)
        with pytest.raises(web_handler.BodyRefused) as caught:
            await asyncio.wait_for(web_handler.text_body(request), 0.1)
        assert caught.value.response.status_code == 408
    asyncio.run(check())


def test_browser_and_mcp_share_one_app_and_lifespan(tmp_path):
    events = []
    @asynccontextmanager
    async def lifespan(app):
        events.append("start")
        yield
        events.append("stop")
    sdk = Starlette(routes=[Route("/mcp", lambda request: JSONResponse({"mcp": True}), methods=["POST"])],
                    lifespan=lifespan)
    config = SimpleNamespace(url="https://canvas.example.test/mcp", issuer_paths=(),
        watchdog=None, app=lambda _: sdk, auth={}, on_discover=None)
    store = CanvasStore(tmp_path)
    try:
        canvas_id, token = store.create_named("owner", "one-app")
        app = server.App(store, config)
        with TestClient(app, base_url="https://canvas.example.test") as client:
            assert events == ["start"]
            assert client.post("/mcp").json() == {"mcp": True}
            page = client.get("/canvas/" + canvas_id)
            assert page.status_code == 200 and "Content-Security-Policy" in page.headers
            session = client.post("/canvas/" + canvas_id + "/session", json={"token": token})
            assert session.status_code == 200 and "Secure" in session.headers["set-cookie"]
            assert client.get("/canvas/" + canvas_id + "/version").status_code == 200
            assert client.post("/canvas").status_code == 404
            assert client.get("/admin/limits").status_code == 404
        assert events == ["start", "stop"]
    finally:
        store.close()


@pytest.mark.parametrize("flag", ["--host=0.0.0.0", "--port=18510", "--public-base-url=https://canvas.example.test"])
def test_split_web_listener_flags_are_gone(flag):
    with pytest.raises(SystemExit):
        server.build_parser().parse_args(["--config", "fixture.json", flag])


def test_public_url_path_owns_browser_links_assets_and_cookie_path(tmp_path):
    from boring_service_kit.http import ServiceConfig
    (tmp_path / "keys.json").write_text('{"issuer":"key:canvas-prefix","keys":[]}')
    (tmp_path / "config.json").write_text(json.dumps({"url":"https://canvas.example.test/services/canvas/mcp",
        "mode":"keys", "keys":{"file":"keys.json"}}))
    store = CanvasStore(tmp_path / "state")
    try:
        cid, token = store.create_named("owner", "prefixed")
        with TestClient(server.App(store, ServiceConfig(tmp_path / "config.json")),
                        base_url="https://canvas.example.test") as client:
            page = client.get("/services/canvas/canvas/" + cid)
            assert page.status_code == 200
            assert 'href="/services/canvas/canvas/assets/editor.css' in page.text
            assert 'window.__CANVAS_BASE__ = "/services/canvas/canvas"' in page.text
            assert client.get("/services/canvas/canvas/assets/editor.css").status_code == 200
            session = client.post("/services/canvas/canvas/" + cid + "/session", json={"token":token})
            assert "Path=/services/canvas/canvas/" + cid in session.headers["set-cookie"]
            assert client.get("/services/canvas/canvas/" + cid + "/version").status_code == 200
            assert client.get("/canvas/" + cid).status_code == 404
    finally:
        store.close()


def test_same_app_serves_own_issuer_under_its_prefix(tmp_path):
    from boring_service_kit.http import ServiceConfig
    (tmp_path / "oauth").mkdir(mode=0o700)
    (tmp_path / "approval").write_text("fixture-approval-for-synthetic-tests")
    (tmp_path / "config.json").write_text(json.dumps({"url":"https://canvas.example.test/services/canvas/mcp",
        "mode":"issuer", "issuer":{"url":"https://canvas.example.test/services/canvas",
        "subject":"owner", "role":"writer", "database":"oauth/tokens.sqlite",
        "approval_secret_file":"approval"}}))
    config = ServiceConfig(tmp_path / "config.json")
    store = CanvasStore(tmp_path / "state")
    try:
        cid, _ = store.create_named("owner", "issuer-canvas")
        with TestClient(server.App(store, config), base_url="https://canvas.example.test") as client:
            assert client.get("/services/canvas/canvas/" + cid).status_code == 200
            assert client.post("/services/canvas/mcp").status_code == 401
            metadata = client.get("/.well-known/oauth-protected-resource/services/canvas/mcp")
            assert metadata.json()["resource"] == config.url
            issuer = client.get("/.well-known/oauth-authorization-server/services/canvas")
            assert issuer.json()["issuer"] == "https://canvas.example.test/services/canvas"
    finally:
        store.close()


def test_container_admin_reads_browser_url_from_service_owner(tmp_path, monkeypatch, capsys):
    from boring_canvas import admin
    (tmp_path / "keys.json").write_text('{"issuer":"key:admin-fixture","keys":[]}')
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"url":"https://canvas.example.test/services/canvas/mcp",
        "mode":"keys", "keys":{"file":"keys.json"}}))
    state = tmp_path / "state"
    store = CanvasStore(state)
    cid, _ = store.create_named("owner", "admin-canvas")
    store.close()
    cmdline = tmp_path / "cmdline"
    cmdline.write_bytes(bytes([0]).join(part.encode() for part in
        ["python", "-m", "boring_canvas.server", f"--state-dir={state}", f"--config={config}", ""]))
    monkeypatch.setattr(admin, "SERVER_CMDLINE", cmdline)
    assert admin.main(["token", "admin-canvas"]) == 0
    assert json.loads(capsys.readouterr().out)["url"] == "https://canvas.example.test/services/canvas/canvas/" + cid
