"""Failure boundaries which must preserve a recoverable transfer."""
import asyncio
import hashlib
from pathlib import Path
import pytest
from boring_fishmarket.storage import Store
from boring_fishmarket.server import FileResponse
from boring_fishmarket.helper import TransferClient,AcknowledgementUnknown
from test_market import market,authenticated,upload,owner,URL  # noqa: F401  (pytest fixture)


def test_local_checksum_and_lost_ack_preserve_remote(market,tmp_path):  # noqa: F811
    async def run():
        app,store,key,*_=market
        async with app.web.router.lifespan_context(app.web):
            async with await authenticated(app,key) as http:
                async with TransferClient(URL,key,http) as client:
                    receipt=await client.call("create_transfer",{"direction":"upload","max_bytes":10})
                    assert (await upload(http,receipt,b"good")).status_code==200
                    store.path(receipt["id"]).write_bytes(b"evil")
                    destination=tmp_path/"verified"
                    with pytest.raises(RuntimeError,match="checksum"):
                        await client.collect(receipt["id"],destination)
                    assert not destination.exists()
                    assert not list(tmp_path.glob(".fishmarket-*"))
                    assert store.status(receipt["id"],owner())["state"]=="ready"
                    store.path(receipt["id"]).write_bytes(b"good")
                    original=client.call
                    async def lost_ack(name,args):
                        if name=="acknowledge":raise ConnectionError()
                        return await original(name,args)
                    client.call=lost_ack
                    with pytest.raises(AcknowledgementUnknown):
                        await client.collect(receipt["id"],destination)
                    assert destination.read_bytes()==b"good"
                    assert store.path(receipt["id"]).exists()
                    client.call=original
                    assert (await client.ack(receipt["id"],destination))["collected"]
                    assert not store.path(receipt["id"]).exists()
                    recovery=await client.call("list_transfers",{})
                    assert recovery["transfers"][0]["state"]=="collected"
    asyncio.run(run())


def test_stalled_socket_at_expiry_closes_reader_and_releases_quota(tmp_path):
    # The store's clock is the test's: liveness never depends on how fast the
    # host runs. Only the stall itself is real (FileResponse waits 50 ms).
    now=[1000.0]
    store=Store(tmp_path/"state",max_file=10,quota=10,clock=lambda:now[0])
    try:
        receipt=store.create("owner","download","a",1,hashlib.sha256(b"x").hexdigest())
        id=receipt["id"]
        store.upload_start(id,receipt["upload_token"],"a",1)
        store.path(id,".part").write_bytes(b"x")
        store.upload_finish(id,1,hashlib.sha256(b"x").hexdigest());store.release(id)
        token=store.open_download(id,"owner")["token"]
        store.claim(id,token,"browser")
        until=now[0]+.05
        with store.db:store.db.execute("UPDATE transfers SET claim_until=? WHERE id=?",(until,id))
        context=store.reader(id,browser="browser");row,handle=context.__enter__()
        async def run():
            async def chunks():yield handle.read(1)
            async def receive():await asyncio.Event().wait()
            async def send(message):
                now[0]=until+1  # the socket stalls past the claim's deadline
                await asyncio.Event().wait()
            response=FileResponse(chunks(),close=lambda:context.__exit__(None,None,None),
                                  deadline=until,clock=store.clock)
            with pytest.raises(asyncio.TimeoutError):
                await response({"type":"http","asgi":{"spec_version":"2.4"}},receive,send)
        asyncio.run(run())
        assert handle.closed and id not in store.active
        assert not store.path(id).exists()
        assert store.db.execute("SELECT sum(reserved) FROM transfers").fetchone()[0]==0
    finally:store.close()


def test_delete_failure_keeps_quota_and_retries(tmp_path,monkeypatch):
    store=Store(tmp_path/"state",max_file=10,quota=10)
    try:
        receipt=store.create("owner","upload",max_bytes=10);id=receipt["id"]
        store.upload_start(id,receipt["upload_token"],"a",1)
        store.path(id,".part").write_bytes(b"x")
        store.upload_finish(id,1,hashlib.sha256(b"x").hexdigest());store.release(id)
        original=Path.unlink
        def fail(path,*args,**kwargs):
            if path==store.path(id):raise PermissionError()
            return original(path,*args,**kwargs)
        monkeypatch.setattr(Path,"unlink",fail)
        with pytest.raises(PermissionError):store.revoke(id,"owner")
        assert store.status(id,"owner")["state"]=="deleted"
        assert store.db.execute("SELECT sum(reserved) FROM transfers").fetchone()[0]==1
        monkeypatch.setattr(Path,"unlink",original)
        store.cleanup()
        assert store.db.execute("SELECT sum(reserved) FROM transfers").fetchone()[0]==0
    finally:store.close()
