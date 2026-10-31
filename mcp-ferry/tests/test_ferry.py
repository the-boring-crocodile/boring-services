"""The ferry end to end: Crocodile tokens in front (the kit's fake control plane),
a real MCP server over HTTP behind it (uvicorn on a loopback port)."""
import asyncio
import json
import logging
import signal
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from boring_service_kit.testing import OWN_URL, Service, mcp_headers, mcp_meta
from boring_mcp_ferry import ferry
from boring_mcp_ferry.config import ConfigError, read
from boring_mcp_ferry.server import prepare, quiet_http_logs
from boring_service_kit.outcomes import Failed


def load_backend(url):
    from boring_mcp_ferry.config import Backend
    return Backend(name="x", service=Path("x.json"), url=url, headers={}, timeout_seconds=None,
                   allow=None, deny=frozenset())


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def backend_server():
    server = MCPServer("fixture-backend", instructions="Fixture backend: look reads, switch changes.")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def look(where: str) -> str:
        """Read something."""
        return f"seen {where}"

    @server.tool(annotations=ToolAnnotations(read_only_hint=False))
    def switch(on: bool) -> str:
        """Change something."""
        return f"switched {'on' if on else 'off'}"

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def secret_tool() -> str:
        """Filtered out by the ferry."""
        return "never"

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def slow(seconds: float) -> str:
        """Takes its time."""
        await asyncio.sleep(seconds)
        return "late"

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def broken() -> str:
        """Fails on its own terms."""
        raise ToolError("the backend's own error")
    return server


@asynccontextmanager
async def backend(port):
    app = backend_server().streamable_http_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    server.capture_signals = lambda: __import__("contextlib").nullcontext()  # the test owns the signals
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await task


def write_config(tmp_path, service, backend_url, tools=None, pond=False, name="fixture", own_url=OWN_URL):
    (tmp_path / "secret").write_text("fixture-only")
    (tmp_path / f"{name}-url").write_text(backend_url + "\n")
    spec = {"url": own_url,
            "oauth": {"issuer": service.cp.url, "introspection_url": service.cp.url + "/introspect",
                      "client_id": "fixture", "secret_file": "secret"},
            "access": [{"subject": "pond", "role": "writer"}, {"subject": "meadow", "role": "reader"}]}
    if pond:
        spec["pond"] = {"url": service.cp.url, "client_id": "fixture", "secret_file": "secret"}
    (tmp_path / f"{name}.json").write_text(json.dumps(spec))
    backend_spec = {"service": f"{name}.json", "upstream": {"url_file": f"{name}-url"}}
    if tools is not None:
        backend_spec["tools"] = tools
    (tmp_path / "config.json").write_text(json.dumps({"backends": {name: backend_spec}}))
    return tmp_path / "config.json"


@asynccontextmanager
async def ferry_with(tmp_path, backend_url, tools=None, pond=False):
    service = Service()
    path = write_config(tmp_path, service, backend_url, tools, pond)
    (front,), upstreams = prepare(path, tmp_path / "state", True)
    service.app = front.app
    async with service:
        yield service, front
    for upstream in upstreams:
        await upstream.close()


def test_tools_pass_through_with_roles_and_filter(tmp_path):
    async def scenario():
        async with backend(free_port()) as url, ferry_with(tmp_path, url, {"deny": ["secret_tool"]}) as (svc, _):
            listed = await svc.client.post("/mcp", headers=mcp_headers("tools/list", token="meadow"),
                                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                                 "params": {"_meta": mcp_meta()}})
            names = {t["name"] for t in listed.json()["result"]["tools"]}
            assert names == {"look", "switch", "slow", "broken"}  # secret_tool filtered
            assert await svc.result("look", {"where": "garden"}, "meadow") == (False, "seen garden")
            error, refused = await svc.result("switch", {"on": True}, "meadow")  # reader, write tool
            assert error and refused["code"] == "forbidden"
            assert await svc.result("switch", {"on": True}, "pond") == (False, "switched on")
            error, refused = await svc.result("secret_tool", {}, "pond")
            assert error and refused["code"] == "not_found"
            error, refused = await svc.result("nope", {}, "pond")
            assert error and refused["code"] == "not_found"
            error, text = await svc.result("broken", {}, "pond")  # the backend's own error, unchanged
            assert error and "the backend's own error" in text
            assert (await svc.call("look", {"where": "x"}, None)).status_code == 401
            assert (await svc.call("look", {"where": "x"}, "other-service")).status_code == 401
        log = (tmp_path / "state" / "fixture-calls.jsonl").read_text()
        assert '"tool":"look"' in log.replace(" ", "") and "garden" not in log  # names, no values
    asyncio.run(scenario())


def test_allow_list_offers_only_named_tools(tmp_path):
    async def scenario():
        async with backend(free_port()) as url, ferry_with(tmp_path, url, {"allow": ["look"]}) as (svc, _):
            assert await svc.result("look", {"where": "a"}, "pond") == (False, "seen a")
            error, refused = await svc.result("switch", {"on": False}, "pond")
            assert error and refused["code"] == "not_found"
    asyncio.run(scenario())


def test_timeout_is_a_failure_that_may_have_run(tmp_path):
    async def scenario():
        async with backend(free_port()) as url, ferry_with(tmp_path, url) as (svc, front):
            await svc.result("look", {"where": "warm"}, "pond")  # tool list fetched
            front.server.carried.timeout = 0.3
            error, failed = await svc.result("slow", {"seconds": 2}, "pond")
            assert error and failed["status"] == "failed" and failed["code"] == "timeout"
            assert "may have run" in failed["message"]
    asyncio.run(scenario())


def test_one_deadline_covers_the_tool_list_and_the_call(tmp_path):
    async def scenario():
        async with backend(free_port()) as url, ferry_with(tmp_path, url) as (svc, front):
            front.server.carried.timeout = 0.8
            real = front.server.carried.upstream.tools

            async def slow_list():
                await asyncio.sleep(0.5)
                return await real()
            front.server.carried.upstream.tools = slow_list
            front.server.carried.fetched = -1e9  # stale: this call must fetch the list first
            started = time.monotonic()
            error, failed = await svc.result("slow", {"seconds": 0.6}, "pond")  # 0.5 + 0.6 > 0.8
            assert error and failed["code"] == "timeout"
            assert time.monotonic() - started < 1.05  # one 0.8 s deadline, not two
    asyncio.run(scenario())


def test_a_call_fetches_the_tool_list_once(tmp_path):
    async def scenario():
        async with backend(free_port()) as url, ferry_with(tmp_path, url) as (svc, front):
            real = front.server.carried.upstream.tools
            fetched = []

            async def counted():
                fetched.append(1)
                return await real()
            front.server.carried.upstream.tools = counted
            front.server.carried.fetched = -1e9  # stale: the SDK's own tools/list and the call share one fetch
            assert await svc.result("look", {"where": "garden"}, "pond") == (False, "seen garden")
            assert len(fetched) == 1
    asyncio.run(scenario())


def test_concurrent_calls_on_a_stale_list_share_one_fetch(tmp_path):
    async def scenario():
        async with backend(free_port()) as url, ferry_with(tmp_path, url) as (svc, front):
            real, fetched = front.server.carried.upstream.tools, []

            async def counted():
                fetched.append(1)
                await asyncio.sleep(0.2)
                return await real()
            front.server.carried.upstream.tools = counted
            front.server.carried.fetched = -1e9
            answers = await asyncio.gather(*(svc.result("look", {"where": str(i)}, "pond") for i in range(8)))
            assert answers == [(False, f"seen {i}") for i in range(8)]
            assert len(fetched) == 1
    asyncio.run(scenario())


def test_protocol_errors_do_not_echo_the_backend_url():
    backend_ = load_backend("http://homeassistant:8086/s3cret-path")
    upstream = ferry.Upstream(backend_)
    text = upstream.redact("Redirect to http://homeassistant:8086/s3cret-path/ not followed; /s3cret-path")
    assert "s3cret" not in text and text.count("<backend URL>") == 2
    asyncio.run(upstream.close())


def test_a_hung_tool_list_does_not_wedge_later_calls(tmp_path):
    async def scenario():
        async with backend(free_port()) as url, ferry_with(tmp_path, url) as (svc, front):
            front.server.carried.timeout = 0.5
            real, hang = front.server.carried.upstream.tools, [True]

            async def sometimes_hung():
                if hang[0]:
                    await asyncio.Event().wait()  # never answers, never fails
                return await real()
            front.server.carried.upstream.tools = sometimes_hung
            front.server.carried.fetched = -1e9
            error, refused = await svc.result("look", {"where": "x"}, "pond")
            assert error and refused["status"] == "refused"
            hang[0] = False  # the backend recovers
            await asyncio.sleep(0.6)  # the hung fetch has met its own cap by now
            assert await svc.result("look", {"where": "y"}, "pond") == (False, "seen y")
    asyncio.run(scenario())


def test_a_tool_list_that_fails_is_a_refusal_nothing_sent(tmp_path):
    async def scenario():
        async with backend(free_port()) as url, ferry_with(tmp_path, url) as (svc, front):
            async def broken():
                raise Failed("connection", "lost", effects="unknown")
            front.server.carried.upstream.tools = broken
            error, refused = await svc.result("look", {"where": "x"}, "pond")
            assert error and refused["status"] == "refused" and refused["code"] == "unavailable"
            assert "nothing was sent" in refused["message"]
    asyncio.run(scenario())


def test_unknown_names_do_not_fetch_a_fresh_list_again(tmp_path):
    async def scenario():
        async with backend(free_port()) as url, ferry_with(tmp_path, url) as (svc, front):
            real, fetched = front.server.carried.upstream.tools, []

            async def counting():
                fetched.append(1)
                return await real()
            front.server.carried.upstream.tools = counting
            await svc.result("look", {"where": "x"}, "pond")
            front.server.carried.fetched -= 5  # older than the SDK's own listing would once have reused
            for arguments in ({}, {"a": 1}, {"b": 2}):  # with arguments the SDK lists the tools first
                error, refused = await svc.result("nope", arguments, "pond")
                assert error and refused["code"] == "not_found"
            assert len(fetched) == 1
    asyncio.run(scenario())


def test_backend_urls_stay_out_of_the_logs(tmp_path, caplog):
    async def scenario():
        async with backend(free_port()) as url, ferry_with(tmp_path, url) as (svc, _):
            await svc.result("look", {"where": "x"}, "pond")
            return url
    levels = {n: logging.getLogger(n).level for n in ("httpx2", "httpcore2", "httpx", "httpcore")}
    try:
        with caplog.at_level(logging.INFO):
            url = asyncio.run(scenario())
            assert url in caplog.text  # the HTTP client logs request URLs at INFO ...
        caplog.clear()
        quiet_http_logs()  # ... which the ferry's process turns off
        with caplog.at_level(logging.INFO):
            url = asyncio.run(scenario())
        assert url.rsplit("/", 1)[0] not in caplog.text
    finally:
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


def test_front_ends_wire_the_named_registration(tmp_path, caplog):
    async def scenario():
        async with ferry_with(tmp_path, f"http://127.0.0.1:{free_port()}/mcp", pond=True) as (_, front):
            with caplog.at_level(logging.INFO, logger="boring_mcp_ferry"):
                await front.config.watchdog.register()
    asyncio.run(scenario())
    assert "MCP registration of fixture: not registered" in caplog.text  # the fake ack is malformed


def test_backend_down_is_unavailable_and_nothing_sent(tmp_path):
    async def scenario():
        async with ferry_with(tmp_path, f"http://127.0.0.1:{free_port()}/mcp") as (svc, front):
            error, refused = await svc.result("look", {"where": "x"}, "pond")
            assert error and refused["status"] == "refused" and refused["code"] == "unavailable"
            assert not await front.server.carried.upstream.healthy()
    asyncio.run(scenario())


def discover_request(svc):
    return svc.client.post("/mcp", headers=mcp_headers("server/discover"),
                           json={"jsonrpc": "2.0", "id": 1, "method": "server/discover",
                                 "params": {"_meta": mcp_meta("pond")}})


def test_heartbeat_only_while_the_backend_answers(tmp_path):
    async def scenario():
        port = free_port()
        async with backend(port) as url:
            async with ferry_with(tmp_path, url, pond=True) as (svc, _):
                assert (await discover_request(svc)).status_code == 200
        async with ferry_with(tmp_path / "down", f"http://127.0.0.1:{port}/mcp", pond=True) as (svc, _):
            response = await discover_request(svc)
            assert response.status_code == 503 and "not reachable" in response.text
    (tmp_path / "down").mkdir()
    asyncio.run(scenario())


def test_registration_attempts_name_the_backend(tmp_path, caplog):
    class Dog:
        async def register(self):
            return {"ping_interval_seconds": 3, "sources": ["pond"]}
    dog = Dog()
    ferry.labelled(dog, "homeassistant")
    with caplog.at_level(logging.INFO, logger="boring_mcp_ferry"):
        asyncio.run(dog.register())
    assert "MCP registration of homeassistant: registered" in caplog.text


@pytest.mark.parametrize("config, message", [
    ({}, "at least one backend"),
    ({"backends": {"Bad_Name": {"service": "s.json", "upstream": {"url": "http://x/mcp"}}}}, "lowercase"),
    ({"backends": {"a": {"service": "s.json", "upstream": {"url": "http://x/mcp", "url_file": "f"}}}}, "exactly one"),
    ({"backends": {"a": {"service": "s.json", "upstream": {"url": "http://u:p@x/mcp"}}}}, "without credentials"),
    ({"backends": {"a": {"service": "s.json", "upstream": {"url": "http://x/mcp"}, "tools": {"allow": [], "deny": []}}}},
     "exactly one of allow or deny"),
    ({"backends": {"a": {"service": "s.json", "upstream": {"url": "http://x/mcp"}, "extra": 1}}}, "upstream, optional service"),
    ({"backends": {"a": {"service": "s.json", "upstream": {"url": "http://x/mcp", "timeout_seconds": 0}}}}, "positive"),
])
def test_bad_configuration_is_refused(tmp_path, config, message):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ConfigError, match=message):
        read(path)


def test_headers_file_rejects_transport_headers(tmp_path):
    (tmp_path / "h.json").write_text(json.dumps({"Host": "evil"}))
    (tmp_path / "config.json").write_text(json.dumps({"backends": {"a": {
        "service": "s.json", "upstream": {"url": "http://x/mcp", "headers_file": "h.json"}}}}))
    with pytest.raises(ConfigError, match="transport headers"):
        read(tmp_path / "config.json")
    (tmp_path / "h.json").write_text(json.dumps({"X-Api-Key": "k"}))
    assert read(tmp_path / "config.json").backends[0].headers == {"X-Api-Key": "k"}


@pytest.mark.parametrize("bind", ["0.0.0.0", "0", "::", "[::]", "::0", "0:0:0:0:0:0:0:0", " "])
def test_front_ends_must_not_listen_on_every_address(tmp_path, bind):
    service = Service()
    try:
        path = write_config(tmp_path, service, "http://127.0.0.1:1/mcp", own_url="https://ferry.example.test/x/mcp")
        spec = json.loads((tmp_path / "fixture.json").read_text())
        (tmp_path / "fixture.json").write_text(json.dumps({**spec, "bind": bind, "port": 18520}))
        with pytest.raises(ConfigError, match="not every address"):
            prepare(path, None, False)
    finally:
        service.cp.close()


def test_two_backends_cannot_share_a_service_identity(tmp_path):
    service = Service()
    try:
        write_config(tmp_path, service, "http://127.0.0.1:1/mcp")
        spec = json.loads((tmp_path / "fixture.json").read_text())
        (tmp_path / "second.json").write_text(json.dumps({**spec, "url": "http://127.0.0.1:18598/mcp"}))
        (tmp_path / "config.json").write_text(json.dumps({"backends": {
            "fixture": {"service": "fixture.json", "upstream": {"url": "http://127.0.0.1:1/mcp"}},
            "second": {"service": "second.json", "upstream": {"url": "http://127.0.0.1:2/mcp"}}}}))
        with pytest.raises(ConfigError, match="same client_id"):
            prepare(tmp_path / "config.json", None, False)
    finally:
        service.cp.close()


def test_a_front_end_without_crocodile_tokens_is_refused(tmp_path):
    (tmp_path / "keys.json").write_text("{}")
    (tmp_path / "s.json").write_text(json.dumps({"url": "http://127.0.0.1:18600/mcp", "keys": {"file": "keys.json"}}))
    (tmp_path / "config.json").write_text(json.dumps({"backends": {"a": {
        "service": "s.json", "upstream": {"url": "http://x/mcp"}}}}))
    with pytest.raises(ConfigError):
        prepare(tmp_path / "config.json", None, False)


def test_sigterm_stops_every_front_end(tmp_path):
    service = Service()
    try:
        backends = {}
        ports = [free_port(), free_port()]
        (tmp_path / "secret").write_text("fixture-only")
        for name, port in zip(("one", "two"), ports):
            (tmp_path / f"{name}.json").write_text(json.dumps({
                "url": f"http://127.0.0.1:{port}/mcp",
                "oauth": {"issuer": service.cp.url, "introspection_url": service.cp.url + "/introspect",
                          "client_id": f"fixture-{name}", "secret_file": "secret"},
                "access": [{"subject": "pond", "role": "writer"}]}))
            backends[name] = {"service": f"{name}.json", "upstream": {"url": f"http://127.0.0.1:{free_port()}/mcp"}}
        (tmp_path / "config.json").write_text(json.dumps({"backends": backends}))
        process = subprocess.Popen([sys.executable, "-m", "boring_mcp_ferry.server", "http", "--config",
                                    str(tmp_path / "config.json"), "--no-call-log"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + 20
        for port in ports:
            while True:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    assert process.poll() is None, process.stdout.read()
                    assert time.monotonic() < deadline, "front ends did not start"
                    time.sleep(0.1)
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            pytest.fail("SIGTERM did not stop the ferry: " + process.stdout.read())
        for port in ports:
            with pytest.raises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
    finally:
        service.cp.close()


# --- a gateway: several backends behind four tools (DECISIONS S18) ----------------------------------

@asynccontextmanager
async def gateway_with(tmp_path, backend_url):
    service = Service()
    write_config(tmp_path, service, backend_url, name="gateway")
    (tmp_path / "guide.md").write_text("# Fixture guide\nLook before you switch.\n")
    (tmp_path / "config.json").write_text(json.dumps({
        "backends": {
            "fixture": {"upstream": {"url": backend_url}, "tools": {"deny": ["secret_tool"]},
                        "guide_file": "guide.md"},
            "down": {"upstream": {"url": f"http://127.0.0.1:{free_port()}/mcp"}}},
        "gateways": {"services": {"service": "gateway.json", "backends": ["fixture", "down"]}}}))
    (front,), upstreams = prepare(tmp_path / "config.json", tmp_path / "state", True)
    service.app = front.app
    async with service:
        yield service, front
    for upstream in upstreams:
        await upstream.close()


def test_gateway_offers_four_tools_and_passes_calls_through(tmp_path):
    async def scenario():
        async with backend(free_port()) as url, gateway_with(tmp_path, url) as (svc, front):
            listed = await svc.client.post("/mcp", headers=mcp_headers("tools/list", token="meadow"),
                                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                                 "params": {"_meta": mcp_meta()}})
            tools = {t["name"]: t for t in listed.json()["result"]["tools"]}
            assert set(tools) == {"service_list", "service_describe", "service_read", "service_call"}
            assert tools["service_read"]["annotations"]["readOnlyHint"] is True
            assert tools["service_call"]["annotations"]["readOnlyHint"] is False
            error, listing = await svc.result("service_list", {}, "meadow")
            assert not error and listing == {"services": [{"name": "fixture", "available": True},
                                                          {"name": "down", "available": False}]}
            error, described = await svc.result("service_describe", {"service": "fixture"}, "meadow")
            assert not error and described["service"] == "fixture"
            assert {t["name"]: t["read_only"] for t in described["tools"]} == {
                "look": True, "switch": False, "slow": True, "broken": True}  # secret_tool filtered
            assert described["instructions"] == "Fixture backend: look reads, switch changes."
            assert described["guide"].startswith("# Fixture guide")
            assert await svc.result("service_read", {"service": "fixture", "tool": "look",
                                                     "arguments": {"where": "garden"}}, "meadow") == (False, "seen garden")
            error, refused = await svc.result("service_read", {"service": "fixture", "tool": "switch",
                                                               "arguments": {"on": True}}, "pond")
            assert error and refused["code"] == "forbidden" and "service_call" in refused["message"]
            error, refused = await svc.result("service_call", {"service": "fixture", "tool": "switch",
                                                               "arguments": {"on": True}}, "meadow")
            assert error and refused["code"] == "forbidden"  # a reader cannot change through the gateway
            assert await svc.result("service_call", {"service": "fixture", "tool": "switch",
                                                     "arguments": {"on": True}}, "pond") == (False, "switched on")
            assert await svc.result("service_call", {"service": "fixture", "tool": "look",
                                                     "arguments": {"where": "x"}}, "meadow") == (False, "seen x")
            error, text = await svc.result("service_call", {"service": "fixture", "tool": "broken"}, "pond")
            assert error and "the backend's own error" in text  # its own error, unchanged
            for args, code in (({"service": "nope", "tool": "look"}, "not_found"),
                               ({"service": "fixture", "tool": "secret_tool"}, "not_found"),
                               ({"service": "fixture", "tool": "look", "extra": 1}, "invalid_request"),
                               ({"service": "fixture", "tool": "look", "arguments": "x"}, "invalid_request")):
                error, refused = await svc.result("service_call", args, "pond")
                assert error and refused["code"] == code, (args, refused)
            error, refused = await svc.result("service_describe", {"service": "down"}, "pond")
            assert error and refused["code"] == "unavailable"
        log = (tmp_path / "state" / "services-calls.jsonl").read_text()
        assert '"tool":"service_read"' in log.replace(" ", "") and "garden" not in log
    asyncio.run(scenario())


def test_gateway_configuration(tmp_path):
    base = {"upstream": {"url": "http://127.0.0.1:1/mcp"}}
    for config, message in (
            ({"backends": {"a": base}}, "neither a service of its own nor a gateway"),
            ({"backends": {"a": base}, "gateways": {"g": {"service": "g.json", "backends": ["b"]}}}, "no backend 'b'"),
            ({"backends": {"a": base}, "gateways": {"a": {"service": "g.json", "backends": ["a"]}}}, "not a backend"),
            ({"backends": {"a": base}, "gateways": {"g": {"service": "g.json", "backends": ["a", "a"]}}}, "each once"),
            ({"backends": {"a": {**base, "guide_file": "missing.md"}},
              "gateways": {"g": {"service": "g.json", "backends": ["a"]}}}, "guide_file cannot be read")):
        (tmp_path / "config.json").write_text(json.dumps(config))
        with pytest.raises(ConfigError, match=message):
            read(tmp_path / "config.json")


def test_a_gateway_may_take_the_box_access_keys(tmp_path):
    """Copilot on a dev box: a loopback gateway with access keys only (no Crocodile)."""
    (tmp_path / "keys.json").write_text("{}")
    (tmp_path / "g.json").write_text(json.dumps({"url": "http://127.0.0.1:18600/mcp", "keys": {"file": "keys.json"}}))
    (tmp_path / "config.json").write_text(json.dumps({
        "backends": {"a": {"upstream": {"url": "http://127.0.0.1:1/mcp"}}},
        "gateways": {"g": {"service": "g.json", "backends": ["a"]}}}))
    fronts, upstreams = prepare(tmp_path / "config.json", None, False)
    assert [f.name for f in fronts] == ["g"] and len(upstreams) == 1
