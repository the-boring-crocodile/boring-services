"""Actual authenticated SDK/HTTP, lifecycle races and streaming helper evidence."""
import asyncio
import hashlib
import json
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor
import httpx2
import pytest
from boring_service_kit.keys import add
from boring_service_kit.http import ServiceConfig
from boring_service_kit.outcomes import Refused
from boring_fishmarket.storage import Store,CHUNK
from boring_fishmarket.server import App,byte_range
from boring_fishmarket.service import browser_receipt, UNSAFE_URL_AUTH_PARAM
from boring_fishmarket.helper import TransferClient

URL="http://127.0.0.1:18861/fishmarket/mcp"
BASE=URL[:-4]
ORIGIN="http://127.0.0.1:18861"


@pytest.fixture
def market(tmp_path):
    root=tmp_path/"state"
    keys=tmp_path/"keys.json";_,key=add(keys,"one","writer",1,issuer="key:test")
    _,other=add(keys,"two","writer",1)
    _,reader=add(keys,"read","reader",1)
    config_path=tmp_path/"config.json"
    config_path.write_text(json.dumps({"url":URL,"keys":{"file":str(keys)}}))
    config=ServiceConfig(config_path)
    store=Store(root,max_file=32*1024**2,quota=64*1024**2)
    app=App(store,config,unsafe_url_auth=True)  # as deployed
    yield app,store,key,other,reader
    store.close()


def owner(subject="one"):return json.dumps(["key:test",subject],separators=(",",":"))


async def upload(client,receipt,data,name="file.txt"):
    token=receipt.get("upload_token",receipt.get("token"))
    async def chunks():
        for i in range(0,len(data),CHUNK):yield data[i:i+CHUNK]
    return await client.put(receipt.get("upload_url",BASE+"/api/"+receipt["id"]+"/upload"),
        content=chunks(),headers={"Content-Length":str(len(data)),
        "X-Fishmarket-Token":token,"X-Fishmarket-Filename":quote(name,safe="")})


async def authenticated(app,key):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),base_url=ORIGIN,
                              headers={"Authorization":"Bearer "+key})


def test_sdk_both_directions_helper_and_foreign_owner(market,tmp_path,caplog):
    caplog.set_level("INFO")
    async def run():
        app,store,key,other,reader=market
        async with app.web.router.lifespan_context(app.web):
            async with await authenticated(app,key) as http:
                async with TransferClient(URL,key,http) as client:
                    source=tmp_path/"hello-ä.txt";source.write_bytes(b"hello"*1000)
                    deposited=await client.deposit(source)
                    assert deposited["state"]=="ready"
                    assert "#" + UNSAFE_URL_AUTH_PARAM + "=" in deposited["open_url"]
                    assert store.status(deposited["id"],owner())["size"]==5000
                    inbound=await client.call("create_transfer",{"direction":"upload","max_bytes":10000})
                    assert (await upload(http,inbound,b"uploaded")).status_code==200
                    assert (await upload(http,inbound,b"again")).status_code==403
                    assert (await client.call("list_ready",{}))["transfers"][0]["id"]==inbound["id"]
                    # Existing destination refuses without collecting/acknowledging.
                    dest=tmp_path/"result";dest.write_bytes(b"preserved")
                    with pytest.raises(FileExistsError):await client.collect(inbound["id"],dest)
                    assert store.status(inbound["id"],owner())["state"]=="ready"
                    dest.unlink()
                    assert (await client.collect(inbound["id"],dest))["collected"]
                    assert dest.read_bytes()==b"uploaded"
                    assert not store.path(inbound["id"]).exists()
                    assert (await client.ack(inbound["id"],dest))["collected"]
                # Anonymous MCP is denied.
                anon=await http.post(URL,json={"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}},
                    headers={"Authorization":""})
                assert anon.status_code==401
            async with await authenticated(app,other) as http:
                async with TransferClient(URL,other,http) as client:
                    with pytest.raises(Refused):await client.call("transfer_status",{"transfer_id":deposited["id"]})
                    assert (await http.get(BASE+"/api/"+inbound["id"]+"/collect")).status_code==404
            async with await authenticated(app,reader) as http:
                async with TransferClient(URL,reader,http) as client:
                    with pytest.raises(Refused):await client.call("create_transfer",{"direction":"upload"})
    asyncio.run(run())
    assert market[2] not in caplog.text
    assert "hello-ä.txt" not in caplog.text
    assert "uploaded" not in caplog.text



# Every tool by role. acknowledge is a reader tool that writes: a granted collector
# acknowledges what it collected (test_review). A new tool fails the inventory below.
FISH_WRITER={"create_transfer","open_download","grant_collector","revoke_transfer"}
FISH_READER={"transfer_status","list_transfers","list_ready","collection_receipt","acknowledge"}


def test_reader_matrix_refuses_every_writer_tool_without_effect(market):
    async def run():
        app,store,key,other,reader=market
        assert {t.name for t in await app.server.list_tools()}==FISH_WRITER|FISH_READER
        owned=store.create(owner(),"upload",max_bytes=10)
        before=store.status(owned["id"],owner())
        attempts={"create_transfer":{"direction":"upload"},"open_download":{"transfer_id":owned["id"]},
                  "grant_collector":{"transfer_id":owned["id"],"collector":json.dumps(["key:test","read"])},
                  "revoke_transfer":{"transfer_id":owned["id"]}}
        assert set(attempts)==FISH_WRITER
        async with app.web.router.lifespan_context(app.web):
            async with await authenticated(app,reader) as http:
                async with TransferClient(URL,reader,http) as client:
                    for tool,arguments in attempts.items():
                        with pytest.raises(Refused) as refused:await client.call(tool,arguments)
                        assert refused.value.code=="forbidden",tool
                    # Reads pass the permission check and reach the store.
                    assert (await client.call("list_transfers",{}))["transfers"]==[]
                    assert (await client.call("list_ready",{}))["transfers"]==[]
                    with pytest.raises(Refused) as refused:
                        await client.call("transfer_status",{"transfer_id":owned["id"]})
                    assert refused.value.code=="not_found"
        assert store.status(owned["id"],owner())==before
        assert len(store.list_transfers(owner())["transfers"])==1
    asyncio.run(run())


def test_browser_claim_preview_origin_range_expiry(market):
    async def run():
        app,store,key,*_=market
        receipt=store.create(owner(),"download","file.txt",10,hashlib.sha256(b"0123456789").hexdigest())
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),base_url=ORIGIN) as a:
            assert (await upload(a,receipt,b"0123456789")).status_code==200
            link=browser_receipt(BASE, store.open_download(receipt["id"],owner()))
            # Passive page/head fetches do not claim.
            page=await a.get(link["url"])
            assert "file.txt" not in page.text
            assert "default-src 'none'" in page.headers["content-security-policy"]
            assert (await a.head(link["url"])).status_code==200
            assert store.status(receipt["id"],owner())["state"]=="ready"
            endpoint=BASE+"/api/"+receipt["id"]+"/claim"
            assert (await a.post(endpoint,json={"token":link["token"]})).status_code==403
            assert (await a.post(endpoint,json={"token":link["token"]},headers={"Origin":"https://evil.test"})).status_code==403
            claimed=await a.post(endpoint,json={"token":link["token"]},headers={"Origin":ORIGIN})
            assert claimed.status_code==200
            until=claimed.json()["retry_until"]
            assert (await a.post(endpoint,json={"token":link["token"]},headers={"Origin":ORIGIN})).json()["retry_until"]==until
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),base_url=ORIGIN) as b:
                await b.get(link["url"])
                assert (await b.post(endpoint,json={"token":link["token"]},headers={"Origin":ORIGIN})).status_code==403
            file=BASE+"/api/"+receipt["id"]+"/download"
            assert (await a.get(file,headers={"Range":"bytes=2-4"})).content==b"234"
            assert (await a.get(file,headers={"Range":"bytes=-2"})).content==b"89"
            response=await a.get(file)
            assert response.content==b"0123456789" and response.headers["content-disposition"].startswith("attachment")
            assert (await a.get(file,headers={"Range":"bytes=20-"})).status_code==416
            store.clock=lambda:until+1
            assert (await a.get(file)).status_code==404
            store.cleanup();assert not store.path(receipt["id"]).exists()
    asyncio.run(run())


def test_quota_races_claim_races_and_recovery(tmp_path):
    store=Store(tmp_path/"state",max_file=10,quota=10)
    try:
        def create(_):
            try:return store.create("owner","upload",max_bytes=10)
            except Refused:return None
        with ThreadPoolExecutor(8) as pool:results=list(pool.map(create,range(8)))
        good=[r for r in results if r];assert len(good)==1
        row=good[0];store.upload_start(row["id"],row["upload_token"],"a.txt",5)
        store.path(row["id"],".part").write_bytes(b"part")
        store.close()
        store=Store(tmp_path/"state",max_file=10,quota=10)
        assert store.status(row["id"],"owner")["state"]=="pending"
        assert not store.path(row["id"],".part").exists()
        store.revoke(row["id"],"owner")
        d=store.create("owner","download","b",1,hashlib.sha256(b"x").hexdigest())
        store.upload_start(d["id"],d["upload_token"],"b",1)
        store.path(d["id"],".part").write_bytes(b"x")
        store.upload_finish(d["id"],1,hashlib.sha256(b"x").hexdigest());store.release(d["id"])
        token=store.open_download(d["id"],"owner")["token"]
        def claim(n):
            try:store.claim(d["id"],token,str(n));return True
            except Refused:return False
        with ThreadPoolExecutor(8) as pool:assert sum(pool.map(claim,range(8)))==1
    finally:store.close()


def test_interruption_checksum_overflow_and_upload_race(market):
    async def run():
        app,store,key,*_=market
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),base_url=ORIGIN) as http:
            row=store.create(owner(),"download","file.txt",3,hashlib.sha256(b"yes").hexdigest())
            response=await upload(http,row,b"bad")
            assert response.status_code==500 and response.json()["code"]=="checksum"
            assert not list(store.payloads.iterdir())
            assert store.status(row["id"],owner())["state"]=="pending"
            assert (await upload(http,row,b"yes")).status_code==200
            inbound=store.create(owner(),"upload",max_bytes=10)
            started=asyncio.Event();resume=asyncio.Event()
            async def slow():
                yield b"a";started.set();await resume.wait();yield b"b"
            request=asyncio.create_task(http.put(BASE+"/api/"+inbound["id"]+"/upload",content=slow(),
                headers={"Content-Length":"2","X-Fishmarket-Token":inbound["upload_token"],
                         "X-Fishmarket-Filename":"file.txt"}))
            await started.wait()
            assert (await upload(http,inbound,b"x")).status_code==409
            resume.set();assert (await request).status_code==200
            assert store.path(inbound["id"]).read_bytes()==b"ab"  # the winner's bytes, not the loser's b"x"
            assert store.status(inbound["id"],owner())["state"]=="ready"
            assert store.status(inbound["id"],owner())["size"]==2
            assert store.status(inbound["id"],owner())["sha"]==hashlib.sha256(b"ab").hexdigest()
            assert not store.path(inbound["id"],".part").exists()
            broken=store.create(owner(),"upload",max_bytes=10)
            async def interrupted():yield b"a";raise OSError("synthetic disconnect")
            response=await http.put(BASE+"/api/"+broken["id"]+"/upload",content=interrupted(),
                headers={"Content-Length":"2","X-Fishmarket-Token":broken["upload_token"],
                         "X-Fishmarket-Filename":"file.txt"})
            assert response.status_code==500 and store.status(broken["id"],owner())["state"]=="pending"
            assert not store.path(broken["id"],".part").exists()
            response=await http.put(BASE+"/api/"+broken["id"]+"/upload",content=b"abcd",
                headers={"Content-Length":"2","X-Fishmarket-Token":broken["upload_token"],
                         "X-Fishmarket-Filename":"file.txt"})
            assert response.status_code==500 and not store.path(broken["id"]).exists()
            assert store.status(broken["id"],owner())["state"]=="pending"
            assert not store.path(broken["id"],".part").exists()
    asyncio.run(run())


def test_explicit_grants_ack_and_portable_names(market):
    app,store,*_=market
    row=store.create(owner(),"upload",max_bytes=10)
    store.grant(row["id"],owner(),owner("two"))
    assert store.status(row["id"],owner("two"))["id"]==row["id"]
    store.grant(row["id"],owner(),owner("two"),False)
    with pytest.raises(Refused):store.status(row["id"],owner("two"))
    for name in ("../a","a/b","a\\b","\r\n","",chr(0),"x"*241):
        with pytest.raises(Refused):store.create(owner(),"download",name,1,"a"*64)
    # Display name is not a filesystem name; Windows reserved names are harmless.
    assert store.create(owner(),"download","CON",1,"a"*64)["id"]


def test_no_plaintext_tokens_or_content_and_single_process(market):
    app,store,*_=market
    row=store.create(owner(),"upload",max_bytes=10)
    assert row["upload_token"].encode() not in (store.root/"transfers.sqlite").read_bytes()
    with pytest.raises(ValueError):Store(store.root)
    assert byte_range(None,0)==(0,0,200)


def test_open_url_only_with_the_opt_in():
    """The token travels in a link only when the operator chose --unsafe-url-auth."""
    from boring_fishmarket.service import browser_receipt
    receipt={"id":"a"*32,"direction":"download","token":"secret-token"}
    plain=browser_receipt("https://host/fishmarket",receipt)
    assert plain["url"]=="https://host/fishmarket/download/"+"a"*32 and plain["token"]=="secret-token"
    assert "open_url" not in plain
    opened=browser_receipt("https://host/fishmarket",receipt,True)
    assert opened["open_url"]==plain["url"]+"#"+UNSAFE_URL_AUTH_PARAM+"=secret-token"
