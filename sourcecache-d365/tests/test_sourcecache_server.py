"""boring-sourcecache-d365 over MCP: tools, index selection, the build command, a lone server over HTTP."""

from __future__ import annotations

import asyncio
import json

import pytest

import corpus
from boring_sourcecache_d365.server import create_server, main

TOOLS = {"source_info", "source_search", "source_element", "label_lookup"}


@pytest.fixture(scope="module")
def indexes(tmp_path_factory):
    root = tmp_path_factory.mktemp("kcorpus")
    base = corpus.write(root)
    first, second = root / "standard.db", root / "customer.db"
    main(["build", "--source", str(base), "--db", str(first), "--name", "d365-synthetic"])
    main(["build", "--source", str(base / "ContosoExtensions"), "--db", str(second), "--name", "contoso-only"])
    return str(first), str(second)


def call(server, tool, arguments):
    result = asyncio.run(server.call_tool(tool, arguments))
    return result.is_error, json.loads(result.content[0].text)


def test_tools_are_few_strict_and_small(indexes):
    tools = asyncio.run(create_server([indexes[0]], enforce_access=False).list_tools())
    assert {t.name for t in tools} == TOOLS
    listed = json.dumps([t.model_dump(exclude_none=True, by_alias=True) for t in tools])
    assert len(listed) < 4000 and '"title"' not in listed
    assert all(t.annotations.read_only_hint for t in tools)


def test_a_server_built_with_defaults_checks_roles(indexes):
    """Role checks are on unless a caller opts out: without a verified caller every tool is refused."""
    server = create_server([indexes[0]])
    for tool, arguments in (("source_info", {}), ("source_search", {"query": "WHSHelper"}),
                            ("source_element", {"element_type": "AxClass", "name": "WHSHelper"}),
                            ("label_lookup", {"text": "Customer account"})):
        is_error, data = call(server, tool, arguments)
        assert is_error and data["code"] == "forbidden", tool


def test_results_name_their_index(indexes):
    server = create_server(list(indexes), enforce_access=False)
    is_error, data = call(server, "source_search", {"query": "WHSHelper"})
    assert not is_error and data["index"]["name"] == "d365-synthetic"
    is_error, data = call(server, "source_search", {"query": "WHSHelper", "index": "contoso-only"})
    assert data["index"]["name"] == "contoso-only" and data["total_files"] == 1


def test_info_lists_indexes_and_models(indexes):
    server = create_server(list(indexes), enforce_access=False)
    _, listing = call(server, "source_info", {})
    assert [(i["name"], i["default"]) for i in listing["indexes"]] == [("d365-synthetic", True),
                                                                      ("contoso-only", False)]
    _, info = call(server, "source_info", {"index": "d365-synthetic"})
    assert {m["layer"] for m in info["models"]} == {"sys", "isv", "cus"}


def test_label_lookup_with_references(indexes):
    server = create_server([indexes[0]], enforce_access=False)
    _, data = call(server, "label_lookup", {"label_id": "@SYS10", "references": True, "locale": "en-US"})
    assert data["labels"][0]["text"] == "Customer account"
    assert {(r["element_type"], r["name"]) for r in data["references"]} >= {("AxTable", "WHSPutRule"),
                                                                         ("AxClass", "InventLocationValidator")}


def test_refusals_carry_codes(indexes):
    server = create_server([indexes[0]], enforce_access=False)
    is_error, data = call(server, "source_search", {"query": "ab"})
    assert is_error and data["code"] == "invalid_request"
    is_error, data = call(server, "source_info", {"index": "nope"})
    assert is_error and data["code"] == "not_found" and data["details"]["available"] == ["d365-synthetic"]


def test_a_lone_server_signs_claude_in_by_itself(indexes, tmp_path):
    """No Crocodile: the server is its own issuer; Claude identifies itself with its metadata document."""
    from boring_service_kit.http import ServiceConfig
    from boring_service_kit.testing import Service, metadata_document, sign_in
    url, claude, callback = "http://127.0.0.1:18513/mcp", "https://claude.ai/oauth/x.json", "https://claude.ai/cb"
    (tmp_path / "state").mkdir(mode=0o700)
    (tmp_path / "approval").write_text("lone-approval")
    (tmp_path / "config.json").write_text(json.dumps({
        "url": url, "issuer": {"database": "state/oauth.db", "approval_secret_file": "approval", "subject": "robin"},
        "access": [{"subject": "robin", "role": "reader"}]}))
    config = ServiceConfig(tmp_path / "config.json")
    config.issuer.fetch = lambda client_id: metadata_document(client_id, callback, "Claude")
    server = create_server([indexes[0]], **config.auth)

    async def scenario():
        async with Service(config.app(server), url) as service:
            assert (await service.call("label_lookup", {"text": "Location ID"}, token=None)).status_code == 401
            tokens = await sign_in(service.client, url, claude, callback, "lone-approval")
            refused, found = await service.result("label_lookup", {"text": "Location ID is not valid"},
                                                  tokens["access_token"])
            return refused, found

    refused, found = asyncio.run(scenario())
    assert not refused and found["labels"][0]["ref"] == "@SYS100"


def test_info_shows_warm_up_state(indexes):
    import time
    server = create_server(list(indexes), warm_up=True, enforce_access=False)
    deadline = time.monotonic() + 10
    while True:
        _, listing = call(server, "source_info", {})
        states = [i["warm"]["state"] for i in listing["indexes"]]
        if states == ["warm", "warm"] or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    assert states == ["warm", "warm"]
    _, cold = call(create_server(list(indexes), enforce_access=False), "source_info", {})
    assert [i["warm"]["state"] for i in cold["indexes"]] == ["cold", "cold"]


@pytest.mark.parametrize("limit,requested,expected,bound", [
    (28.0, 300, 28, True), (28.0, 0, 28, True), (28.0, 10, 10, False), (28.0, 60, 28, True),
    (None, 300, 300, False), (None, 0, 0, False), (None, 60, 60, False)])
def test_query_guard_is_the_ponds_bound_only_behind_a_pond(monkeypatch, limit, requested, expected, bound):
    """Behind a Pond (call_limit 28) a query ends in time for the answer; on loopback
    for Copilot (no pond section, call_limit None) only --stall-seconds applies."""
    import boring_sourcecache_d365.server as server
    seen = []
    monkeypatch.setattr(server, "create_server",
                        lambda *a, **kw: seen.append((kw["stall_seconds"], kw["pond_bound"])))
    class Config:
        call_limit = limit
        def __init__(self, path): pass
        def serve(self, factory): factory({}, None)
    monkeypatch.setattr(server, "ServiceConfig", Config)
    main(["serve", "--db", "unused.db", "--stall-seconds", str(requested), "--config", "fixture.json"])
    assert seen == [(expected, bound)]


def test_serve_builds_the_real_server_from_its_indexes(monkeypatch, indexes, tmp_path):
    """Only the HTTP serving is stubbed: main builds the real server over both index files."""
    import boring_sourcecache_d365.server as server
    built = []
    class Config:
        call_limit = 28.0
        def __init__(self, path): pass
        def serve(self, factory): built.append(factory({}, None))
    monkeypatch.setattr(server, "ServiceConfig", Config)
    main(["serve", "--db", indexes[0], "--db", indexes[1], "--config", "fixture.json",
          "--state-dir", str(tmp_path / "state"), "--no-warm-up"])
    assert len(built) == 1
    assert {t.name for t in asyncio.run(built[0].list_tools())} == TOOLS
    is_error, refused = call(built[0], "source_info", {})  # served with access enforced: no caller, no answer
    assert is_error and refused["code"] == "forbidden", refused


def test_serve_needs_a_config():
    with pytest.raises(SystemExit) as stopped:
        main(["serve", "--db", "unused.db"])
    assert stopped.value.code == 2


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_invalid_stall_guard_refused_before_start(value):
    with pytest.raises(SystemExit) as stopped:
        main(["serve", "--db", "unused.db", "--stall-seconds", value])
    assert stopped.value.code == 2
