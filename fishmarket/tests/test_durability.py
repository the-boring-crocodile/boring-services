"""A published name is made durable (directory fsync) before it counts:
before the collector acknowledges, and before the service marks an upload ready."""
import asyncio
import errno
import hashlib
import os
import stat

import pytest

from boring_fishmarket import storage
from boring_fishmarket.helper import AcknowledgementWithheld, TransferClient
from boring_fishmarket.storage import Store
from boring_service_kit.outcomes import Failed
from test_market import market, authenticated, upload, owner, URL  # noqa: F401  (pytest fixture)

posix_only = pytest.mark.skipif(os.name == "nt", reason="directory fsync is POSIX; Windows makes no claim")


def watch_fsync(monkeypatch, events, directory, fail=False):
    """os.fsync, recording a flush of `directory` (or failing it, EIO)."""
    real, wanted = os.fsync, os.stat(directory)

    def fsync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (wanted.st_dev, wanted.st_ino):
            if fail:
                raise OSError(errno.EIO, "injected flush failure")
            events.append("flush-directory")
        return real(fd)

    monkeypatch.setattr(os, "fsync", fsync)


@posix_only
def test_collect_publishes_then_flushes_then_acknowledges(market, tmp_path, monkeypatch):  # noqa: F811
    async def run():
        app, store, key, *_ = market
        async with app.web.router.lifespan_context(app.web):
            async with await authenticated(app, key) as http:
                async with TransferClient(URL, key, http) as client:
                    receipt = await client.call("create_transfer", {"direction": "upload", "max_bytes": 10})
                    assert (await upload(http, receipt, b"good")).status_code == 200
                    events = []
                    link = os.link
                    monkeypatch.setattr(os, "link", lambda *a, **k: (events.append("publish"), link(*a, **k))[1])
                    watch_fsync(monkeypatch, events, tmp_path)
                    original = client.call

                    async def call(name, args):
                        if name == "acknowledge":
                            events.append("acknowledge")
                        return await original(name, args)

                    client.call = call
                    result = await client.collect(receipt["id"], tmp_path / "received.txt")
                    assert result["collected"] and result["directory_flushed"] is True
                    assert events == ["publish", "flush-directory", "acknowledge"]
    asyncio.run(run())


@posix_only
def test_failed_flush_withholds_the_acknowledgement(market, tmp_path, monkeypatch):  # noqa: F811
    async def run():
        app, store, key, *_ = market
        async with app.web.router.lifespan_context(app.web):
            async with await authenticated(app, key) as http:
                async with TransferClient(URL, key, http) as client:
                    receipt = await client.call("create_transfer", {"direction": "upload", "max_bytes": 10})
                    assert (await upload(http, receipt, b"good")).status_code == 200
                    acknowledged = []
                    original = client.call

                    async def call(name, args):
                        if name == "acknowledge":
                            acknowledged.append(args)
                        return await original(name, args)

                    client.call = call
                    destination = tmp_path / "received.txt"
                    with monkeypatch.context() as patch:
                        watch_fsync(patch, [], tmp_path, fail=True)
                        with pytest.raises(AcknowledgementWithheld, match="remote copy stays"):
                            await client.collect(receipt["id"], destination)
                        assert destination.read_bytes() == b"good"  # published, not durable
                        assert not list(tmp_path.glob(".fishmarket-*"))
                        with pytest.raises(AcknowledgementWithheld):
                            await client.ack(receipt["id"], destination)  # ack flushes first too
                    assert acknowledged == []
                    assert store.status(receipt["id"], owner())["state"] == "ready"
                    assert store.path(receipt["id"]).exists()  # the remote copy stays
                    assert (await client.ack(receipt["id"], destination))["collected"]
                    assert not store.path(receipt["id"]).exists()
    asyncio.run(run())


def deposit_part(store, data=b"x"):
    sha = hashlib.sha256(data).hexdigest()
    receipt = store.create("owner", "download", "a", len(data), sha)
    store.upload_start(receipt["id"], receipt["upload_token"], "a", len(data))
    store.path(receipt["id"], ".part").write_bytes(data)
    return receipt["id"], len(data), sha


def test_upload_payload_name_is_flushed_before_ready(tmp_path, monkeypatch):
    store = Store(tmp_path / "state")
    try:
        transfer_id, size, sha = deposit_part(store)
        seen = []
        real = storage.flush_directory

        def flush(directory):
            row = store._row(transfer_id)
            seen.append((directory, store.path(transfer_id).exists(), row["state"]))
            return real(directory)

        monkeypatch.setattr(storage, "flush_directory", flush)
        store.upload_finish(transfer_id, size, sha)
        assert seen == [(store.payloads, True, "uploading")]  # renamed, flushed, then ready
        assert store._row(transfer_id)["state"] == "ready"
    finally:
        store.close()


def test_upload_flush_failure_publishes_nothing(tmp_path, monkeypatch):
    store = Store(tmp_path / "state")
    try:
        transfer_id, size, sha = deposit_part(store)

        def fail(directory):
            raise OSError(errno.EIO, "injected flush failure")

        monkeypatch.setattr(storage, "flush_directory", fail)
        with pytest.raises(Failed, match="durable"):
            store.upload_finish(transfer_id, size, sha)
        store.upload_abort(transfer_id)  # what the upload route does on any failure
        assert store._row(transfer_id)["state"] == "pending"
        assert not store.path(transfer_id).exists()
        assert store.published(transfer_id) is None
    finally:
        store.close()


def test_windows_makes_no_flush_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(storage.os, "name", "nt")
    assert storage.flush_directory(tmp_path) is False
