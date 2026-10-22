"""Collection by token: an agent inside a Pond never holds the service bearer.

collection_receipt issues a token for one ready upload: COLLECT_SECONDS from
issue, never past the transfer, any number of reads; a new receipt replaces it,
acknowledging or revoking ends it.
"""

import asyncio
import hashlib

import httpx2
import pytest
from boring_service_kit.outcomes import Refused
from boring_fishmarket.storage import COLLECT_SECONDS, Store
from boring_fishmarket.helper import TransferClient
from test_market import market as market_fixture, authenticated, upload, URL, BASE, ORIGIN

market = market_fixture


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def published(store, data=b"x", owner="owner"):
    receipt = store.create(owner, "upload", max_bytes=max(1, len(data)))
    transfer_id = receipt["id"]
    store.upload_start(transfer_id, receipt["upload_token"], "file.txt", len(data))
    store.path(transfer_id, ".part").write_bytes(data)
    store.upload_finish(transfer_id, len(data), hashlib.sha256(data).hexdigest())
    store.release(transfer_id)
    return transfer_id


def readable(store, transfer_id, token):
    return store.valid_stream(transfer_id, collect=token)


def test_token_window_rotation_and_end(tmp_path):
    clock = Clock()
    store = Store(tmp_path / "state", lifetime=86400, clock=clock)
    try:
        transfer_id = published(store)
        first = store.collection_receipt(transfer_id, "owner")
        assert first["token_until"] == clock.now + COLLECT_SECONDS
        assert readable(store, transfer_id, first["token"])
        assert readable(store, transfer_id, first["token"])  # multi-use
        assert not readable(store, transfer_id, "not-the-token")

        second = store.collection_receipt(transfer_id, "owner")
        assert not readable(store, transfer_id, first["token"])  # rotated
        assert readable(store, transfer_id, second["token"])

        clock.now += COLLECT_SECONDS
        assert not readable(store, transfer_id, second["token"])  # window over
        third = store.collection_receipt(transfer_id, "owner")
        assert readable(store, transfer_id, third["token"])

        size = store.status(transfer_id, "owner")["size"]
        sha = store.status(transfer_id, "owner")["sha"]
        store.ack(transfer_id, "owner", size, sha)
        assert not readable(store, transfer_id, third["token"])  # acknowledged
    finally:
        store.close()


def test_token_never_outlives_the_transfer(tmp_path):
    clock = Clock()
    store = Store(tmp_path / "state", lifetime=300, clock=clock)
    try:
        transfer_id = published(store)
        receipt = store.collection_receipt(transfer_id, "owner")
        assert receipt["token_until"] == receipt["expires"] < clock.now + COLLECT_SECONDS
        clock.now = receipt["expires"]
        assert not readable(store, transfer_id, receipt["token"])
    finally:
        store.close()


def test_revoke_ends_the_token_and_only_collectors_get_one(tmp_path):
    store = Store(tmp_path / "state")
    try:
        transfer_id = published(store)
        with pytest.raises(Refused):
            store.collection_receipt(transfer_id, "stranger")
        receipt = store.collection_receipt(transfer_id, "owner")
        store.revoke(transfer_id, "owner")
        assert not readable(store, transfer_id, receipt["token"])
        # A token belongs to one transfer: it cannot read another.
        other = published(store)
        assert not readable(store, other, receipt["token"])
    finally:
        store.close()


def test_agent_collects_over_http_without_the_bearer(market):
    app, store, key, *_ = market
    data = bytes(range(256)) * 2000

    async def run():
        async with app.web.router.lifespan_context(app.web):
            async with await authenticated(app, key) as http:
                async with TransferClient(URL, key, http) as client:
                    inbound = await client.call("create_transfer", {"direction": "upload", "max_bytes": len(data)})
                    assert (await upload(http, inbound, data)).status_code == 200
                    receipt = await client.call("collection_receipt", {"transfer_id": inbound["id"]})
            # From here as an agent would: no Authorization header at all.
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ORIGIN) as agent:
                url = BASE + "/api/" + inbound["id"] + "/collect"
                assert (await agent.get(url)).status_code == 403
                assert (await agent.get(url, headers={"X-Fishmarket-Token": "wrong"})).status_code == 403
                assert (await agent.get(url, headers={"X-Fishmarket-Token": "x" * 129})).status_code == 403
                header = {"X-Fishmarket-Token": receipt["token"]}
                whole = await agent.get(url, headers=header)
                assert whole.status_code == 200 and whole.content == data
                assert hashlib.sha256(whole.content).hexdigest() == receipt["sha"]
                part = await agent.get(url, headers={**header, "Range": "bytes=100-"})
                assert part.status_code == 206 and part.content == data[100:]
            async with await authenticated(app, key) as http:
                async with TransferClient(URL, key, http) as client:
                    done = await client.call("acknowledge", {"transfer_id": inbound["id"], "size": receipt["size"], "sha": receipt["sha"]})
                    assert done["collected"]
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ORIGIN) as agent:
                assert (await agent.get(url, headers=header)).status_code == 403

    asyncio.run(run())
