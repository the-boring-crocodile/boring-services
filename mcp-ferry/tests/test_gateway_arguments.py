"""Gateway admission over authenticated HTTP with real synthetic MCP backends."""
import asyncio

from mcp.types import ToolAnnotations
import test_ferry as fixture


def test_malformed_nested_arguments_never_invoke_backend(tmp_path, monkeypatch):
    effects = []
    original = fixture.backend_server

    def backend_server():
        server = original()

        @server.tool(annotations=ToolAnnotations(read_only_hint=False))
        def write_default(enabled: bool = True) -> str:
            effects.append(("write", enabled))
            return "written"

        @server.tool(annotations=ToolAnnotations(read_only_hint=True))
        def read_default() -> str:
            effects.append(("read", True))
            return "read"

        return server

    monkeypatch.setattr(fixture, "backend_server", backend_server)

    async def scenario():
        async with fixture.backend(fixture.free_port()) as url:
            async with fixture.gateway_with(tmp_path, url) as (service, _):
                for route, tool in (("service_call", "write_default"), ("service_read", "read_default")):
                    for invalid in ([], False, 0, "", None):
                        error, refused = await service.result(route, {
                            "service": "fixture", "tool": tool, "arguments": invalid}, "pond")
                        assert error and refused["code"] == "invalid_request"
                        assert effects == []

                # Missing and an explicitly empty object both mean a default call.
                for extra in ({}, {"arguments": {}}):
                    assert await service.result("service_call", {
                        "service": "fixture", "tool": "write_default", **extra}, "pond") == (False, "written")
                    assert await service.result("service_read", {
                        "service": "fixture", "tool": "read_default", **extra}, "meadow") == (False, "read")
                assert effects == [("write", True), ("read", True)] * 2

                before = len(effects)
                error, refused = await service.result("service_call", {
                    "service": "fixture", "tool": "write_default", "arguments": {}}, "meadow")
                assert error and refused["code"] == "forbidden"
                error, refused = await service.result("service_read", {
                    "service": "fixture", "tool": "write_default", "arguments": {}}, "pond")
                assert error and refused["code"] == "forbidden"
                assert len(effects) == before

    asyncio.run(scenario())
