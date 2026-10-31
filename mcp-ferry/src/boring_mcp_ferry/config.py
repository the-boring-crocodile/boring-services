"""The ferry's configuration: which backends it carries, and how it reaches each.

    {"backends": {
        "homeassistant": {
            "service": "services/homeassistant.json",
            "upstream": {"url_file": "../credentials/homeassistant-url",
                         "headers_file": "../credentials/homeassistant-headers.json",
                         "timeout_seconds": 20},
            "tools": {"deny": ["ha_restart"]}
        },
        "d365": {"upstream": {"url": "http://127.0.0.1:18514/mcp", "headers_file": "d365.headers.json"},
                 "guide_file": "../kit/d365.md"}
    },
     "gateways": {"pondnt-services": {"service": "services/gateway.json", "backends": ["d365"]}}}

- `service`: the backend's front end towards the Pond, a boring-service-kit
  ServiceConfig file (its URL and port, the Crocodile as issuer by
  introspection, `access`, `pond` registration). One per backend: its own
  service credentials, URL and token audience.
- `upstream`: where the ferry reaches the backend, an internal URL that only
  the ferry can reach. `url` inline, or `url_file` when the URL itself is a
  secret (a server whose only protection is a secret path). `headers_file`: a
  JSON object of HTTP headers for a server with a static secret.
  `timeout_seconds`: at most the Pond's call time (the default).
- `tools`: `allow` or `deny`, a list of tool names; a tool left out is neither
  listed nor callable.
- `service` is optional when a gateway carries the backend: a backend has its
  own front end (`service`), sits behind one or more gateways, or both.
- `guide_file`: optional text a gateway's `service_describe` returns with the
  backend's tools (e.g. a kit page), read when the ferry starts.

`gateways` (optional): one front end each (`service`, a ServiceConfig file),
offering four tools over the backends it names (`backends`): service_list,
service_describe, service_read and service_call.

Relative files resolve next to the configuration file. Unknown keys are
refused, so a typo cannot silently drop a restriction.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")  # the Pond's service names
KEYS = {"backends", "gateways"}
BACKEND_KEYS = {"service", "upstream", "tools", "guide_file"}
GATEWAY_KEYS = {"service", "backends"}
GUIDE_CHARS = 64 * 1024
UPSTREAM_KEYS = {"url", "url_file", "headers_file", "timeout_seconds"}
HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}\Z")
FORBIDDEN_HEADERS = {"host", "content-length", "content-type", "accept", "mcp-protocol-version",
                     "mcp-session-id", "mcp-method", "transfer-encoding", "connection"}


class ConfigError(ValueError):
    """The configuration is not usable; the ferry does not start."""


@dataclass(frozen=True)
class Backend:
    name: str
    service: Path | None        # its own front end's ServiceConfig file; None: only behind gateways
    url: str                    # internal, never shown to callers
    headers: dict[str, str]
    timeout_seconds: float | None
    allow: frozenset[str] | None
    deny: frozenset[str]
    guide: str | None = None    # the guide_file's text, for a gateway's service_describe

    def offers(self, tool: str) -> bool:
        """Whether this ferry passes a tool of this backend on."""
        if tool in self.deny:
            return False
        return self.allow is None or tool in self.allow


def _file(config: Path, value: Any, what: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{what} must be a path")
    path = Path(value)
    return path if path.is_absolute() else config.resolve().parent / path


def _url(value: str, where: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment):
        raise ConfigError(f"{where}: an absolute http(s) URL without credentials, query or fragment")
    return value


def _upstream(config: Path, name: str, spec: Any) -> tuple[str, dict[str, str], float | None]:
    where = f"backends.{name}.upstream"
    if not isinstance(spec, dict) or set(spec) - UPSTREAM_KEYS or ("url" in spec) == ("url_file" in spec):
        raise ConfigError(f"{where}: exactly one of url or url_file, optional headers_file and timeout_seconds")
    if "url" in spec:
        if not isinstance(spec["url"], str):
            raise ConfigError(f"{where}.url must be a URL")
        url = spec["url"]
    else:
        try:
            url = _file(config, spec["url_file"], f"{where}.url_file").read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ConfigError(f"{where}.url_file cannot be read ({type(error).__name__})") from None
    url = _url(url, where)
    headers: dict[str, str] = {}
    if "headers_file" in spec:
        try:
            data = json.loads(_file(config, spec["headers_file"], f"{where}.headers_file").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ConfigError(f"{where}.headers_file is not readable JSON ({type(error).__name__})") from None
        if (not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items())
                or not all(HEADER_NAME.fullmatch(k) and k.lower() not in FORBIDDEN_HEADERS for k in data)
                or any("\r" in v or "\n" in v for v in data.values())):
            raise ConfigError(f"{where}.headers_file: a JSON object of header names to text values "
                              "(no transport headers such as Host or Mcp-Session-Id)")
        headers = dict(data)
    timeout = spec.get("timeout_seconds")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or not math.isfinite(timeout) or timeout <= 0):
        raise ConfigError(f"{where}.timeout_seconds must be a positive number of seconds")
    return url, headers, None if timeout is None else float(timeout)


def _tools(name: str, spec: Any) -> tuple[frozenset[str] | None, frozenset[str]]:
    where = f"backends.{name}.tools"
    if spec is None:
        return None, frozenset()
    if (not isinstance(spec, dict) or len(spec) != 1 or set(spec) - {"allow", "deny"}
            or not isinstance(next(iter(spec.values())), list)
            or not all(isinstance(t, str) and t for t in next(iter(spec.values())))):
        raise ConfigError(f"{where}: exactly one of allow or deny, a list of tool names")
    names = frozenset(next(iter(spec.values())))
    return (names, frozenset()) if "allow" in spec else (None, names)


@dataclass(frozen=True)
class Gateway:
    name: str
    service: Path               # its ServiceConfig file
    backends: tuple[str, ...]   # the backends it offers, in this order


@dataclass(frozen=True)
class Ferry:
    backends: list[Backend]
    gateways: list[Gateway]

    @property
    def frontends(self) -> list[Backend | Gateway]:
        """Configured listeners in runtime order; gateway-only backends have no listener."""
        return [b for b in self.backends if b.service is not None] + list(self.gateways)


def _guide(config: Path, name: str, value: Any) -> str:
    where = f"backends.{name}.guide_file"
    try:
        text = _file(config, value, where).read_text(encoding="utf-8-sig")
    except OSError as error:
        raise ConfigError(f"{where} cannot be read ({type(error).__name__})") from None
    if len(text) > GUIDE_CHARS:
        raise ConfigError(f"{where}: at most {GUIDE_CHARS} characters")
    return text


def read(path: Path) -> Ferry:
    """The backends and gateways of a ferry configuration, checked; ConfigError when anything is off."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConfigError(f"{path} is not readable JSON ({type(error).__name__})") from None
    if (not isinstance(data, dict) or "backends" not in data or set(data) - KEYS
            or not isinstance(data["backends"], dict) or not data["backends"]):
        raise ConfigError("the configuration is {\"backends\": {NAME: {...}, ...}, \"gateways\": {...}} with at "
                          "least one backend; gateways optional")
    backends = []
    for name, spec in data["backends"].items():
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ConfigError(f"backend name {name!r}: lowercase letters, digits and hyphens, as Pond services")
        if not isinstance(spec, dict) or "upstream" not in spec or set(spec) - BACKEND_KEYS:
            raise ConfigError(f"backends.{name}: upstream, optional service, tools and guide_file")
        url, headers, timeout = _upstream(path, name, spec["upstream"])
        allow, deny = _tools(name, spec.get("tools"))
        service = _file(path, spec["service"], f"backends.{name}.service") if "service" in spec else None
        guide = _guide(path, name, spec["guide_file"]) if "guide_file" in spec else None
        backends.append(Backend(name, service, url, headers, timeout, allow, deny, guide))
    gateways = []
    spec_all = data.get("gateways", {})
    if not isinstance(spec_all, dict):
        raise ConfigError("gateways is {NAME: {\"service\": ..., \"backends\": [...]}}")
    known = {b.name for b in backends}
    for name, spec in spec_all.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in known:
            raise ConfigError(f"gateway name {name!r}: lowercase letters, digits and hyphens, not a backend's name")
        if (not isinstance(spec, dict) or set(spec) != GATEWAY_KEYS or not isinstance(spec["backends"], list)
                or not spec["backends"] or not all(isinstance(b, str) for b in spec["backends"])
                or len(set(spec["backends"])) != len(spec["backends"])):
            raise ConfigError(f"gateways.{name}: service and backends (a list of backend names, each once)")
        unknown = [b for b in spec["backends"] if b not in known]
        if unknown:
            raise ConfigError(f"gateways.{name}: no backend {unknown[0]!r}")
        gateways.append(Gateway(name, _file(path, spec["service"], f"gateways.{name}.service"),
                                tuple(spec["backends"])))
    served = {b for g in gateways for b in g.backends}
    for backend in backends:
        if backend.service is None and backend.name not in served:
            raise ConfigError(f"backends.{backend.name}: neither a service of its own nor a gateway carries it")
    return Ferry(backends, gateways)
