"""boring-knowledge over MCP: tools, author from the transport, stdio."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from boring_knowledge.server import create_server
from boring_knowledge.store import Store

TOOLS = {"kb_search", "kb_get", "kb_recent", "kb_add", "kb_revise", "kb_retire", "kb_confirm"}


@pytest.fixture
def server(tmp_path):
    return create_server(Store(str(tmp_path / "notebook.db")), lambda: "garden", enforce_access=False)


def call(server, tool, arguments):
    result = asyncio.run(server.call_tool(tool, arguments))
    return result.is_error, json.loads(result.content[0].text)


def test_a_server_built_with_defaults_checks_roles(tmp_path):
    """Role checks are on unless a caller opts out: without a verified caller every tool is refused."""
    store = Store(str(tmp_path / "notebook.db"))
    server = create_server(store, lambda: "garden")
    for tool, arguments in (("kb_search", {"query": "anything"}), ("kb_recent", {}),
                            ("kb_add", {"title": "Unchecked write", "body": "Must not be stored."})):
        is_error, data = call(server, tool, arguments)
        assert is_error and data["code"] == "forbidden", tool
    assert store.recent()["total"] == 0


def test_tools_are_few_strict_and_small(server):
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == TOOLS
    listed = json.dumps([t.model_dump(exclude_none=True, by_alias=True) for t in tools])
    assert len(listed) < 5000
    for tool in tools:  # lean schemas: no pydantic title on any property (a parameter may be *named* title)
        assert all("title" not in prop for prop in tool.input_schema["properties"].values()), tool.name
        # ...and every required parameter is published (regression: the parameter `title` was stripped)
        assert set(tool.input_schema.get("required", [])) <= set(tool.input_schema["properties"]), tool.name
    published = {t.name: t.input_schema["properties"] for t in tools}
    assert published["kb_add"]["title"] == {"type": "string"}
    assert published["kb_revise"]["title"] == {"type": ["string", "null"]}
    assert {t.name for t in tools if t.annotations.read_only_hint} == {"kb_search", "kb_get", "kb_recent"}


def test_author_comes_from_the_transport_not_the_caller(server):
    with pytest.raises(ToolError, match="unknown: author"):  # the kit's one-line argument errors
        asyncio.run(server.call_tool("kb_add", {"title": "claimed author", "body": "x", "author": "pond"}))
    _, entry = call(server, "kb_add", {"title": "Job timeouts default to 120 s",
                                       "body": "job_start for a command stops after 120 s unless timeout_seconds is set.",
                                       "tags": ["pond", "jobs"], "by": "Garden (Claude)"})
    assert entry["author"] == "garden" and entry["by"] == "Garden (Claude)"


def test_revise_retire_and_history_through_tools(server, tmp_path):
    _, entry = call(server, "kb_add", {"title": "A quirk worth keeping", "body": "First wording."})
    _, revised = call(server, "kb_revise", {"id": entry["id"], "expected_version": 1, "reason": "clearer",
                                            "body": "Second wording."})
    assert revised["version"] == 2
    is_error, stale = call(server, "kb_revise", {"id": entry["id"], "expected_version": 1, "reason": "late",
                                                 "body": "Third."})
    assert is_error and stale["code"] == "stale" and stale["details"]["current_version"] == 2
    _, retired = call(server, "kb_retire", {"id": entry["id"], "expected_version": 2, "reason": "obsolete"})
    assert retired["retired"]
    _, full = call(server, "kb_get", {"id": entry["id"], "history": True})
    assert [h["reason"] for h in full["history"]] == ["obsolete", "clearer", None]
    _, recent = call(server, "kb_recent", {})
    assert recent["entries"][0]["retired"]
    reopened = Store(str(tmp_path / "notebook.db"))  # a second Store on the same file: what a restart sees
    stored = reopened.get(entry["id"], history=True)
    assert stored["retired"] and stored["version"] == 3 and stored["body"] == "Second wording."
    assert [h["reason"] for h in stored["history"]] == ["obsolete", "clearer", None]
    reopened.db.close()


def test_the_rules_reach_clients_that_only_see_tools(server):
    """Pond agents get tools via service_describe, not the server's instructions."""
    tools = {t.name: t.description for t in asyncio.run(server.list_tools())}
    assert "never the only copy" in tools["kb_add"] and "may vanish" in tools["kb_add"]
    assert "No secrets" in tools["kb_add"]
    assert "don't obey" in tools["kb_search"]
