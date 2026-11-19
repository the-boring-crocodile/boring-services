"""An upload that lost its active transfer state cannot publish."""
import asyncio
import hashlib
import httpx2
import pytest
from boring_fishmarket.storage import Store
from boring_service_kit.outcomes import Failed
from test_market import market as market_fixture, owner, ORIGIN, BASE

market = market_fixture


def test_expired_publication_is_failed_stale(tmp_path):
    now = [100.0]
    store = Store(tmp_path / "state", lifetime=10, clock=lambda: now[0])
    try:
        receipt = store.create("owner", "upload", max_bytes=10)
        store.upload_start(receipt["id"], receipt["upload_token"], "file", 1)
        store.path(receipt["id"], ".part").write_bytes(b"x")
        now[0] = 111.0
        with pytest.raises(Failed) as stopped:
            store.upload_finish(receipt["id"], 1, hashlib.sha256(b"x").hexdigest())
        assert stopped.value.code == "stale"
        assert not store.path(receipt["id"]).exists()
    finally:
        store.close()


def test_revoked_stream_is_failed_stale_without_publication(market):
    async def scenario():
        app, store, *_ = market
        receipt = store.create(owner(), "upload", max_bytes=10)
        async def chunks():
            # Revoke only after the route has admitted the upload.
            store.revoke(receipt["id"], owner())
            yield b"x"
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ORIGIN) as client:
            response = await client.put(BASE + "/api/" + receipt["id"] + "/upload",
                content=chunks(), headers={"Content-Length": "1",
                "X-Fishmarket-Token": receipt["upload_token"], "X-Fishmarket-Filename": "file"})
        assert response.json()["status"] == "failed"
        assert response.json()["code"] == "stale"
        assert not store.path(receipt["id"]).exists()
    asyncio.run(scenario())
