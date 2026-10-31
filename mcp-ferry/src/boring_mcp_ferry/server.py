"""boring-mcp-ferry as a process.

    boring-mcp-ferry http --config <config.json> [--state-dir DIR] [--no-call-log]

Starts one front end per backend that has a `service`, and one per gateway (each
on its own port, as its ServiceConfig says) in one process, with each one's
registration watchdog. A backend that
is down does not stop the ferry: its service shows as unavailable. A bad
configuration stops it before anything listens (exit status 2, the reason on
stderr). SIGTERM stops every front end.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boring_service_kit.calllog import CallLog
from boring_service_kit.http import ServiceConfig
from boring_service_kit.pond import run
from boring_service_kit.tools import default_state_dir

from .config import Backend, ConfigError, read
from .ferry import ArrivalGate, Carried, HealthGate, Upstream, front, gateway_front


@dataclass
class Front:
    """One front end (a backend's own, or a gateway): its name, how it is put on the network, its ASGI app."""
    name: str
    config: ServiceConfig
    server: Any
    app: Any


def _every_address(bind: str) -> bool:
    """Whether `bind` listens on every address, however it is spelled."""
    if not bind.strip():
        return True
    try:
        return ipaddress.ip_address(bind.strip("[]")).is_unspecified
    except ValueError:
        pass
    try:  # the short IPv4 spellings the resolver also takes ("0", "0.0")
        return socket.inet_aton(bind) == bytes(4)
    except OSError:
        return False  # a host name: the kit resolves it to its addresses


def _service(path: Any, where: str, needs_oauth: bool) -> ServiceConfig:
    try:
        config = ServiceConfig(path)
    except (OSError, ValueError) as error:
        raise ConfigError(f"{where}: {error}") from None
    if needs_oauth and config.verifier is None:
        raise ConfigError(f"{where}: the ferry needs `oauth` (the Crocodile by introspection): a third-party "
                          "server reaches the Pond only with its tokens")
    if _every_address(config.bind):
        raise ConfigError(f"{where}: bind the ferry's own address on its default network (172.16.46.2 in the "
                          "template), or loopback, not every address: the backends share the ferry's other "
                          "networks and must not reach its front ends")
    return config


def prepare(path: Path, state_dir: Path | None, log_calls: bool) -> tuple[list[Front], list[Upstream]]:
    """Every front end and the upstreams they share, checked before anything listens."""
    ferry = read(path)
    upstreams = {backend.name: Upstream(backend) for backend in ferry.backends}
    prepared: list[Front] = []
    ports: dict[int, str] = {}
    urls: dict[str, str] = {}
    clients: dict[str, str] = {}

    def unique(name: str, config: ServiceConfig) -> None:
        authority = getattr(config, "authority", None)
        client = authority.client_id if authority is not None else None
        for seen, what, key in ((ports, "port", config.port), (urls, "URL", config.url), (clients, "client_id", client)):
            if key is None:
                continue
            if key in seen:
                raise ConfigError(f"{seen[key]} and {name} have the same {what}: each front end is its own service "
                                  "(its own service-add, URL and port)")
            seen[key] = name

    def calls(name: str) -> CallLog:
        return CallLog((state_dir or default_state_dir("boring-mcp-ferry")) / f"{name}-calls.jsonl"
                       if log_calls else None)

    by_name = {backend.name: backend for backend in ferry.backends}
    for spec in ferry.frontends:
        backend_front = isinstance(spec, Backend)
        group = "backends" if backend_front else "gateways"
        assert spec.service is not None
        config = _service(spec.service, f"{group}.{spec.name}.service", needs_oauth=backend_front)
        unique(spec.name, config)
        if backend_front:
            server = front(spec, upstreams[spec.name], config, calls(spec.name))
            app = HealthGate(config.app(server), upstreams[spec.name])
        else:
            # A gateway may take the box's access keys or the Crocodile's tokens.
            carried = {name: Carried(by_name[name], upstreams[name], config.call_limit) for name in spec.backends}
            server = gateway_front(spec, carried, config, calls(spec.name))
            app = ArrivalGate(config.app(server))
        prepared.append(Front(spec.name, config, server, app))
    return prepared, list(upstreams.values())


async def serve(fronts: list[Front], upstreams: list[Upstream]) -> None:
    """Every front end until the process is told to stop. Each uvicorn restores the
    previous SIGTERM handler when it stops and raises the signal again, so one SIGTERM
    stops them all in turn (tested)."""
    try:
        await asyncio.gather(*(run(f.app, f.config.watchdog, f.config.bind, f.config.port) for f in fronts))
    finally:
        for upstream in upstreams:
            await upstream.close()


def quiet_http_logs() -> None:
    """The HTTP clients log every request's URL at INFO, and the SDK's client a redirect's target
    at WARNING; a backend's URL can hold its secret path. The ferry's own messages redact it."""
    for noisy in ("httpx2", "httpcore2", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("mcp.client").setLevel(logging.ERROR)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="boring-mcp-ferry", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    http = commands.add_parser("http", help="every backend's front end over HTTP, configured by --config")
    http.add_argument("--config", type=Path, required=True, help="the ferry's configuration (config.json)")
    http.add_argument("--state-dir", type=Path)
    http.add_argument("--no-call-log", action="store_true")
    args = parser.parse_args(argv)
    try:
        prepared, upstreams = prepare(args.config, args.state_dir, not args.no_call_log)
    except ConfigError as error:
        print(json.dumps({"status": "refused", "code": "invalid_config", "message": str(error)}), file=sys.stderr)
        raise SystemExit(2) from None
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    quiet_http_logs()
    for f in prepared:
        logging.getLogger("boring_mcp_ferry").info("carrying %s at %s (port %d)", f.name, f.config.url,
                                                   f.config.port)
    asyncio.run(serve(prepared, upstreams))


if __name__ == "__main__":  # pragma: no cover
    main()
