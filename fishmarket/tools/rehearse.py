#!/usr/bin/env python3
"""Temporary loopback rehearsal; no production files, services or registration."""

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import httpx2
from boring_service_kit.keys import add
from boring_fishmarket.helper import TransferClient, digest_file


async def stop_child(process):
    """Own the same bounded shutdown for every fixture child."""
    if process.poll() is None:
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, 5)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait, 5)


async def rehearse(directory, port, self_test, lan_address=None, lan_port=18596):
    base = f"http://127.0.0.1:{port}/fishmarket"
    browser_base = (
        f"http://{lan_address}:{lan_port}/fishmarket" if lan_address else base
    )
    children = AsyncExitStack()
    keys = directory / "keys.json"
    _, token = add(keys, "rehearsal", "writer", 1, issuer="key:fishmarket-rehearsal")
    config = directory / "config.json"
    config.write_text(
        json.dumps(
            {
                "url": base + "/mcp",
                "bind": "127.0.0.1",
                "port": port,
                "keys": {"file": str(keys)},
            }
        )
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "boring_fishmarket.main",
            "--config",
            str(config),
            "--state-dir",
            str(directory / "state"),
            "--max-file-bytes",
            "104857600",
            "--quota-bytes",
            "209715200",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    children.push_async_callback(stop_child, process)
    try:
        async with httpx2.AsyncClient(trust_env=False, timeout=5) as http:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("fixture exited during startup")
                try:
                    if (await http.get(base + "/health")).status_code == 200:
                        break
                except httpx2.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("fixture startup timeout")
        if lan_address:
            relay = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("lan_browser.py")),
                    "--address",
                    lan_address,
                    "--port",
                    str(lan_port),
                    "--upstream-port",
                    str(port),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            children.push_async_callback(stop_child, relay)
            async with httpx2.AsyncClient(trust_env=False, timeout=5) as probe:
                for _ in range(100):
                    if relay.poll() is not None:
                        raise RuntimeError("LAN relay startup failed")
                    try:
                        if (
                            await probe.get(browser_base + "/web/market.js")
                        ).status_code == 200:
                            break
                    except httpx2.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("LAN relay startup timeout")
                assert (await probe.get(browser_base + "/mcp")).status_code == 403
        async with TransferClient(base + "/mcp", token) as client:
            source = directory / "hello-from-fishmarket.txt"
            source.write_text("<^==> Fishmarket says hello. One file, one handover.\n")
            outbound = await client.deposit(source)
            inbound = await client.call(
                "create_transfer", {"direction": "upload", "max_bytes": 104857600}
            )
            for receipt in (outbound, inbound):
                for field in ("url", "open_url", "upload_url"):
                    if field in receipt:
                        receipt[field] = receipt[field].replace(base, browser_base, 1)
            if self_test:
                async with httpx2.AsyncClient(trust_env=False) as browser:
                    await browser.get(outbound["url"])
                    claim = await browser.post(
                        browser_base + "/api/" + outbound["id"] + "/claim",
                        json={"token": outbound["token"]},
                        headers={"Origin": browser_base.removesuffix("/fishmarket")},
                    )
                    claim.raise_for_status()
                    downloaded = await browser.get(
                        browser_base.removesuffix("/fishmarket")
                        + claim.json()["download_url"]
                    )
                    assert downloaded.content == source.read_bytes()
                    response = await browser.put(
                        inbound["upload_url"],
                        content=b"hello back",
                        headers={
                            "Content-Length": "10",
                            "X-Fishmarket-Token": inbound["token"],
                            "X-Fishmarket-Filename": "reply.txt",
                        },
                    )
                    response.raise_for_status()
            else:
                print("DOWNLOAD: " + outbound["open_url"], flush=True)
                print("UPLOAD:   " + inbound["open_url"], flush=True)
                print(
                    "Download/save the greeting, then upload a disposable test file. Waiting 15 minutes.",
                    flush=True,
                )
            deadline = time.monotonic() + 900
            while True:
                ready = await client.call(
                    "transfer_status", {"transfer_id": inbound["id"]}
                )
                if ready["state"] == "ready":
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("rehearsal timed out")
                await asyncio.sleep(1)
            destination = directory / "collected-test-file"
            await client.collect(inbound["id"], destination)
            assert digest_file(destination) == (ready["size"], ready["sha"])
            assert (await client.ack(inbound["id"], destination))["collected"]
            refused = await client.http.put(
                base + "/api/" + inbound["id"] + "/upload",
                content=b"x",
                headers={
                    "Content-Length": "1",
                    "X-Fishmarket-Token": inbound["token"],
                    "X-Fishmarket-Filename": "retry.txt",
                },
            )
            assert refused.status_code == 403
            state = await client.call(
                "transfer_status", {"transfer_id": outbound["id"]}
            )
            assert state["state"] == "claimed", "Download button has not been used"
            print(
                "PASS: download claimed; upload collected/verified/acknowledged; consumed token refused.",
                flush=True,
            )
    finally:
        await children.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18595)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--temp-parent", type=Path, help="parent for disposable fixture state")
    parser.add_argument(
        "--lan-address", help="explicit private LAN IPv4 for browser-only relay"
    )
    parser.add_argument("--lan-port", type=int, default=18596)
    args = parser.parse_args()
    os.umask(0o077)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.port))
    if args.lan_address:
        with socket.socket() as probe:
            probe.bind((args.lan_address, args.lan_port))
    with tempfile.TemporaryDirectory(prefix="fishmarket-rehearsal-", dir=args.temp_parent) as temporary:
        asyncio.run(
            rehearse(
                Path(temporary),
                args.port,
                args.self_test,
                args.lan_address,
                args.lan_port,
            )
        )
    print("PASS: fixture stopped and all temporary files removed.")


if __name__ == "__main__":
    main()
