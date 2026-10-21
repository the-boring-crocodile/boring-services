"""Streaming client: local paths are consumed here, never by the service."""

from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from contextlib import AsyncExitStack
import tempfile
from urllib.parse import quote, urlsplit
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from boring_service_kit.outcomes import Refused, Failed
from .storage import CHUNK, flush_directory


def digest_file(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


class AcknowledgementUnknown(RuntimeError):
    """Verified local publication succeeded; remote acknowledgement needs retry."""


class AcknowledgementWithheld(RuntimeError):
    """The local file is published, but its directory entry could not be made
    durable; nothing was acknowledged, so the remote copy stays."""


def durable_publication(path):
    """Flush the directory holding `path` before an acknowledgement lets the
    service dispose of its copy. True when flushed, False where the platform
    offers no flush (Windows, see flush_directory); a failed flush raises
    AcknowledgementWithheld."""
    try:
        return flush_directory(path.parent)
    except OSError as exc:
        raise AcknowledgementWithheld(
            f"local file published, but its directory could not be flushed ({exc.strerror or exc}); "
            "not acknowledged, the remote copy stays: retry ack with this ID/file"
        ) from None


class TransferClient:
    def __init__(self, url, token, http=None):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or (
                parsed.scheme == "http"
                and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
            or not parsed.path.endswith("/mcp")
        ):
            raise ValueError("MCP URL needs HTTPS (loopback HTTP fixtures only)")
        self.url, self.base, self.token = url, url[:-4], token
        self.http = http
        self.session = None

    def checked_url(self, url):
        if (
            not url.startswith(self.base + "/api/")
            or urlsplit(url).query
            or urlsplit(url).fragment
        ):
            raise RuntimeError("service returned an unexpected transfer endpoint")
        return url

    async def __aenter__(self):
        self.stack = AsyncExitStack()
        try:
            if self.http is None:
                self.http = await self.stack.enter_async_context(
                    httpx2.AsyncClient(
                        timeout=600, trust_env=False, follow_redirects=False
                    )
                )
            previous = self.http.headers.get("Authorization")

            def restore_authorization():
                if previous is None:
                    self.http.headers.pop("Authorization", None)
                else:
                    self.http.headers["Authorization"] = previous

            self.stack.callback(restore_authorization)
            self.http.headers["Authorization"] = "Bearer " + self.token
            streams = await self.stack.enter_async_context(
                streamable_http_client(self.url, http_client=self.http)
            )
            self.session = await self.stack.enter_async_context(
                ClientSession(streams[0], streams[1])
            )
            await self.session.initialize()
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def __aexit__(self, *args):
        return await self.stack.__aexit__(*args)

    async def call(self, name, arguments):
        result = await self.session.call_tool(name, arguments)
        data = json.loads(result.content[0].text)
        if result.is_error:
            outcome = {"refused": Refused, "failed": Failed}.get(data.get("status"))
            if outcome is None:
                raise RuntimeError("unrecognized transfer control outcome")
            raise outcome(data["code"], data["message"])
        return data

    async def deposit(self, path):
        size, sha = await asyncio.to_thread(digest_file, path)
        receipt = await self.call(
            "create_transfer",
            {"direction": "download", "name": path.name, "size": size, "sha": sha},
        )

        async def chunks():
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(CHUNK)
                    if not chunk:
                        break
                    yield chunk

        response = await self.http.put(
            self.checked_url(receipt["upload_url"]),
            content=chunks(),
            headers={
                "Content-Length": str(size),
                "X-Fishmarket-Token": receipt["upload_token"],
                "X-Fishmarket-Filename": quote(path.name, safe=""),
            },
        )
        response.raise_for_status()
        return await self.call("open_download", {"transfer_id": receipt["id"]})

    async def collect(self, transfer_id, destination):
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("destination already exists")
        receipt = await self.call("collection_receipt", {"transfer_id": transfer_id})
        fd, tmp = tempfile.mkstemp(prefix=".fishmarket-", dir=destination.parent)
        temporary = Path(tmp)
        digest = hashlib.sha256()
        size = 0
        published = False
        try:
            with os.fdopen(fd, "wb") as output:
                async with self.http.stream(
                    "GET", self.checked_url(receipt["download_url"])
                ) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes(CHUNK):
                        size += len(chunk)
                        if size > receipt["size"]:
                            raise RuntimeError("download exceeded expected size")
                        output.write(chunk)
                        digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size != receipt["size"] or digest.hexdigest() != receipt["sha"]:
                raise RuntimeError(
                    "download checksum/size mismatch; remote file retained"
                )
            # Hard-link publication is atomic and never overwrites, on Windows and Linux.
            os.link(temporary, destination)
            published = True
            # Before the acknowledgement authorizes disposal of the remote copy.
            flushed = durable_publication(destination)
            try:
                temporary.unlink()
                await self.call(
                    "acknowledge",
                    {
                        "transfer_id": transfer_id,
                        "size": size,
                        "sha": digest.hexdigest(),
                    },
                )
            except Exception:
                raise AcknowledgementUnknown(
                    "local file published; acknowledgement outcome unknown; use ack with this ID/file"
                ) from None
            return {"collected": True, "size": size, "sha": digest.hexdigest(),
                    "directory_flushed": flushed}
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                if published:
                    raise AcknowledgementUnknown(
                        "local file published; temporary cleanup failed; inspect destination and retry ack"
                    ) from None
                raise

    async def ack(self, transfer_id, path):
        size, sha = await asyncio.to_thread(digest_file, path)
        flushed = durable_publication(path)
        result = await self.call(
            "acknowledge", {"transfer_id": transfer_id, "size": size, "sha": sha}
        )
        return {**result, "directory_flushed": flushed}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True)
    p.add_argument(
        "--token-file",
        type=Path,
        required=True,
        help="private service bearer/access-key file",
    )
    sub = p.add_subparsers(dest="command", required=True)
    deposit = sub.add_parser("deposit")
    deposit.add_argument("file", type=Path)
    for name in ("collect", "ack"):
        command = sub.add_parser(name)
        command.add_argument("id")
        command.add_argument("file", type=Path)
    upload = sub.add_parser("upload-link")
    upload.add_argument("--max-bytes", type=int)
    sub.add_parser("ready")
    sub.add_parser("list")
    args = p.parse_args()

    async def run():
        async with TransferClient(
            args.url, args.token_file.read_text(encoding="utf-8").strip()
        ) as client:
            if args.command == "deposit":
                result = await client.deposit(args.file)
            elif args.command == "collect":
                result = await client.collect(args.id, args.file)
            elif args.command == "ack":
                result = await client.ack(args.id, args.file)
            elif args.command == "ready":
                result = await client.call("list_ready", {})
            elif args.command == "list":
                result = await client.call("list_transfers", {})
            else:
                parameters = {"direction": "upload"}
                if args.max_bytes is not None:
                    parameters["max_bytes"] = args.max_bytes
                result = await client.call("create_transfer", parameters)
            print(json.dumps(result, ensure_ascii=False))

    try:
        asyncio.run(run())
    except Exception as exc:
        # Avoid raw HTTP exceptions containing capabilities, filenames or source paths.
        print(
            json.dumps(
                {
                    "failed": True,
                    "type": type(exc).__name__,
                    "message": (
                        "Local file verified and published; retry ack with the same ID and file."
                        if isinstance(exc, AcknowledgementUnknown)
                        else "Local file published but its directory could not be flushed; not "
                        "acknowledged, the remote copy stays; retry ack with the same ID and file."
                        if isinstance(exc, AcknowledgementWithheld)
                        else "Transfer did not complete; inspect status/local destination before retrying."
                    ),
                }
            )
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
