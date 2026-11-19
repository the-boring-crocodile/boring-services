# Boring Services

Four independent MCP services and an optional gateway, for Linux and
Windows. A Pond reaches services through its Crocodile; an MCP client can
also connect directly using the service's configured authentication.
No service needs a Pond.

| Service | What it does |
|---|---|
| [Knowledge](knowledge/README.md) | Shared notebooks with append-only, attributed history |
| [Canvas](canvas/README.md) | Drawings (Excalidraw) that agents push and people draw on in the browser |
| [Fishmarket](fishmarket/README.md) | Temporary one-file handovers between an agent and a browser |
| [D365 Sourcecache](sourcecache-d365/README.md) | Immutable indexes of D365 source: elements, labels, search |

[MCP Ferry](mcp-ferry/README.md) carries selected backends behind four
discovery and call tools. Use it when a client benefits from a smaller tool
list. The backends retain their operations, permissions and running-job
handles; the gateway forwards their results without retrying their work.

[Boring Tunnel](tunnel/README.md) is a separate optional Linux component for
pondNT's desktop tunnel: restricted SSH account and Crocodile service registration,
with no container or daemon. Standard service builds do not include it.

All of it — code, tests, docs — was written by AI (OpenAI's models and Anthropic's Claude); Wald decided what to build, tested it, ran it and had the last word ([all of it is AI](https://github.com/the-boring-crocodile/boring-pond/blob/main/README.md#all-of-it-is-ai)).

Every service speaks Streamable HTTP MCP and selects one authentication mode
in its JSON configuration: Crocodile, keys or its own issuer. The Crocodile
grant, each key or the own issuer supplies the reader/writer role:
[CONFIGURATION](docs/CONFIGURATION.md), examples in [examples/](examples/).

## Outcomes for service authors

[Outcome causes](OUTCOME_CODES.md) declares the local additions; all other locally produced causes use Common's shared list.

Use Common 0.8's `Refused(code, message, **details)` before work starts and
`Failed(code, message, **details)` after work starts and may have had effects.
The code names the cause independently of status. The service-kit wrapper
renders `as_data()` as one compact JSON text error:
`{"status":"refused","code":"stale","message":"read it again","current_version":2}`.
Details are flat, JSON values; convert a Path to str at the construction site.
The wrapper stays text-only so clients do not resend a second structured copy.

A downstream outcome needs `status: refused|failed`, a valid lowercase code
and a string message. Preserve that status, code and details when carrying it.
A bare `{code,message}` is no longer valid: the worker reports failed/upstream_error
with the service's text and unknown effects. `as_dict()` was removed; use
`as_data()`. OAuth/HTTP authority and JSON-RPC listing errors keep their own
protocol shapes. Tool lifecycle records keep their own fields.

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
(`/.well-known/oauth-protected-resource/<MCP path>`), Canvas's browser routes
under the MCP URL's base, and with its own sign-in also the paths above.
Canvas derives the browser address from its MCP URL. The deploy checks what applies: the endpoint at the
configured URL always; that the issuer it trusts (a Crocodile, or another
issuer's introspection URL) can be reached from inside the container, when it
trusts one; that it registered, only when it serves a Pond.
[CONFIGURATION](docs/CONFIGURATION.md) has the configuration for each case.

## Installing on Linux

Each service ships as a release artifact and installs itself on its first
deploy (Docker; how deploy, rollback and uninstall work:
[boring-deploy](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/packages/boring-deploy/README.md)).
This continues the example site of boring-crocodile `docs/INSTALL.md`: a
Crocodile at `crocodile-url.example.test` with the Ponds `pond` and `garden`,
and every service on the same domain.

### Before the first install

**Architecture.** The Linux release archives target x86_64. They include the
locked wheels used to build their container images. Allow disk space for the
new and previous image plus the service's persistent data; there is no single
RAM or disk minimum for all services.

**Docker** Engine with the Compose v2 plugin, 2.24 or later (Debian and
Ubuntu: the packages `docker.io` and `docker-compose-v2`).

**Your reverse proxy** (nginx, Apache, Traefik, Caddy, ...) forwards each
service's paths, unchanged, next to the Crocodile's routes
([The reverse proxy](https://github.com/the-boring-crocodile/boring-crocodile/blob/main/docs/INSTALL.md#the-reverse-proxy)).
Every MCP URL also needs its own metadata path,
`/.well-known/oauth-protected-resource/` followed by the MCP path: the
deploy's smoke checks that the MCP URL answers 401 naming it.

| Service | Paths | To |
|---|---|---|
| Canvas | `/services/canvas/*` (MCP and browser) and its MCP metadata | `127.0.0.1:18511` |
| Knowledge | `/services/knowledge/mcp` and its metadata | `127.0.0.1:18512` |
| Sourcecache | `/services/sourcecache-d365/mcp` and its metadata | `127.0.0.1:18513` |
| Fishmarket | `/fishmarket/*` and `/.well-known/oauth-protected-resource/fishmarket/mcp` | `fishmarket:8000` on the proxy's Docker network (no host port; with a proxy on the host instead: `docker network create fishmarket-net`, use that as `N` in the block, add `--loopback-port` to its first install and route to `127.0.0.1:18518`; an install made without it: add `ports: ["127.0.0.1:18518:8000"]` under the service in `/opt/boring-fishmarket/compose.yaml` and run its `deploy` again) |
| MCP Ferry | `/services/NAME/mcp` and its metadata, per backend | `127.0.0.1:18520` to `18529`, one port per backend |

For Canvas, in Caddy (the same pattern for every row of the table), inside the site block that already holds the
Crocodile's routes (`crocodile-url.example.test { ... }`):

        handle /services/canvas/* {
            reverse_proxy 127.0.0.1:18511
        }
        handle /.well-known/oauth-protected-resource/services/canvas/mcp {
            reverse_proxy 127.0.0.1:18511
        }

**The container reaches the Crocodile.**
The container itself calls the Crocodile's public URL (token introspection
and registration), so from inside Docker that name must resolve to an address the container can reach, the host
firewall must let the stack's network reach the proxy's port 443 (Canvas
`172.16.42.0/24`, Knowledge `172.16.43.0/24`, Sourcecache `172.16.44.0/24`,
Ferry `172.16.46.0/24`, in each `compose.yaml`; Fishmarket has none of its
own: it is on your proxy's network), and the certificate must be one the
container trusts. With public DNS and a public CA (Let's Encrypt, for one)
there is nothing to do. The deploy's check tries exactly that before the
switch (a one-off container of the new image, with the stack's settings, GETs
the Crocodile URL in `config.json`) and refuses, naming name, connection or
TLS; fix it and run the deploy again without the first-install options: it
reuses what the first run set up, for example
`sudo /root/releases/boring-$S-$V/deploy` in the same shell.

**A name only your host knows, or a private CA.** Inside the container the
name may not resolve at all, or, depending on how the host resolves it,
resolve to `127.0.0.1`, which there is the container itself (the check then
says "connection refused"; ours did). And the image trusts only public CAs. After a refused first install, add both to the service's container in
`HOME/compose.yaml` (for Canvas `/opt/boring-canvas/compose.yaml`; the host's
bundle must already hold your root, e.g. through `update-ca-certificates`):

        extra_hosts: ["crocodile-url.example.test:host-gateway"]   # the host, as seen from the container
        volumes:
          - /etc/ssl/certs/ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro   # beside the existing mounts

then run the deploy again as above; its check uses these settings. Until a
deploy has passed, the image setting is not there yet: deploy writes its
`boring-image.env` only at the switch. The operator's `.env` is kept on
updates and uninstall; migration removes only the legacy `BORING_IMAGE=`
line, preserving its mode and all other lines.

For manual Compose commands in the service home, use
`sudo docker compose --env-file boring-image.env config` (or `logs`,
`up -d`, `exec`, ...). If you keep an operator `.env`, load it first:
`sudo docker compose --env-file .env --env-file boring-image.env config`.
Deploy, smoke, uninstall and the Canvas admin helper choose these files
automatically. Include the same env flags when adding a `-f` path.

### Back to a release built on Common before 0.8

The old deployer rewrites `.env` as 0644, including any operator values.
Before invoking it, choose the service HOME from `/etc/boring-pond/homes.json`
and a new root-only recovery directory. Save an existing operator file with
`sudo cp -p "$SERVICE_HOME/.env" "$RECOVERY/operator.env"`.
Keep HOME inaccessible to unrelated local accounts during this window; the
backup alone does not protect the old writer's temporary 0644 copy.

Set `boring-image.env` aside in that recovery directory for the old runtime.
After the old deploy, including a failed attempt, retain its `BORING_IMAGE=`
line privately if it rewrote `.env`. Restore the operator file with
`sudo cp -p "$RECOVERY/operator.env" "$SERVICE_HOME/.env"`, preserving its
owner and mode, then put that image line back without changing the mode.
The old runtime reads its image choice from `.env`, not
`boring-image.env`. If there was no operator `.env`, retain the old deployer's
image-only file. If the attempt stops before the switch and you keep the newer
runtime, restore its saved `boring-image.env` too. Verify the selected old image and private file permissions
before any manual start, then restore HOME's original directory permissions.

When rolling back across a listener/configuration format change, restore
the saved service JSON and Compose together, including Ferry's frontend files. The ordered procedure and the post-adoption resume point are in
[updating a site](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/updating.md#4-rolling-back).

### First install, one block per service

Read the [release notes](CHANGELOG.md) and choose your component's archive
from the [releases page](https://github.com/the-boring-crocodile/boring-services/releases).
The download block starts with Canvas. To install a different service, use
its `V` and `S` assignment from the update blocks below.

    V=0.3.0 S=canvas
    f=boring-$S-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-services/releases/download/$S-v$V
    mkdir -p ~/Downloads && cd ~/Downloads &&
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&
    sudo install -d -m 0700 /root/releases &&
    sudo tar xzf "$f" -C /root/releases

Each command below registers and installs the service. The installer reads the
local Crocodile's address from its admin inventory, shows the service-add plan
and asks for typed APPLY. The credential goes straight into HOME/credentials;
the generated config contains one authority block. No manual service-add,
secret capture or per-service access list is needed. Add `--check` for a
read-only plan.

#### Canvas

    sudo /root/releases/boring-canvas-VERSION/deploy --install --url https://crocodile-url.example.test/services/canvas/mcp --callers pond=writer,garden=writer

MCP and browser routes share one listener. A canvas page is at
`https://crocodile-url.example.test/services/canvas/canvas/<id>`.
Add `--unsafe-url-auth` only when links should carry the browser token.

#### Knowledge

    sudo /root/releases/boring-knowledge-VERSION/deploy --install --url https://crocodile-url.example.test/services/knowledge/mcp --callers pond=writer,garden=reader

[Install one service](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/install-service.md)
walks through the first Knowledge install.
[Add access later](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/add-service.md)
changes the caller/role map with one grant command and no restart.

#### Sourcecache

Build a [snapshot](sourcecache-d365/README.md#building-a-snapshot) first and
stage it as a protected root-owned input:

    sudo /root/releases/boring-sourcecache-d365-VERSION/deploy --install --url https://crocodile-url.example.test/services/sourcecache-d365/mcp --callers pond=reader,garden=reader --index /root/snapshot.db

#### Fishmarket

The proxy's Docker network must exist; it reaches the service as
`fishmarket:8000` there. Replace edge with that network's name:

    sudo /root/releases/boring-fishmarket-VERSION/deploy --install --url https://crocodile-url.example.test/fishmarket/mcp --callers pond=writer,garden=writer --edge-network edge

For a proxy on the host, use a dedicated network and add `--loopback-port`
to publish `127.0.0.1:18518`. Add `--unsafe-url-auth` only when receipts
should carry a link with its token.

#### MCP Ferry

Prepare the business routing and upstream credentials under a protected,
root-owned `/root/ferry-config`. For example, config.json:

```json
{"backends":{"homeassistant":{"service":"services/homeassistant.json","upstream":{"url_file":"../credentials/homeassistant-url"}}}}
```

Use `{}` for services/homeassistant.json; the installer fills its authority.
Provision credentials/homeassistant-url privately with the backend's URL and
any secret path. No credential belongs in a command line or chat.
The [Ferry guide](mcp-ferry/README.md) describes isolated backend networks.

    sudo /root/releases/boring-mcp-ferry-VERSION/deploy --install --config-dir /root/ferry-config --url https://crocodile-url.example.test/services/homeassistant/mcp --callers pond=reader,garden=reader

Several frontends take `--url NAME=URL,...` for exactly the selected backend
and gateway frontend names. Each gets its own grant and credential. First
install assigns listener addresses from the template's fixed default-network
address and published TCP target range, recording them in Compose. The
backend containers belong in the operator's Compose file, on separate
networks shared only with Ferry. An unavailable backend does not stop Ferry.

### First-install settings

| Service | Settings |
|---|---|
| Crocodile mode | `--url MCP_URL`, `--callers NAME=ROLE,...`; no prepared auth config for Canvas, Knowledge, Sourcecache or Fishmarket |
| keys/own issuer | `--config-dir DIR` with the explicit mode and its files; no `--url` registration |
| Canvas | `--unsafe-url-auth` |
| Fishmarket | `--edge-network NAME`, optional `--loopback-port`, `--unsafe-url-auth` |
| Ferry | `--config-dir DIR` for routing, selected frontend files and upstream credentials |
| Sourcecache | `--index FILE` |

Selected input files, directories and their parents must be root-owned,
protected from other writers and free of links. Plans show paths and
permissions; apply checks again before effects. Container JSON has no bind,
port or behind_proxy fields: Compose owns listeners and exposure.

HOME is by default the release's `/opt/boring-SERVICE`. Name another HOME as the deploy's
argument on first install; it is recorded in `/etc/boring-pond/homes.json`.
The installer creates the service account, state/, config/, credentials/ and
Compose file. Later deploys replace the image and keep the operator's Compose.
Grant/role changes need no restart and take effect within at most60seconds.
Configuration changes require a service restart.

If an install fails, inspect the named step, service-list and the installed
files before retrying. Registration may have succeeded before a later failure;
preserve the once-only credential. The installer refuses to overwrite it.
Uninstall keeps persistent files. With `--delete-data`, it removes matching
registrations after stop through the admin's reviewed plan, seals retained
folders, then removes the account; it never deletes data.

A successful deploy checks endpoint metadata and acknowledged registration.
The proxy must route the MCP URL and its protected-resource metadata. In a
granted Pond, service_list and service_describe show the service.

### Updating a service

Read the release's entry in [CHANGELOG.md](CHANGELOG.md) first. Then the service's block, in one
shell: download, check, unpack, the deploy's read-only `--check`, and the
deploy, which runs only when the check passed. Each block uses the version
from the 0.5 family release set.

Canvas:

    V=0.3.0 S=canvas
    f=boring-$S-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-services/releases/download/$S-v$V
    mkdir -p ~/Downloads && cd ~/Downloads &&
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&            # must print OK; if not, the block stops here
    sudo mkdir -p /root/releases && sudo tar xzf "$f" -C /root/releases &&
    sudo /root/releases/boring-$S-$V/deploy --check &&
    sudo /root/releases/boring-$S-$V/deploy

Knowledge:

    V=0.3.0 S=knowledge
    f=boring-$S-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-services/releases/download/$S-v$V
    mkdir -p ~/Downloads && cd ~/Downloads &&
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&            # must print OK; if not, the block stops here
    sudo mkdir -p /root/releases && sudo tar xzf "$f" -C /root/releases &&
    sudo /root/releases/boring-$S-$V/deploy --check &&
    sudo /root/releases/boring-$S-$V/deploy

Sourcecache:

    V=0.2.0 S=sourcecache-d365
    f=boring-$S-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-services/releases/download/$S-v$V
    mkdir -p ~/Downloads && cd ~/Downloads &&
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&            # must print OK; if not, the block stops here
    sudo mkdir -p /root/releases && sudo tar xzf "$f" -C /root/releases &&
    sudo /root/releases/boring-$S-$V/deploy --check &&
    sudo /root/releases/boring-$S-$V/deploy

Fishmarket:

    V=0.2.0 S=fishmarket
    f=boring-$S-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-services/releases/download/$S-v$V
    mkdir -p ~/Downloads && cd ~/Downloads &&
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&            # must print OK; if not, the block stops here
    sudo mkdir -p /root/releases && sudo tar xzf "$f" -C /root/releases &&
    sudo /root/releases/boring-$S-$V/deploy --check &&
    sudo /root/releases/boring-$S-$V/deploy

MCP Ferry:

    V=0.2.0 S=mcp-ferry
    f=boring-$S-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-services/releases/download/$S-v$V
    mkdir -p ~/Downloads && cd ~/Downloads &&
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&            # must print OK; if not, the block stops here
    sudo mkdir -p /root/releases && sudo tar xzf "$f" -C /root/releases &&
    sudo /root/releases/boring-$S-$V/deploy --check &&
    sudo /root/releases/boring-$S-$V/deploy

## Installing natively (Windows, or Linux without Docker)

[CONFIGURATION](docs/CONFIGURATION.md#native-installation): build the wheels
for the target, install them into a venv with `uv`, start each service with
its configuration.

## Development

boring-service-kit comes from Pond Common's 0.8 release line. Use the exact
Common versions and hashes from each service's `requirements.lock`
(`scripts/lock.py`). This check uses a fresh venv for each service:

    python scripts/check.py --common-wheels <Common dist>

Release artifacts: `scripts/build.py`. Changes: [CHANGELOG](CHANGELOG.md).

## Licence

Code under the GNU AGPL, version 3 or later; documentation under CC BY-SA
4.0. The example configurations (`examples/`) are public domain (CC0).
Canvas bundles Excalidraw, under its own licences. What you make with the
tools is yours. Details and the exact terms: [COPYING.md](COPYING.md).
