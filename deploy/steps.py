"""Deploy steps for a service (runtime docker; see boring_deploy).

The HOME holds the operator's compose.yaml (one service, image ${BORING_IMAGE})
and everything the service reads; the deploy changes only .env. RELEASE.json
names the image, the package to import, and for a service with a schema the
data file and how to read its generation.

An update leaves a stopped stack stopped: the engine's Docker runtime decides
(no `compose up`, the new image still in .env) and then skips the smoke.
"""
import datetime
import inspect
import json
import re
import sqlite3
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


def _compose(ctx, *args, image=None, check=True):
    env = {"BORING_IMAGE": image} if image else None
    return ctx.run("docker", "compose", "-f", str(ctx.home / "compose.yaml"), *args, env=env, check=check)


def _service(ctx, image=None):
    config = json.loads(_compose(ctx, "config", "--format", "json", image=image).stdout)
    (name, service), = config["services"].items()
    return name, service


def check(ctx):
    """The site's compose file renders with the new image, and the image holds the service."""
    name, service = _service(ctx, image=ctx.image)
    module = ctx.release.details["module"]
    ctx.run("docker", "run", "--rm", "--entrypoint", "python", ctx.image, "-I", "-c", f"import {module}")
    ctx.log(f"{name}: compose renders with {ctx.image}; {module} imports")
    inside = _config_inside(ctx, name, service)
    if inside:
        _check_crocodile_reach(ctx, name, inside)


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


def _check_crocodile_reach(ctx, name, inside):
    """Before the switch: from a one-off container of the new image on the stack's own
    network, user and config (`compose run`, removed again), GET the Crocodile URL the
    config registers or introspects with, as the service's own client does (httpx2,
    system trust store, no proxy environment). A config without one is not checked."""
    code = inspect.getsource(crocodile_reach) + (
        "import json, sys\n"
        "c = json.load(open(sys.argv[1]))\n"
        "url = (c.get('pond') or {}).get('url') or (c.get('oauth') or {}).get('introspection_url')\n"
        "def get(u):\n"
        "    import httpx2\n"
        "    return httpx2.get(u, timeout=5, trust_env=False).status_code\n"
        "print(json.dumps({'url': url, 'problem': url and crocodile_reach(url, get)}))\n")
    result = _compose(ctx, "run", "--rm", "--no-deps", "-T", "--entrypoint", "python", name,
                      "-I", "-c", code, inside, image=ctx.image)
    answer = json.loads(result.stdout.strip().splitlines()[-1])
    if not answer["url"]:
        return
    if answer["problem"]:
        raise boring_deploy.Failure(f"{name}: the container cannot reach the Crocodile at {answer['url']} "
                                    f"({answer['problem']}); nothing switched", REACH_HINT)
    ctx.log(f"{name}: the new image reaches {answer['url']} from the stack's network")


def _data(ctx):
    data = ctx.release.details.get("data")
    if not data or not (ctx.home / data["file"]).is_file():
        return None, None
    return ctx.home / data["file"], data


def data_generation(ctx):
    path, data = _data(ctx)
    if path is None:
        return None
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        row = db.execute(data["query"]).fetchone()
    return int(row[0]) if row else None


def backup(ctx, directory):
    path, _ = _data(ctx)
    target = Path(directory) / path.name
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as source, closing(sqlite3.connect(target)) as copy:
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


def registrations(logs, since):
    """Registration outcomes in `docker compose logs --timestamps --no-log-prefix` output,
    oldest first, logged at or after `since` (older lines belong to an earlier process)."""
    outcomes = []
    for line in logs.splitlines():
        stamp, _, message = line.partition(" ")
        instant = _instant(stamp)
        if instant is not None and instant >= since and "MCP registration: " in message:
            outcomes.append(message.split("MCP registration: ", 1)[1])
    return outcomes


def smoke(ctx):
    """The service's own MCP endpoint answers at its URL (through the edge), and if it
    serves a Pond, the process started by this deploy has had its registration acknowledged.

    Claims no more: a registration ack says the Crocodile accepted the service, not that
    every worker can use it."""
    name, service = _service(ctx)
    inside = _config_inside(ctx, name, service)
    config = json.loads(_compose(ctx, "exec", "-T", name, "cat", inside).stdout) if inside else {}
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
    if config.get("pond"):
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
