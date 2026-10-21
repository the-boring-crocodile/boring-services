"""Upload disk flush must leave the event loop responsive."""
import asyncio
import os
import threading
from unittest.mock import patch
from test_market import market, authenticated, upload, owner  # noqa: F401  (pytest fixture)

def test_upload_fsync_allows_unrelated_task(market):  # noqa: F811
    entered, release = threading.Event(), threading.Event()
    missed = []
    original = os.fsync
    def slow_fsync(fd):
        entered.set()
        if not release.wait(.3):
            missed.append(True)
        original(fd)
    async def scenario():
        app, store, key, _, _ = market
        receipt = store.create(owner(), "upload", max_bytes=1024)
        async def unrelated():
            while not entered.is_set():
                await asyncio.sleep(.001)
            release.set()
        async with await authenticated(app, key) as http:
            observer = asyncio.create_task(unrelated())
            try:
                result = await upload(http, receipt, b"fixture")
                assert result.status_code == 200, result.text
                await observer
            finally:
                release.set()
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
    with patch("os.fsync", slow_fsync):
        asyncio.run(scenario())
    assert entered.is_set()
    assert not missed, "fsync prevented the unrelated event-loop task from running"

def test_cancelled_upload_waits_for_admission_then_releases(market):  # noqa: F811
    entered, release = threading.Event(), threading.Event()
    app, store, key, _, _ = market
    original = store.upload_start
    def paused_start(*args):
        row = original(*args)
        entered.set()
        assert release.wait(2)
        return row
    async def scenario():
        receipt = store.create(owner(), "upload", max_bytes=1024)
        async with await authenticated(app, key) as http:
            task = asyncio.create_task(upload(http, receipt, b"fixture"))
            try:
                while not entered.is_set():
                    if task.done(): await task
                    await asyncio.sleep(.001)
                task.cancel()
                await asyncio.sleep(.01)
                assert not task.done(), "upload abandoned in-flight disk admission"
                release.set()
                try: await task
                except asyncio.CancelledError: pass
                else: raise AssertionError("cancelled upload returned success")
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert not store.active
        assert store._row(receipt["id"])["state"] == "pending"
        assert not store.path(receipt["id"], ".part").exists()
    with patch.object(store, "upload_start", paused_start):
        asyncio.run(scenario())
