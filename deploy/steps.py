"""Deploy steps for a service (runtime docker; see boring_deploy).

The HOME holds the operator's compose.yaml (one service, image ${BORING_IMAGE})
and everything the service reads; the deploy changes only boring-image.env. RELEASE.json
names the image, the package to import, and for a service with a schema the
data file and how to read its generation.

An update leaves a stopped stack stopped: the engine's Docker runtime decides
(no `compose up`, the new image still in boring-image.env) and then skips the smoke.
"""
import datetime
import inspect
import ipaddress
import json
import re
import sqlite3
import tempfile
from contextlib import closing
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import boring_deploy

SMOKE_SECONDS = 60  # how long the smoke waits for the endpoint and the registration
REACH_HINT = ("boring-services README, Installing on Linux: 'The container itself calls the "
              "Crocodile's public URL' (name resolvable in Docker, firewall path to 443, trusted CA)")


def _compose(ctx, *args, image=None, check=True, files=()):
    env = {"BORING_IMAGE": image} if image else None
    extra = [arg for path in files for arg in ("-f", str(path))]
    return ctx.run(*boring_deploy.compose_command(ctx.home, *extra, *args), env=env, check=check)


def _without_fixed_addresses(name, service):
    """A compose override for a one-off container of `name`: on the same networks, but none
    of the service's fixed addresses (the running container holds them: the ferry's
    ipv4_address). None when it has none. Needs Docker Compose 2.24 or later (`!override`)."""
    networks = service.get("networks") or {}
    if not any(isinstance(c, dict) and (c.get("ipv4_address") or c.get("ipv6_address"))
               for c in networks.values()):
        return None
    lines = ["services:", f"  {json.dumps(name)}:", "    networks: !override"]
    lines += [f"      {json.dumps(network)}: {{}}" for network in networks]
    return "\n".join(lines) + "\n"


MARKER_IMAGE = "boring-deploy-marker:render"  # renders ${BORING_IMAGE} so the deploy's service is found


def managed_service(config):
    ours = [(name, service) for name, service in config["services"].items()
            if service.get("image") == MARKER_IMAGE]
    if len(ours) != 1:
        raise boring_deploy.Failure(f"compose.yaml must have exactly one service with image ${{BORING_IMAGE}}, "
                                    f"found {len(ours)}", "the deploy builds and replaces only that one")
    return ours[0]

def exposure(service):
    """Normalized Compose publications and the explicit proxy-network designation."""
    mode = service.get("network_mode") or ""
    if mode == "host" or mode.startswith(("service:", "container:")):
        raise boring_deploy.Failure("network_mode shares a namespace; use loopback ports or a named proxy network")
    shown = []
    for port in service.get("ports") or []:
        if not isinstance(port, dict):
            raise boring_deploy.Failure("Compose ports must be normalized objects")
        host = port.get("host_ip") or "0.0.0.0"
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise boring_deploy.Failure("published port is not loopback; bind host ports to 127.0.0.1/::1, or remove ports and name an attached proxy network")
        shown.append(f"{host}:{port.get('published') or 'random'}->{port.get('target')}/{port.get('protocol', 'tcp')}")
    if shown:
        return "loopback " + ", ".join(shown)
    proxy = (service.get("labels") or {}).get("boring.proxy-network")
    if not isinstance(proxy, str) or not proxy or proxy not in (service.get("networks") or {}):
        raise boring_deploy.Failure("no loopback publication: name an attached proxy network with Compose label boring.proxy-network")
    return "proxy network " + proxy + " (no published ports)"


def _service(ctx, image=None):
    """The service the deploy builds: the one service whose image is ${BORING_IMAGE}. A stack
    may hold others beside it (the ferry's backends, each an image of its own). Found by
    rendering ${BORING_IMAGE} as a marker no real image has, then returned as rendered with
    `image` (the new one, for the check) when given."""
    config = json.loads(_compose(ctx, "config", "--format", "json", image=MARKER_IMAGE).stdout)
    name, service = managed_service(config)
    if image is not None:
        service = json.loads(_compose(ctx, "config", "--format", "json", image=image).stdout)["services"][name]
    return name, service


def preflight(ctx):
    """Read the operator's listeners before install, image preparation or switching."""
    if (ctx.home / "compose.yaml").is_file():
        try:
            name, service = _service(ctx)
            ctx.log(name + " exposure: " + exposure(service))
            # An interrupted first install leaves compose.yaml before a release
            # is installed. Let check inspect that config with the prepared image.
            if ctx.installed is None:
                return []
            inside = _config_inside(ctx, name, service)
            if inside:
                # Read through the installed stack's mounts and identity. No image
                # preparation or listener: only the stdlib legacy metadata parser.
                code = inspect.getsource(legacy_access) + inspect.getsource(frontend_metadata) + """
import json, sys
print(json.dumps(frontend_metadata(sys.argv[1], legacy=True)))
"""
                result = _in_candidate(ctx, name, code, inside,
                                       override=_without_fixed_addresses(name, service))
                legacy = json.loads(result.stdout)
                if legacy:
                    return [_legacy_access_refusal(legacy)]
        except boring_deploy.Failure as error:
            return [str(error)]
    return []


def check(ctx):
    """The new image imports and parses the configured service before any switch."""
    name, service = _service(ctx, image=ctx.image)
    exposure(service)
    module = ctx.release.details["module"]
    ctx.run("docker", "run", "--rm", "--entrypoint", "python", ctx.image, "-I", "-c", f"import {module}")
    ctx.log(f"{name}: compose renders with {ctx.image}; {module} imports")
    inside = _config_inside(ctx, name, service)
    if inside:
        override = _without_fixed_addresses(name, service)
        _check_configuration(ctx, name, inside, module, override)
        _check_crocodile_reach(ctx, name, inside, override)


def validate_configuration(path, module):
    """Parse through the candidate's owners without opening issuer state or starting servers."""
    from pathlib import Path
    from boring_service_kit.http import ServiceConfig
    legacy = frontend_metadata(path, legacy=True)
    if legacy:
        return legacy
    if module == "boring_mcp_ferry":
        from boring_mcp_ferry.server import validate
        validate(Path(path))
    else:
        ServiceConfig.validate(Path(path))


def _in_candidate(ctx, name, code, *arguments, override=None, read_sealed=False):
    """Use the stack's identity and candidate image, or installed image before prepare.

    The uninstall uses a sealed-file metadata read instead of the stack identity.

    That read uses root and DAC read/search to traverse sealed0700 folders and
    read account-owned0400 secrets. It runs only the read-only metadata parser;
    it neither starts the service nor publishes ports. Ordinary checks keep
    the service's own identity/capabilities.
    """
    reader = ("--user", "0:0", "--cap-add", "DAC_READ_SEARCH") if read_sealed else ()
    run = ("run", "--rm", "--no-deps", "-T", *reader, "--entrypoint", "python", name,
           "-I", "-B", "-c", code, *arguments)
    if override is None:
        return _compose(ctx, *run, image=ctx.image)
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "one-off.yaml"
        path.write_text(override)
        return _compose(ctx, *run, image=ctx.image, files=[path])


def _check_configuration(ctx, name, inside, module, override=None):
    code = (inspect.getsource(legacy_access) + inspect.getsource(frontend_metadata)
            + inspect.getsource(validate_configuration)) + """
import json, sys
legacy = validate_configuration(sys.argv[1], sys.argv[2])
if legacy:
    print(json.dumps(legacy))
"""
    result = _in_candidate(ctx, name, code, inside, module, override=override)
    if result.stdout.strip():
        raise boring_deploy.Failure(_legacy_access_refusal(json.loads(result.stdout)))
    ctx.log(f"{name}: configuration validates with the new image")


def crocodile_reach(url, get):
    """None when `get(url)` (an HTTPS GET returning the status) gets an HTTP answer, else
    which of name, connection or TLS failed. Any answer, even 401 or 404, proves the name,
    the network path and the certificate; a proxy's 502/503/504 says the Crocodile behind
    it is not reached. Its source runs inside the new image (check), so it uses only the
    standard library and what `get` brings."""
    import socket
    import ssl
    try:
        status = get(url)
    except Exception as error:
        chain, seen = error, []
        while chain is not None and len(seen) < 16:
            seen.append(chain)
            chain = chain.__cause__ or chain.__context__
        kind = ("name" if any(isinstance(e, socket.gaierror) for e in seen) else
                "TLS" if any(isinstance(e, ssl.SSLError) for e in seen) else "connection")
        return f"{kind}: {type(error).__name__}: {error}"
    if status in (502, 503, 504):
        return f"connection: the proxy answers {status}; the Crocodile behind it is not reached"
    return None


def legacy_access(path):
    """Advisory read of the retired format, never accepted as runtime config."""
    import json
    from pathlib import Path
    from urllib.parse import urlsplit
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "access" not in data:
        return None
    oauth = data.get("oauth") if isinstance(data.get("oauth"), dict) else {}
    own = data.get("issuer") if isinstance(data.get("issuer"), dict) else {}
    resource = urlsplit(data.get("url", ""))
    issuer = oauth.get("issuer") or own.get("url") or f"{resource.scheme}://{resource.netloc}"
    # Published AccessPolicy used the configured spelling, stripping trailing /.
    try:
        parsed = urlsplit(issuer)
        valid = (isinstance(issuer, str) and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.fragment and not parsed.query
                 and (parsed.scheme == "https" or parsed.scheme == "http"
                      and parsed.hostname in ("127.0.0.1", "localhost", "::1")))
    except (ValueError, TypeError):
        valid = False
    issuer = issuer.rstrip("/") if valid else None
    info = {"path": str(path), "authority_url": issuer, "client_id": oauth.get("client_id"),
            "callers": {}, "aliases": [], "problem": None}
    grants = data["access"]
    if not isinstance(grants, list) or issuer is None:
        info["problem"] = "access needs a list of grants and a valid configured issuer"
        return info
    authors = set()
    for grant in grants:
        if not isinstance(grant, dict) or set(grant) - {"subject", "role", "author"}:
            info["problem"] = "legacy access grants support only subject, role and author"
            break
        subject, role = grant.get("subject"), grant.get("role")
        if (not isinstance(subject, str) or not subject or role not in ("reader", "writer")
                or subject in info["callers"]):
            info["problem"] = "legacy access needs distinct subjects and reader/writer roles"
            break
        default = json.dumps([issuer, subject], separators=(",", ":"))
        author = grant.get("author", default)
        if not isinstance(author, str) or not author or author in authors:
            info["problem"] = "legacy access author identifiers must be nonempty and unique"
            break
        authors.add(author)
        info["callers"][subject] = role
        if author != default:
            info["aliases"].append({"subject": subject, "author": author})
    return info


def _legacy_access_refusal(frontends):
    """Join legacy client identities to the Crocodile's service-name owner."""
    import shlex
    from install_steps import CROCODILE, _admin_json
    authority = _admin_json(["list", "--json"]).get("public_base_url")
    records = _admin_json(["service-list"], expected_type=list)
    lines = ["Legacy access is replaced by Crocodile caller roles. Resolve each difference, then review each preview, "
             "run its apply command and type APPLY. Then use one mode=crocodile authority "
             "block and remove access. Check that an intended reader can read and an intended "
             "writer can perform a harmless write. Nothing was applied by this check."]
    for info in frontends.values():
        lines.append(info["path"] + ":")
        for alias in info["aliases"]:
            lines.append(f"subject {alias['subject']!r}, custom author {alias['author']!r}: "
                         "explicit decision required; existing data would otherwise be stranded")
        if info["problem"]:
            lines.append(info["problem"] + "; no complete grant command")
            continue
        if not isinstance(authority, str) or info["authority_url"] != authority.rstrip("/"):
            lines.append("issuer differs from the local Crocodile; review roles at its owner")
            continue
        client = info["client_id"]
        matches = [row for row in records if isinstance(row, dict)
                   and isinstance(client, str) and client and row.get("client_id") == client]
        if len(matches) != 1 or not isinstance(matches[0].get("name"), str):
            lines.append(f"client_id {info['client_id']!r} is "
                         + ("unmapped" if not matches else "ambiguous") + "; no grant command")
            continue
        membership = matches[0].get("callers")
        if not isinstance(membership, dict) or any(not isinstance(name, str) for name in membership):
            lines.append("Crocodile caller membership is unavailable; no complete grant command")
            continue
        local = info["callers"]
        shared = sorted(local.keys() & membership.keys())
        local_only = sorted(local.keys() - membership.keys())
        crocodile_only = sorted(membership.keys() - local.keys())
        if local_only or crocodile_only:
            for name in shared:
                lines.append(f"{name}: suggested role {local[name]} (present in both)")
            for source, names in (("local only", local_only), ("Crocodile only", crocodile_only)):
                for name in names:
                    lines.append(f"{name}: {source}; explicit decision required")
            lines.append("Resolve every caller difference before writing a complete grant command")
            continue
        callers = ",".join(f"{name}={local[name]}" for name in shared)
        command = [CROCODILE, "service-grants", matches[0]["name"], "--callers", callers]
        lines.append("Preview: " + shlex.join(command))
        lines.append("Apply:   " + shlex.join([*command, "--apply"]))
    return "\n".join(lines)


def frontend_metadata(path, *, legacy=False):
    """Configuration metadata belongs to ServiceConfig; no second auth parser here."""
    from dataclasses import asdict
    import json
    from pathlib import Path
    source = Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "backends" in data:
        from boring_mcp_ferry.config import read
        paths = {frontend.name: frontend.service for frontend in read(source).frontends}
    else:
        paths = {"": source}
    if legacy:
        return {name: info for name, source in paths.items()
                if (info := legacy_access(source)) is not None}
    from boring_service_kit.http import ServiceConfig
    return {name: asdict(ServiceConfig.inspect(source)) for name, source in paths.items()}


def _check_crocodile_reach(ctx, name, inside, override=None):
    """Before the switch: from a one-off container of the new image on the stack's own
    network, user and config (`compose run`, removed again), GET the Crocodile URL the
    config registers or introspects with, as the service's own client does (httpx2,
    system trust store, no proxy environment). A config without one is not checked."""
    code = inspect.getsource(crocodile_reach) + inspect.getsource(frontend_metadata) + """
import json, sys
def get(u):
    import httpx2
    return httpx2.get(u, timeout=5, trust_env=False).status_code
answers = [{'frontend': frontend, 'url': url, 'problem': crocodile_reach(url, get)}
           for frontend, info in frontend_metadata(sys.argv[1]).items()
           if (url := info['authority_url']) is not None]
print(json.dumps({'frontends': answers}))
"""
    result = _in_candidate(ctx, name, code, inside, override=override)
    answer = json.loads(result.stdout.strip().splitlines()[-1])
    for front in answer.get("frontends", [answer]):
        if not front["url"]:
            continue
        label = name + (f" ({front['frontend']})" if front.get("frontend") else "")
        if front["problem"]:
            raise boring_deploy.Failure(f"{label}: the container cannot reach the Crocodile at {front['url']} "
                                        f"({front['problem']}); nothing switched", REACH_HINT)
        ctx.log(f"{label}: the new image reaches {front['url']} from the stack's network")


def _data(ctx):
    data = ctx.release.details.get("data")
    if not data or not (ctx.home / data["file"]).is_file():
        return None, None
    return ctx.home / data["file"], data


def data_generation(ctx):
    path, data = _data(ctx)
    if path is None:
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute(data["query"]).fetchone()
    return int(row[0]) if row else None


def backup(ctx, directory):
    path, _ = _data(ctx)
    target = Path(directory) / path.name
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(target)) as copy:
        source.backup(copy)
    return target


def _config_inside(ctx, name, service):
    command = service.get("command") or []
    for index, arg in enumerate(command):
        if arg.startswith("--config="):
            return arg.split("=", 1)[1]
        if arg == "--config" and index + 1 < len(command):
            return command[index + 1]
    return None


def metadata_url(url):
    """RFC 9728 protected resource metadata URL of a resource, as the MCP SDK (and so
    boring-service-kit) builds it: /.well-known/oauth-protected-resource + the resource path."""
    parts = urlsplit(url)
    path = parts.path if parts.path != "/" else ""
    return f"{parts.scheme}://{parts.netloc}/.well-known/oauth-protected-resource{path}"


def _get(url):
    """(status, headers, body) of an unauthenticated GET; status is a text for no answer."""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.headers, response.read(65536)
    except urllib.error.HTTPError as error:
        return error.code, error.headers, b""
    except OSError as error:
        return str(error), {}, b""


def endpoint_identity(url):
    """None when `url` is this service's MCP endpoint, else what was found instead.

    A kit service refuses an unauthenticated request with 401 and a WWW-Authenticate
    header naming its resource metadata (RFC 9728), built from its configured URL;
    that metadata names the URL as its `resource`. A missing proxy route (404), an
    unrelated page (200 HTML) or another service's challenge is not this service."""
    status, headers, _ = _get(url)
    if status != 401:
        return f"{url} answers {status}, not 401 with an MCP authentication challenge"
    challenge = (headers.get("WWW-Authenticate") or "") if headers else ""
    named = re.search(r'resource_metadata="([^"]*)"', challenge)
    expected = metadata_url(url)
    if not named or named.group(1).rstrip("/") != expected.rstrip("/"):
        return (f"{url} answers 401 for another resource (resource_metadata "
                f"{named.group(1) if named else 'missing'}, expected {expected})")
    status, _, body = _get(expected)
    try:
        resource = json.loads(body).get("resource") if status == 200 else None
    except (ValueError, AttributeError):
        resource = None
    if not isinstance(resource, str) or resource.rstrip("/") != url.rstrip("/"):
        return f"{expected} answers {status} without resource {url}"
    return None


def _instant(text):
    """A Docker RFC 3339 timestamp (nanoseconds, Z) as an aware datetime, or None."""
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)", text.strip())
    if not match:
        return None
    whole, fraction, zone = match.groups()
    stamp = whole + "." + (fraction or "0")[:6].ljust(6, "0") + ("+00:00" if zone == "Z" else zone)
    return datetime.datetime.fromisoformat(stamp)


def _started(ctx, name):
    """When the service's running container (re)started: its StartedAt, as text and instant."""
    container = _compose(ctx, "ps", "-q", name).stdout.strip()
    if not container or len(container.split()) != 1:
        raise boring_deploy.Failure(f"{name}: expected one running container, found {container.split() or 'none'}")
    text = ctx.run("docker", "inspect", "--format", "{{.State.StartedAt}}", container).stdout.strip()
    instant = _instant(text)
    if instant is None:
        raise boring_deploy.Failure(f"{name}: container start time unreadable ({text!r})")
    return text, instant


def registrations(logs, since, backend=None):
    """Registration outcomes in `docker compose logs --timestamps --no-log-prefix` output,
    oldest first, logged at or after `since` (older lines belong to an earlier process).
    `backend`: a ferry's backend, whose attempts the ferry logs with its name."""
    marker = "MCP registration: " if backend is None else f"MCP registration of {backend}: "
    outcomes = []
    for line in logs.splitlines():
        stamp, _, message = line.partition(" ")
        instant = _instant(stamp)
        if instant is not None and instant >= since and marker in message:
            outcomes.append(message.split(marker, 1)[1])
    return outcomes


def smoke(ctx):
    """The service's own MCP endpoint answers at its URL (through the edge), and if it
    serves a Pond, the process started by this deploy has had its registration acknowledged.

    Claims no more: a registration ack says the Crocodile accepted the service, not that
    every worker can use it."""
    name, service = _service(ctx)
    inside = _config_inside(ctx, name, service)
    code = inspect.getsource(frontend_metadata) + """
import json, sys
print(json.dumps(frontend_metadata(sys.argv[1])))
"""
    fronts = json.loads(_compose(ctx, "exec", "-T", name, "python", "-I", "-c", code, inside).stdout) if inside else {"": {}}
    if "" not in fronts:
        _smoke_ferry(ctx, name, fronts)
        return
    config = fronts[""]
    deadline = time.monotonic() + SMOKE_SECONDS
    logs_hint = f"docker compose -f {ctx.home}/compose.yaml logs --tail 50"
    if config.get("url"):
        while True:
            problem = endpoint_identity(config["url"])
            if problem is None:
                break
            if time.monotonic() > deadline:
                raise boring_deploy.Failure(problem, logs_hint)
            time.sleep(2)
        ctx.log(f"{config['url']} is this service's MCP endpoint (401 naming {metadata_url(config['url'])})")
    if config.get("authority_url") is not None and config.get("url"):
        started_text, started = _started(ctx, name)
        while True:
            logs = _compose(ctx, "logs", "--since", started_text, "--timestamps", "--no-log-prefix",
                            "--no-color", name, check=False).stdout
            outcomes = registrations(logs, started)
            if outcomes and outcomes[-1].startswith("registered"):
                ctx.log(f"Pond: {name} (started {started_text}) {outcomes[-1]}")
                break
            if time.monotonic() > deadline:
                raise boring_deploy.Failure(f"no Pond registration by {name} since its start {started_text} "
                                            f"({outcomes[-1] if outcomes else 'nothing logged'})", logs_hint)
            time.sleep(2)


def _smoke_ferry(ctx, name, fronts):
    """A ferry: every configured front end answers at its URL, and each one that serves a
    Pond has its registration acknowledged since this start (the ferry names the backend).
    Whether the backend itself answers is the heartbeat's to show, not the deploy's."""
    logs_hint = f"docker compose -f {ctx.home}/compose.yaml logs --tail 50"
    deadline = time.monotonic() + SMOKE_SECONDS
    started_text, started = _started(ctx, name)
    for backend, front in fronts.items():
        while True:
            problem = endpoint_identity(front["url"])
            if problem is None:
                break
            if time.monotonic() > deadline:
                raise boring_deploy.Failure(f"{backend}: {problem}", logs_hint)
            time.sleep(2)
        ctx.log(f"{backend}: {front['url']} is its MCP endpoint (401 naming {metadata_url(front['url'])})")
        if front["authority_url"] is None:
            continue
        while True:
            logs = _compose(ctx, "logs", "--since", started_text, "--timestamps", "--no-log-prefix",
                            "--no-color", name, check=False).stdout
            outcomes = registrations(logs, started, backend)
            if outcomes and outcomes[-1].startswith("registered"):
                ctx.log(f"Pond: {backend} registered by {name} (started {started_text})")
                break
            if time.monotonic() > deadline:
                raise boring_deploy.Failure(f"no Pond registration of {backend} since {name} started {started_text} "
                                            f"({outcomes[-1] if outcomes else 'nothing logged'})", logs_hint)
            time.sleep(2)
