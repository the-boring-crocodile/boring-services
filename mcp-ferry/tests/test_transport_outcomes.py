"""Transport cause names do not change dispatch uncertainty."""
import asyncio
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from boring_mcp_ferry.ferry import Upstream
from boring_service_kit.outcomes import Failed, Refused
from test_ferry import load_backend


@pytest.mark.parametrize("error,status,code", [
    (MCPError(-32042, "vendor protocol message"), Failed, "upstream_error"),
    (httpx2.ReadError("lost reply"), Failed, "unavailable"),
    (RuntimeError("backend defect"), Failed, "upstream_error"),
    (httpx2.ConnectError("not connected"), Refused, "unavailable"),
])
def test_request_preserves_status_details_with_shared_cause(error, status, code, monkeypatch):
    async def scenario():
        upstream = Upstream(load_backend("http://backend.invalid/mcp"))
        @asynccontextmanager
        async def connection():
            yield object()
        monkeypatch.setattr(upstream, "_client", connection)
        async def work(client):
            raise error
        try:
            with pytest.raises(status) as caught:
                await upstream._request(work)
            data = caught.value.as_data()
            assert data["code"] == code
            if status is Failed:
                assert data["effects"] == "unknown"
            if isinstance(error, MCPError):
                assert data["backend_code"] == -32042
                assert "vendor protocol message" in data["message"]
        finally:
            await upstream.close()
    asyncio.run(scenario())
