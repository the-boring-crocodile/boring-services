#!/usr/bin/env python3
"""Temporary trusted-LAN browser relay; MCP and collection endpoints stay private."""

import argparse
import http.client
import http.server
import ipaddress
import re
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream-port", type=int, required=True)
    args = parser.parse_args()
    address = ipaddress.IPv4Address(args.address)
    if not address.is_private or address.is_loopback or address.is_unspecified:
        parser.error("bind an explicit private LAN IPv4 address")
    external = f"http://{address}:{args.port}"
    internal = f"http://127.0.0.1:{args.upstream_port}"

    class Browser(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def proxy(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            allowed = False
            if self.command in {"GET", "HEAD"}:
                allowed = bool(
                    re.fullmatch(r"/fishmarket/(upload|download)/[a-f0-9]{32}", path)
                    or re.fullmatch(
                        r"/fishmarket/web/(market.js|market.css|icon.svg)", path
                    )
                    or re.fullmatch(r"/fishmarket/api/[a-f0-9]{32}/download", path)
                )
            if self.command == "POST":
                allowed = bool(
                    re.fullmatch(r"/fishmarket/api/[a-f0-9]{32}/claim", path)
                )
            if self.command == "PUT":
                allowed = bool(
                    re.fullmatch(r"/fishmarket/api/[a-f0-9]{32}/upload", path)
                )
            if (
                not allowed
                or parsed.query
                or parsed.netloc
                or self.headers.get("Transfer-Encoding")
            ):
                self.send_error(403, "Browser endpoint only")
                return
            raw = self.headers.get("Content-Length", "0")
            if not re.fullmatch(r"[0-9]{1,12}", raw):
                self.send_error(400)
                return
            size = int(raw)
            if size > (104857600 if self.command == "PUT" else 4096):
                self.send_error(413)
                return
            self.connection.settimeout(600)
            upstream = http.client.HTTPConnection(
                "127.0.0.1", args.upstream_port, timeout=600
            )
            try:
                headers = {
                    k.lower(): v
                    for k, v in self.headers.items()
                    if k.lower()
                    in {
                        "content-length",
                        "content-type",
                        "cookie",
                        "range",
                        "x-fishmarket-token",
                        "x-fishmarket-filename",
                        "origin",
                    }
                }
                if headers.get("origin") == external:
                    headers["origin"] = internal
                # Exact-origin translation is only for this isolated HTTP fixture.
                upstream.putrequest(self.command, path)
                for k, v in headers.items():
                    upstream.putheader(k, v)
                upstream.endheaders()
                remaining = size
                while remaining:
                    data = self.rfile.read(min(256 * 1024, remaining))
                    if not data:
                        raise ConnectionError()
                    upstream.send(data)
                    remaining -= len(data)
                response = upstream.getresponse()
                self.send_response(response.status)
                for k, v in response.getheaders():
                    if k.lower() not in {
                        "connection",
                        "transfer-encoding",
                        "server",
                        "date",
                    }:
                        self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    while True:
                        data = response.read(256 * 1024)
                        if not data:
                            break
                        self.wfile.write(data)
            except (OSError, http.client.HTTPException):
                self.close_connection = True
            finally:
                upstream.close()

        do_GET = do_HEAD = do_POST = do_PUT = proxy

    server = http.server.ThreadingHTTPServer((str(address), args.port), Browser)
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
