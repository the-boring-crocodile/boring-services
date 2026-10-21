"""Review regressions: lifecycle ownership and truthful MCP effects."""

import asyncio
import hashlib
import sqlite3
from contextlib import closing

import pytest
from boring_service_kit.outcomes import Refused
from boring_fishmarket.storage import Store
from boring_fishmarket.helper import TransferClient
from test_market import market as market_fixture, authenticated, URL

# Register the existing fixture; its implementation remains in test_market.
market = market_fixture


def publish(store, owner="owner"):
    receipt = store.create(owner, "upload", max_bytes=10)
    transfer_id = receipt["id"]
    store.upload_start(transfer_id, receipt["upload_token"], "file.txt", 1)
    store.path(transfer_id, ".part").write_bytes(b"x")
    store.upload_finish(transfer_id, 1, hashlib.sha256(b"x").hexdigest())
    store.release(transfer_id)
    return transfer_id


def test_failed_startup_releases_process_lock(tmp_path):
    root = tmp_path / "state"
    store = Store(root)
    transfer_id = publish(store)
    payload = store.path(transfer_id)
    store.close()
    payload.unlink()
    with pytest.raises(RuntimeError, match="published payload missing") as failure:
        Store(root)
    # Keep the traceback alive: garbage collection must not own lock release.
    assert failure.value
    payload.write_bytes(b"x")
    recovered = Store(root)
    recovered.close()


def test_expired_missing_payload_does_not_block_startup(tmp_path):
    root = tmp_path / "state"
    store = Store(root, lifetime=10, clock=lambda: 100)
    transfer_id = publish(store)
    store.path(transfer_id).unlink()
    store.close()
    recovered = Store(root, clock=lambda: 111)
    try:
        assert recovered.status(transfer_id, "owner")["state"] == "expired"
    finally:
        recovered.close()


def test_terminal_history_cannot_hide_active_receipts(tmp_path):
    store = Store(tmp_path / "state", max_file=10, quota=10)
    try:
        for _ in range(100):
            old = store.create("owner", "upload", max_bytes=1)
            store.revoke(old["id"], "owner")
        active = store.create("owner", "upload", max_bytes=1)
        assert store.list_transfers("owner")["transfers"][0]["id"] == active["id"]
    finally:
        store.close()


def test_revoking_collected_transfer_preserves_ack_retry(tmp_path):
    store = Store(tmp_path / "state")
    try:
        transfer_id = publish(store)
        sha = hashlib.sha256(b"x").hexdigest()
        store.ack(transfer_id, "owner", 1, sha)
        store.revoke(transfer_id, "owner")
        assert store.ack(transfer_id, "owner", 1, sha) == {"collected": True}
    finally:
        store.close()


def test_acknowledgement_is_a_write_with_reader_permission(market):
    from test_market import owner

    async def run():
        app, store, key, _, reader = market
        async with app.web.router.lifespan_context(app.web):
            async with await authenticated(app, key) as http:
                async with TransferClient(URL, key, http) as client:
                    tools = (await client.session.list_tools()).tools
                    ack = next(tool for tool in tools if tool.name == "acknowledge")
                    assert ack.annotations.read_only_hint is False
                transfer_id = publish(store, owner())
                store.grant(transfer_id, owner(), owner("read"))
                async with TransferClient(URL, reader, http) as client:
                    assert await client.call(
                        "acknowledge",
                        {
                            "transfer_id": transfer_id,
                            "size": 1,
                            "sha": hashlib.sha256(b"x").hexdigest(),
                        },
                    ) == {"collected": True}
                with closing(sqlite3.connect(store.root / "transfers.sqlite")) as db:
                    stored = db.execute("SELECT state FROM transfers WHERE id=?", (transfer_id,)).fetchone()
                assert stored == ("collected",)
                assert not store.path(transfer_id).exists()

    asyncio.run(run())


def test_revoked_grant_stops_active_stream_and_closes_reader(market):
    from starlette.requests import Request
    from boring_fishmarket.storage import CHUNK

    async def run():
        app, store, *_ = market
        receipt = store.create("owner", "upload", max_bytes=2 * CHUNK)
        transfer_id = receipt["id"]
        store.upload_start(transfer_id, receipt["upload_token"], "file.txt", 2 * CHUNK)
        data = b"x" * (2 * CHUNK)
        store.path(transfer_id, ".part").write_bytes(data)
        store.upload_finish(transfer_id, len(data), hashlib.sha256(data).hexdigest())
        store.release(transfer_id)
        store.grant(transfer_id, "owner", "collector")
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "headers": [],
                "path_params": {"transfer_id": transfer_id},
            }
        )
        response = await app.file(request, caller="collector")
        sent = []

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                sent.append(len(message["body"]))
                store.grant(transfer_id, "owner", "collector", False)

        async def receive():
            await asyncio.Event().wait()

        with pytest.raises(RuntimeError, match="access revoked"):
            await response(
                {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send
            )
        assert sent == [CHUNK]
        assert transfer_id not in store.active
        assert store.path(transfer_id).exists()
        with pytest.raises(Refused):
            store.collection_receipt(transfer_id, "collector")
        assert store.collection_receipt(transfer_id, "owner")["size"] == 2 * CHUNK

    asyncio.run(run())


def test_ready_list_uses_one_expiry_snapshot(tmp_path):
    store = Store(tmp_path / "state", lifetime=10, clock=lambda: 100)
    try:
        transfer_id = publish(store)
        times = iter((109, 111))
        store.clock = lambda: next(times)
        assert store.list_ready("owner")["transfers"][0]["id"] == transfer_id
        assert store.list_ready("owner")["transfers"] == []
    finally:
        store.close()


def test_deposit_rejects_irrelevant_capacity_before_effects(tmp_path):
    store = Store(tmp_path / "state")
    try:
        with pytest.raises(Refused, match="not max_bytes"):
            store.create("owner", "download", "file.txt", 1, "a" * 64, max_bytes=10)
        assert store.list_transfers("owner")["transfers"] == []
    finally:
        store.close()


def test_local_cleanup_failure_after_publication_preserves_remote(
    market, tmp_path, monkeypatch
):
    from pathlib import Path
    from boring_fishmarket.helper import AcknowledgementUnknown

    async def run():
        app, store, key, *_ = market
        async with app.web.router.lifespan_context(app.web):
            async with await authenticated(app, key) as http:
                async with TransferClient(URL, key, http) as client:
                    from test_market import owner

                    transfer_id = publish(store, owner())
                    destination = tmp_path / "received"
                    original = Path.unlink

                    def fail_temporary(path, *args, **kwargs):
                        if path.name.startswith(".fishmarket-"):
                            raise PermissionError()
                        return original(path, *args, **kwargs)

                    with monkeypatch.context() as patch:
                        patch.setattr(Path, "unlink", fail_temporary)
                        with pytest.raises(AcknowledgementUnknown):
                            await client.collect(transfer_id, destination)
                    assert destination.read_bytes() == b"x"
                    assert store.path(transfer_id).exists()
                    for temporary in tmp_path.glob(".fishmarket-*"):
                        temporary.unlink()
                    assert (await client.ack(transfer_id, destination))["collected"]

    asyncio.run(run())


def test_helper_restores_supplied_client_authorization(market):
    async def run():
        app, _, key, other, *_ = market
        async with app.web.router.lifespan_context(app.web):
            async with await authenticated(app, other) as http:
                previous = http.headers["Authorization"]
                async with TransferClient(URL, key, http) as client:
                    assert (await client.call("list_ready", {}))["transfers"] == []
                assert http.headers["Authorization"] == previous

    asyncio.run(run())


def test_download_waits_for_the_store_lock_off_the_event_loop(market, monkeypatch):
    """While another thread holds the store lock, a download waits for it in
    a worker thread: the event loop keeps serving, the download then completes.
    Signals, not sleeps, decide the order; the bounds only stop a broken run."""
    import threading
    from contextlib import contextmanager
    import httpx2
    from boring_fishmarket.storage import CHUNK
    from boring_fishmarket.service import browser_receipt
    from test_market import BASE, ORIGIN, owner, upload

    app, store, *_ = market
    data = bytes(range(256)) * (2 * CHUNK // 256 + 1)  # more than two chunks
    receipt = store.create(owner(), "download", "big.bin", len(data), hashlib.sha256(data).hexdigest())
    # The download opens its payload through store.reader, which takes the
    # store lock first: signal (and note the thread) just before it does.
    about_to_wait, waiting_threads = threading.Event(), []
    reader = store.reader

    @contextmanager
    def signalling_reader(*args, **kwargs):
        waiting_threads.append(threading.get_ident())
        about_to_wait.set()
        with reader(*args, **kwargs) as opened:
            yield opened

    async def run():
        loop_thread = threading.get_ident()
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ORIGIN) as browser:
            assert (await upload(browser, receipt, data, "big.bin")).status_code == 200
            link = browser_receipt(BASE, store.open_download(receipt["id"], owner()))
            await browser.get(link["url"])
            claimed = await browser.post(BASE + "/api/" + receipt["id"] + "/claim",
                                         json={"token": link["token"]}, headers={"Origin": ORIGIN})
            assert claimed.status_code == 200
            monkeypatch.setattr(store, "reader", signalling_reader)
            held, done, released_by_the_loop = threading.Event(), threading.Event(), []

            def hold():
                with store.lock:
                    held.set()
                    # Bounded: a loop blocked on the lock never sets done, so
                    # this times out and the test fails instead of hanging.
                    released_by_the_loop.append(done.wait(30))

            holder = threading.Thread(target=hold)
            holder.start()
            try:
                assert await asyncio.to_thread(held.wait, 30)
                download = asyncio.create_task(browser.get(BASE + "/api/" + receipt["id"] + "/download"))
                assert await asyncio.to_thread(about_to_wait.wait, 30)
                assert waiting_threads and waiting_threads[0] != loop_thread
                # The download is now waiting for the held lock; the loop must still serve.
                health = await asyncio.wait_for(browser.get(BASE + "/health"), 30)
                assert health.status_code == 200 and not download.done()
            finally:
                done.set()
                holder.join()
            assert released_by_the_loop == [True]
            response = await asyncio.wait_for(download, 60)
            assert response.status_code == 200 and response.content == data

    asyncio.run(run())
    assert receipt["id"] not in store.active


@pytest.mark.parametrize("claims", [{}, {"boring_caller": "someone"}, {"boring_caller": {"role": "writer"}}])
def test_collect_without_a_service_identity_is_refused(market, monkeypatch, claims):
    import httpx2
    from test_market import BASE, ORIGIN

    app, store, key, *_ = market
    verifier = app.config.token_verifier

    class WithoutIdentity:
        async def verify_token(self, token):
            access = await verifier.verify_token(token)
            access.claims = claims
            return access

    monkeypatch.setattr(app.config, "token_verifier", WithoutIdentity())

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ORIGIN,
                                      headers={"Authorization": "Bearer " + key}) as http:
            response = await http.get(BASE + "/api/" + "0" * 32 + "/collect")
            assert response.status_code == 403
            assert response.json()["code"] == "forbidden"

    asyncio.run(run())


@pytest.mark.parametrize("path", ["/fishmarket//mcp", "/fishmarket/../mcp", "/mcp"])
def test_browser_mount_rejects_ambiguous_prefix(market, path):
    from copy import copy
    from boring_fishmarket.server import App

    app, store, *_ = market
    config = copy(app.config)
    config.url = "http://127.0.0.1:18861" + path
    with pytest.raises(ValueError, match="safe path prefix"):
        App(store, config)
