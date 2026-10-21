# Boring Services

Four independent MCP services, for Linux and Windows. A Pond reaches them
through its Crocodile; Visual Studio Copilot, Claude, ChatGPT or a script can
also connect to each one directly. No service needs a Pond.

| Service | What it does |
|---|---|
| [Knowledge](knowledge/README.md) | Shared notebooks with append-only, attributed history |
| [Canvas](canvas/README.md) | Drawings (Excalidraw) that agents push and people draw on in the browser |
| [Fishmarket](fishmarket/README.md) | Temporary one-file handovers between an agent and a browser |
| [D365 Sourcecache](sourcecache-d365/README.md) | Immutable indexes of D365 source: elements, labels, search |

All of it — code, tests, docs — was written by AI (OpenAI's models and Anthropic's Claude); Wald decided what to build, tested it, ran it and had the last word ([all of it is AI](https://github.com/the-boring-crocodile/boring-pond/blob/main/README.md#all-of-it-is-ai)).

Every service speaks Streamable HTTP MCP with OAuth and an explicit
reader/writer list, configured by one JSON file. It can sign clients in by
itself, trust a Crocodile or another issuer, or take access keys:
[CONFIGURATION](docs/CONFIGURATION.md), examples in [examples/](examples/).

## Where a service lives

A service answers at whatever URL its configuration names (`url`). The example
below puts Canvas on the Crocodile's domain under a path; a service can just
as well have its own name (`https://knowledge-url.example.test/mcp`) or run on
another host:

- **On the Crocodile's domain, under a path** (`/services/canvas/mcp`): one
  name and one certificate for everything; the service trusts that Crocodile.
- **On its own name, trusting a Crocodile:** the same, with the service's
  routes on its own name. The Crocodile may be on another host; the service
  must reach its public URL (registration and token checks, see below).
- **On its own name, with its own sign-in** (no Crocodile at all,
  [examples/standalone.json](examples/standalone.json)): its sign-in paths
  (`/authorize`, `/token`, `/revoke`, `/approve` and the authorization-server
  metadata) sit at the root of its name, so it needs a name of its own, not a
  path on a Crocodile's domain where those paths are the Crocodile's.

Whichever it is, the reverse proxy forwards the service's paths on that name
to its loopback ports: the MCP path, that path's metadata
(`/.well-known/oauth-protected-resource/<MCP path>`), Canvas's `/canvas/*`,
and with its own sign-in also the paths above. Canvas's `--public-url` names
the host its pages are on. The deploy checks what applies: the endpoint at the
configured URL always; that the issuer it trusts (a Crocodile, or another
issuer's introspection URL) can be reached from inside the container, when it
trusts one; that it registered, only when it serves a Pond.
[CONFIGURATION](docs/CONFIGURATION.md) has the configuration for each case.

## Installing on Linux

Each service ships as a release artifact and installs itself on its first
deploy (Docker; how deploy, rollback and uninstall work:
[boring-deploy](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/packages/boring-deploy/README.md)).
This continues the example site of boring-crocodile `docs/INSTALL.md`: a
Crocodile at `crocodile-url.example.test` with the Ponds `pond` and `garden`.
We add Canvas on the same domain. Whatever reverse proxy you use (nginx,
Apache, Traefik, Caddy, ...), it forwards three paths, unchanged, next to the
Crocodile's routes ([The reverse proxy](https://github.com/the-boring-crocodile/boring-crocodile/blob/main/docs/INSTALL.md#the-reverse-proxy)),
to the ports the container publishes on loopback:

- `/canvas/*` (the canvas pages) to `127.0.0.1:18510`;
- `/services/canvas/mcp` (its MCP endpoint) to `127.0.0.1:18511`;
- `/.well-known/oauth-protected-resource/services/canvas/mcp` (the MCP URL's
  own metadata) to `127.0.0.1:18511`: the deploy's smoke checks that the MCP
  URL answers 401 naming that metadata.

An example configuration for Caddy:

        handle /canvas/* {
            reverse_proxy 127.0.0.1:18510
        }
        handle /services/canvas/mcp {
            reverse_proxy 127.0.0.1:18511
        }
        handle /.well-known/oauth-protected-resource/services/canvas/mcp {
            reverse_proxy 127.0.0.1:18511
        }

The container itself calls the Crocodile's public URL (token introspection
and registration), so from inside Docker that name must resolve, the host
firewall must let the container's network (Canvas: `172.16.42.0/24`, in
`compose.yaml`) reach the proxy's port 443, and the certificate must be one
the container trusts (a public CA is; a private one needs its root mounted
into the container). The deploy's check tries exactly that before the switch
(a one-off container of the new image on the stack's network GETs the
Crocodile URL in `config.json`) and refuses, naming name, connection or TLS;
fix it and run the same deploy again.

Check and unpack the release as for the Crocodile: `sha256sum` against the
published value, then `sudo tar xzf ... -C /root/releases`, as root.

First the Crocodile registers Canvas for both Ponds and issues its
credentials ([SERVICES_AND_PIRATES](https://github.com/the-boring-crocodile/boring-crocodile/blob/main/docs/SERVICES_AND_PIRATES.md)):

    sudo boring-crocodile-admin service-add canvas --url https://crocodile-url.example.test/services/canvas/mcp --ponds pond,garden

It prints the client id and secret once, as JSON on stdout (a reminder goes
to stderr): send its output to a root-only file
(`... > /root/canvas-config/service-add.txt`, then `chmod 600`), never into a
chat or a log. Put them into the configuration directory: `/root/canvas-config/config.json`
from [examples/canvas-family.json](examples/canvas-family.json) with the
client id in both places, and the secret alone in
`/root/canvas-config/credentials/mcp-client-secret` (mode 0600; the install puts
`credentials/` into the service's home beside `config/`, where `config.json`
names it as `../credentials/mcp-client-secret`). Then the first deploy installs Canvas:

    sudo /root/releases/boring-canvas-VERSION/deploy --install --config-dir /root/canvas-config \
        --public-url https://crocodile-url.example.test

| Service | First-install settings |
|---|---|
| all | `--config-dir DIR`: the service's configuration (`config.json`), the files it names and `credentials/` (the secrets) |
| Canvas | `--public-url https://DOMAIN`, `--web-bind ADDRESS` (default 127.0.0.1), `--unsafe-url-auth` |
| Fishmarket | `--edge-network NAME` (the Docker network the reverse proxy uses), `--unsafe-url-auth` |
| Sourcecache | `--index FILE` (a built snapshot) |

The install creates the service account, `state/`, `config/` (a copy of
`--config-dir`), `credentials/` (a copy of its `credentials/`) and `compose.yaml` in the service's home, by default the
release's `/opt/boring-SERVICE` (a site that keeps its stacks elsewhere, such
as `/opt/docker/boring-SERVICE`, names the home as the deploy's argument on
the first install, which records it in `/etc/boring-pond/homes.json`); from then on those files are the site's: a
later change goes into `HOME/config/` (a secret into `HOME/credentials/`) and
takes effect when the service restarts, and a deploy only replaces the image.
Uninstall keeps `state/`, `config/` and `credentials/`; with `--delete-data` it first seals them (root's, mode 0700) and
then removes the account, which it keeps when a seal fails. The deploy's
smoke check waits until an unauthenticated request to
the service's URL answers 401 naming that URL's resource metadata (RFC 9728,
`/.well-known/oauth-protected-resource/...`, whose `resource` is the URL), and
the running container has logged an acknowledged registration since it started;
the proxy must route both paths. In `pond` or `garden`,
`service_list` now shows `canvas`.

## Installing natively (Windows, or Linux without Docker)

[CONFIGURATION](docs/CONFIGURATION.md#native-installation): build the wheels
for the target, install them into a venv with `uv`, start each service with
its configuration.

## Development

boring-service-kit comes from Pond Common releases (0.7), pinned by
hash in each service's `requirements.lock` (`scripts/lock.py`). One check, every
service in its own fresh venv from its lock:

    python scripts/check.py --common-wheels <Common dist>

Release artifacts: `scripts/build.py`. Changes: [CHANGELOG](CHANGELOG.md).

## Licence

Code under the GNU AGPL, version 3 or later; documentation under CC BY-SA
4.0. The example configurations (`examples/`) are public domain (CC0).
Canvas bundles Excalidraw, under its own licences. What you make with the
tools is yours. Details and the exact terms: [COPYING.md](COPYING.md).
