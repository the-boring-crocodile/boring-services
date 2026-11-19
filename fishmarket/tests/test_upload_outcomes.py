"""Length bounds share limit while retaining failed publication status."""
import asyncio
import httpx2
import pytest
from test_market import market as market_fixture, owner, ORIGIN, BASE

market = market_fixture


@pytest.mark.parametrize("body", [b"abc", b"a"])
def test_upload_length_mismatch_is_failed_limit_without_publication(market, body):
    async def scenario():
        app, store, *_ = market
        receipt = store.create(owner(), "upload", max_bytes=10)
        async def chunks():
            yield body
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ORIGIN) as client:
            response = await client.put(BASE + "/api/" + receipt["id"] + "/upload",
                content=chunks(), headers={"Content-Length": "2",
                "X-Fishmarket-Token": receipt["upload_token"], "X-Fishmarket-Filename": "file.txt"})
        assert response.status_code == 500
        assert response.json()["status"] == "failed"
        assert response.json()["code"] == "limit"
        assert not store.path(receipt["id"]).exists()
        assert store.status(receipt["id"], owner())["state"] != "ready"
    asyncio.run(scenario())
