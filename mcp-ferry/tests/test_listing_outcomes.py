"""Read-only tool listing cannot report unknown mutation effects."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from boring_mcp_ferry import ferry
from boring_service_kit.outcomes import Refused
from test_ferry import load_backend


@pytest.mark.parametrize("kind", ["tool-count", "page-count", "listing-timeout"])
def test_listing_refuses_known_no_call_outcomes(monkeypatch, kind):
    async def scenario():
        upstream = ferry.Upstream(load_backend("http://backend.invalid/mcp"))
        client = SimpleNamespace(instructions="", call_tool=AsyncMock())
        if kind == "tool-count":
            monkeypatch.setattr(ferry, "MAX_TOOLS", 1)
            client.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[object(),object()], next_cursor=None))
            expected = "limit"
        elif kind == "page-count":
            monkeypatch.setattr(ferry, "MAX_TOOL_PAGES", 2)
            client.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[], next_cursor="again"))
            expected = "limit"
        else:
            monkeypatch.setattr(ferry, "FETCH_CAP_SECONDS", 0.01)
            async def stalled(*args, **kwargs):
                await asyncio.Event().wait()
            client.list_tools = AsyncMock(side_effect=stalled)
            expected = "timeout"
        @asynccontextmanager
        async def connection():
            yield client
        monkeypatch.setattr(upstream, "_client", connection)
        carried = ferry.Carried(upstream.backend, upstream, call_limit=None)
        try:
            with pytest.raises(Refused) as direct:
                await carried._capped_refresh()
            assert direct.value.code == expected
            client.list_tools.reset_mock()
            with pytest.raises(Refused) as outcome:
                await carried.listed(None)
            assert outcome.value.code == expected
            assert outcome.value.as_data()["status"] == "refused"
            client.call_tool.assert_not_awaited()
            assert client.list_tools.await_count == (2 if kind == "page-count" else 1)
        finally:
            await upstream.close()
    asyncio.run(scenario())
