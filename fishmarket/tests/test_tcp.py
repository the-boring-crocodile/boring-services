"""Real sockets and browser; temporary fixture only, no live endpoints."""
import asyncio
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
import pytest
import uvicorn
from boring_service_kit.keys import add
from boring_service_kit.http import ServiceConfig
from boring_fishmarket.storage import Store,CHUNK
from boring_fishmarket.server import App
from boring_fishmarket.helper import TransferClient,digest_file


@pytest.fixture
def tcp_market(tmp_path):
    sock=socket.socket();sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
    base=f"http://127.0.0.1:{port}/fishmarket"
    keys=tmp_path/"keys.json";_,key=add(keys,"fixture","writer",1,issuer="key:tcp")
    config_file=tmp_path/"config.json";config_file.write_text(json.dumps(
        {"url":base+"/mcp","port":port,"keys":{"file":str(keys)}}))
    store=Store(tmp_path/"state",max_file=32*1024**2,quota=64*1024**2)
    app=App(store,ServiceConfig(config_file),unsafe_url_auth=True)  # the browser test opens open_url
    server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=port,
                         access_log=False,log_level="critical",timeout_graceful_shutdown=2))
    thread=threading.Thread(target=server.run,kwargs={"sockets":[sock]});thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started:
            if not thread.is_alive() or time.monotonic()>deadline:raise RuntimeError("fixture startup failed")
            time.sleep(.02)
        yield base, key
    finally:
        server.should_exit=True;thread.join(timeout=10);sock.close()
        assert not thread.is_alive(),"fixture process did not exit"
        store.close()


@pytest.fixture
def large_source(tmp_path):
    source=tmp_path/"large.bin"
    chunk=b"z"*CHUNK
    with source.open("wb") as f:
        for _ in range(64):f.write(chunk)
    return source


def test_tcp_large_stream_helper(tcp_market, tmp_path, large_source):
    base, key = tcp_market
    source = large_source

    async def run():
        if os.name!="nt":
            import resource
            baseline=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        async with TransferClient(base+"/mcp",key) as client:
            await client.deposit(source)
            inbound=await client.call("create_transfer",{"direction":"upload","max_bytes":source.stat().st_size})
            async def chunks():
                with source.open("rb") as f:
                    for piece in iter(lambda:f.read(CHUNK),b""):yield piece
            result=await client.http.put(inbound["upload_url"],content=chunks(),
                headers={"Content-Length":str(source.stat().st_size),"X-Fishmarket-Token":inbound["token"],
                         "X-Fishmarket-Filename":"large.bin"})
            assert result.status_code==200
            target=tmp_path/"collected.bin"
            assert (await client.collect(inbound["id"],target))["size"]==16*1024**2
            assert digest_file(source)==digest_file(target)
            assert (await client.call("transfer_status",{"transfer_id":inbound["id"]}))["state"]=="collected"
            if os.name!="nt":
                peak_growth=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss-baseline
                print(f"Real TCP 16 MiB deposit/upload/collection peak RSS growth: {peak_growth} KiB")
                assert peak_growth<12*1024, "streaming must not accumulate a whole 16 MiB file"
    asyncio.run(run())


def test_tcp_firefox(tmp_path, large_source, request):
    # Skip before starting the server or any async lifespan. TCP acceptance is
    # a separate test and cannot be hidden by a missing browser runtime.
    runtime = Path("/home/boring-crocodile/lab/canvas-browser")
    if not (runtime / "node_modules/playwright").is_dir():
        pytest.skip("Firefox acceptance requires the browser runtime")
    base, key = request.getfixturevalue("tcp_market")
    source = large_source

    async def run():
        async with TransferClient(base + "/mcp", key) as client:
            download = await client.deposit(source)
            upload=await client.call("create_transfer",{"direction":"upload","max_bytes":CHUNK})
            (tmp_path/"browser-source.bin").write_bytes(b"browser content")
            fixture=tmp_path/"browser.json"
            fixture.write_text(json.dumps({"directory":str(tmp_path),"download":download,"upload":upload}))
            environment=dict(os.environ)
            environment["PLAYWRIGHT_MODULE"]=str(runtime/"node_modules/playwright")
            environment["PLAYWRIGHT_BROWSERS_PATH"]=str(runtime/"browsers")
            result=await asyncio.to_thread(subprocess.run,
                ["node",str(Path(__file__).with_name("browser.mjs")),str(fixture)],
                env=environment,capture_output=True,text=True,timeout=45)
            assert result.returncode==0, result.stdout+result.stderr
            assert digest_file(tmp_path/"browser-result.bin")==digest_file(source)
            target2=tmp_path/"browser-upload-collected.bin"
            await client.collect(upload["id"],target2)
            assert target2.read_bytes()==b"browser content"
            # consumed upload token refuses even after collection.
            response=await client.http.put(upload["upload_url"],content=b"x",
                headers={"Content-Length":"1","X-Fishmarket-Token":upload["token"],
                         "X-Fishmarket-Filename":"x"})
            assert response.status_code==403
            print(result.stdout.strip())
    asyncio.run(run())
