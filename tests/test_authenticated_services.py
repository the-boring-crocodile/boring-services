"""Actual domain tools via OAuth HTTP; fixture helper comes from boring_service_kit.testing."""
import asyncio
import json
import time
from boring_service_kit.access import calling_author
from boring_service_kit.http import ServiceConfig
from boring_service_kit.pond import wrap_app
from boring_knowledge.server import create_server
from boring_knowledge.store import Store
from boring_canvas.mcp_service import build_server
from boring_canvas.storage import CanvasStore
from boring_sourcecache_d365.server import create_server as source_server
from boring_sourcecache_d365.sourcecache import build
from boring_service_kit.testing import Service

def setup(tmp_path, make_server):
    service = Service()
    (tmp_path / "secret").write_text("fixture-only")
    path = tmp_path / "http.json"
    path.write_text(json.dumps({"url": "http://127.0.0.1:18599/mcp",
        "oauth": {"issuer": service.cp.url, "introspection_url": service.cp.url + "/introspect",
                  "client_id": "fixture", "secret_file": "secret"},
        "access": [{"subject": "pond", "role": "writer", "author": "old-pond"},
                   {"subject": "developer", "role": "writer"},
                   {"subject": "reader", "role": "reader"}]}))
    config = ServiceConfig(path)
    good = {"active": True, "aud": config.url, "exp": int(time.time()) + 60}
    service.cp.tokens = {sub: {**good, "sub": sub} for sub in ["pond", "developer", "reader", "denied"]}
    service.app = wrap_app(make_server(config.auth), config.url, discover_bypass=False)
    return service

def test_one_notebook_two_writers_reader_and_denied(tmp_path):
    store = Store(str(tmp_path / "notebook.db"))
    async def scenario():
        async with setup(tmp_path, lambda auth: create_server(store, calling_author,
                           log_calls=False, enforce_access=True, **auth)) as service:
            _, entry = await service.result("kb_add", {"title": "Shared notebook", "body": "original"}, "pond")
            _, revised = await service.result("kb_revise",
                {"id": entry["id"], "expected_version": 1, "reason": "better", "body": "changed"}, "developer")
            assert json.loads(revised["author"])[1] == "developer"
            error, refused = await service.result("kb_retire",
                {"id": entry["id"], "expected_version": 2, "reason": "reader"}, "reader")
            assert error and refused["code"] == "forbidden"
            _, full = await service.result("kb_get", {"id": entry["id"], "history": True}, "reader")
            assert full["created_by"] == "old-pond" and full["version"] == 2 and not full["retired"]
            assert (await service.call("kb_get", {"id": entry["id"]}, "denied")).status_code == 401
    asyncio.run(scenario())

def test_canvas_roles_do_not_share_ownership(tmp_path):
    store = CanvasStore(tmp_path / "canvas")
    async def scenario():
        async with setup(tmp_path, lambda auth: build_server(store, "https://canvas.example",
                           auth, None, caller=calling_author)) as service:
            error, result = await service.result("create_canvas", {"name": "con"}, "pond")
            assert not error, result
            error, result = await service.result("get_elements", {"name": "con"}, "developer")
            assert error and result["code"] == "not_found"
            error, result = await service.result("create_canvas", {"name": "aux"}, "reader")
            assert error and result["code"] == "forbidden"
            _, listing = await service.result("list_canvases", {}, "pond")
            assert [c["name"] for c in listing["canvases"]] == ["con"]
            assert (await service.call("list_canvases", {}, "denied")).status_code == 401
    asyncio.run(scenario())

# Reader/writer matrices through the authenticated entry point: every mutating tool is
# refused to a reader with no effect, reads stay open. The tool inventory makes a new,
# unclassified tool fail here until it is added to one of the sets.
KB_WRITER = {"kb_add", "kb_revise", "kb_confirm", "kb_retire"}
KB_READER = {"kb_search", "kb_get", "kb_recent"}
CANVAS_WRITER = {"create_canvas", "push_scene", "delete_canvas", "open_canvas"}
CANVAS_READER = {"get_elements", "get_edits", "list_canvases"}


def forbidden(outcome):
    error, data = outcome
    return error and data["code"] == "forbidden"


def test_knowledge_reader_matrix(tmp_path):
    store = Store(str(tmp_path / "notebook.db"))
    built = []
    def make(auth):
        built.append(create_server(store, calling_author, log_calls=False, enforce_access=True, **auth))
        return built[-1]
    async def scenario():
        async with setup(tmp_path, make) as service:
            assert {t.name for t in await built[0].list_tools()} == KB_WRITER | KB_READER
            _, entry = await service.result("kb_add", {"title": "Kept as written", "body": "original"}, "pond")
            attempts = {
                "kb_add": {"title": "Reader note", "body": "x"},
                "kb_revise": {"id": entry["id"], "expected_version": 1, "reason": "r", "body": "changed"},
                "kb_confirm": {"id": entry["id"], "expected_version": 1, "evidence": "e"},
                "kb_retire": {"id": entry["id"], "expected_version": 1, "reason": "r"}}
            assert set(attempts) == KB_WRITER
            for tool, arguments in attempts.items():
                assert forbidden(await service.result(tool, arguments, "reader")), tool
            for tool, arguments in (("kb_get", {"id": entry["id"]}), ("kb_recent", {}),
                                    ("kb_search", {"query": "Kept"})):
                error, data = await service.result(tool, arguments, "reader")
                assert not error, (tool, data)
            _, full = await service.result("kb_get", {"id": entry["id"]}, "reader")
            assert full["version"] == 1 and full["body"] == "original" and not full["retired"]
            error, missing = await service.result("kb_get", {"id": entry["id"] + 1}, "pond")
            assert error and missing["code"] == "not_found"  # the reader's kb_add left nothing
    asyncio.run(scenario())


def test_canvas_reader_matrix(tmp_path):
    store = CanvasStore(tmp_path / "canvas")
    built = []
    def make(auth):
        built.append(build_server(store, "https://canvas.example", auth, None,
                                  unsafe_url_auth=True, caller=calling_author))
        return built[-1]
    async def scenario():
        async with setup(tmp_path, make) as service:
            assert {t.name for t in await built[0].list_tools()} == CANVAS_WRITER | CANVAS_READER
            error, _ = await service.result("create_canvas", {"name": "con"}, "pond")
            assert not error
            _, before = await service.result("get_elements", {"name": "con"}, "pond")
            svg = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
            attempts = {"create_canvas": {"name": "aux"}, "push_scene": {"name": "con", "svg": svg},
                        "delete_canvas": {"name": "con"}, "open_canvas": {"name": "con"}}
            assert set(attempts) == CANVAS_WRITER
            for tool, arguments in attempts.items():
                assert forbidden(await service.result(tool, arguments, "reader")), tool
            # Reads pass the permission check and reach the domain (the reader owns no canvas).
            error, listing = await service.result("list_canvases", {}, "reader")
            assert not error and listing["canvases"] == []
            for tool in ("get_elements", "get_edits"):
                error, data = await service.result(tool, {"name": "con"}, "reader")
                assert error and data["code"] == "not_found", (tool, data)
            _, listing = await service.result("list_canvases", {}, "pond")
            assert [c["name"] for c in listing["canvases"]] == ["con"]
            _, after = await service.result("get_elements", {"name": "con"}, "pond")
            assert after == before
    asyncio.run(scenario())


def test_source_snapshot_reader_and_default_deny(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    db = tmp_path / "source.db"
    build(str(root), str(db), "fixture")
    built = []
    def make(auth):
        built.append(source_server([str(db)], enforce_access=True, **auth))
        return built[-1]
    async def scenario():
        async with setup(tmp_path, make) as service:
            # Read-only service: every tool needs reader, none writes.
            assert {t.name for t in await built[0].list_tools()} == {
                "source_info", "source_search", "source_element", "label_lookup"}
            error, info = await service.result("source_info", {}, "reader")
            assert not error and info["indexes"][0]["snapshot_id"]
            assert (await service.call("source_info", {}, "denied")).status_code == 401
    asyncio.run(scenario())
