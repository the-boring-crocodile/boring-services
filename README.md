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
Crocodile at `crocodile-url.example.test` with the Ponds `pond` and `garden`,
and every service on the same domain.

### Before the first install

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
| Canvas | `/canvas/*` (the canvas pages) | `127.0.0.1:18510` |
| | `/services/canvas/mcp` and its metadata | `127.0.0.1:18511` |
| Knowledge | `/services/knowledge/mcp` and its metadata | `127.0.0.1:18512` |
| Sourcecache | `/services/sourcecache-d365/mcp` and its metadata | `127.0.0.1:18513` |
| Fishmarket | `/fishmarket/*` and `/.well-known/oauth-protected-resource/fishmarket/mcp` | `fishmarket:8000` on the proxy's Docker network (no host port; with a proxy on the host instead: `docker network create fishmarket-net`, use that as `N` in the block; its first deploy then stops at the smoke check (502: nothing reaches it yet), as expected; add `ports: ["127.0.0.1:18518:8000"]` under the service in `/opt/boring-fishmarket/compose.yaml`, route to `127.0.0.1:18518` and run its `deploy` again) |
| MCP Ferry | `/services/NAME/mcp` and its metadata, per backend | `127.0.0.1:18520` to `18529`, one port per backend |

For Canvas, in Caddy (the same pattern for every row of the table), inside the site block that already holds the
Crocodile's routes (`crocodile-url.example.test { ... }`):

        handle /canvas/* {
            reverse_proxy 127.0.0.1:18510
        }
        handle /services/canvas/mcp {
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
deploy has passed, `docker compose config` in the home fails with
"BORING_IMAGE not set": the deploy writes `.env` with the image only when it
gets that far.

### First install, one block per service

Each block runs in one shell and stops at the first step that fails. It
downloads the release (`V`; the tested set is in boring-pond's
[releases and versions](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/updating.md#releases-and-versions),
the newest on the [releases page](https://github.com/the-boring-crocodile/boring-services/releases),
tags `SERVICE-vV`), checks and unpacks it as root, writes the service's
`config.json` into `/root/SERVICE-config`, has the Crocodile register the
service for the example site's Ponds `pond` and `garden`
([SERVICES_AND_PIRATES](https://github.com/the-boring-crocodile/boring-crocodile/blob/main/docs/SERVICES_AND_PIRATES.md)),
and installs it.

`service-add` prints the service's client id and secret once, as JSON. The
block writes that straight into a file only root can read, never into a chat
or a log (the redirection runs inside the root shell; with a plain
`sudo ... > file` your own shell would open the file), then moves the secret
into `credentials/mcp-client-secret` (mode 0600), puts the client id into
`config.json` and deletes the printout. `service-add` takes effect at once:
the running Crocodile accepts the service's registration from then on, and
the Ponds see it after its first deploy has registered it. The `"bind":
"0.0.0.0"` in each config is inside the container; Docker publishes the port
on 127.0.0.1 only. Put your own
Ponds into `--ponds` and the `access` list (after
[getting started](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/getting-started.md):
`pond`, and `kingfisher` if you added it). Change the
`access` list (who may read or write) before you run the block, or later in
the service's `HOME/config/config.json`.

**If a block stops.** Before `service-add`, fix the cause and paste it
again. After it, the service is registered (pasting the whole block again
stops at `service-add`: "exists"), and until the next line has run, its
one-time printout, secret included, is in `/root/SERVICE-config/service-add.txt`
(root's only). Then either run the remaining lines from the one that failed
(a stop at the deploy: fix and run `deploy` again, as above), or start over:
`sudo boring-crocodile-admin service-list` shows the record, and
`sudo boring-crocodile-admin service-remove NAME --expected-definition 'RECORD'`
removes it, with RECORD that entry's JSON.

#### Canvas

    V=0.2.19 S=canvas D=crocodile-url.example.test
    f=boring-$S-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-services/releases/download/$S-v$V
    mkdir -p ~/Downloads && cd ~/Downloads &&
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&            # must print OK; if not, the block stops here
    sudo mkdir -p /root/releases && sudo tar xzf "$f" -C /root/releases &&
    sudo install -d -m 0700 /root/$S-config &&
    sudo tee /root/$S-config/config.json > /dev/null <<EOF &&
    {"url": "https://$D/services/$S/mcp", "bind": "0.0.0.0", "port": 18511,
     "oauth": {"issuer": "https://$D", "introspection_url": "https://$D/introspect",
               "client_id": "svc_FROM_SERVICE_ADD", "secret_file": "../credentials/mcp-client-secret"},
     "pond": {"url": "https://$D", "client_id": "svc_FROM_SERVICE_ADD", "secret_file": "../credentials/mcp-client-secret"},
     "access": [{"subject": "pond", "role": "writer", "author": "pond"},
                {"subject": "garden", "role": "writer", "author": "garden"}]}
    EOF
    sudo sh -c "umask 077 && boring-crocodile-admin service-add $S --url https://$D/services/$S/mcp --ponds pond,garden > /root/$S-config/service-add.txt" &&
    sudo python3 - /root/$S-config credentials/mcp-client-secret <<'PY' &&
    import json, os, pathlib, sys          # the secret into its file, the client id into the JSON files
    d, secret = pathlib.Path(sys.argv[1]), sys.argv[2]; os.umask(0o077); added = json.loads((d / "service-add.txt").read_text())
    (d / secret).parent.mkdir(parents=True, exist_ok=True); (d / secret).write_text(added["client_secret"])
    for f in d.rglob("*.json"): f.write_text(f.read_text().replace("svc_FROM_SERVICE_ADD", added["client_id"]))
    (d / "service-add.txt").unlink()
    PY
    sudo /root/releases/boring-$S-$V/deploy --install --config-dir /root/$S-config --public-url https://$D

Each canvas then has its page at `https://crocodile-url.example.test/canvas/<id>`
(`/canvas/` itself answers 404). `--web-bind ADDRESS` publishes the pages
elsewhere than 127.0.0.1.

#### Knowledge

    V=0.2.15 S=knowledge D=crocodile-url.example.test
    f=boring-$S-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-services/releases/download/$S-v$V
    mkdir -p ~/Downloads && cd ~/Downloads &&
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&            # must print OK; if not, the block stops here
    sudo mkdir -p /root/releases && sudo tar xzf "$f" -C /root/releases &&
    sudo install -d -m 0700 /root/$S-config &&
    sudo tee /root/$S-config/config.json > /dev/null <<EOF &&
    {"url": "https://$D/services/$S/mcp", "bind": "0.0.0.0", "port": 18512,
     "oauth": {"issuer": "https://$D", "introspection_url": "https://$D/introspect",
               "client_id": "svc_FROM_SERVICE_ADD", "secret_file": "../credentials/mcp-client-secret"},
     "pond": {"url": "https://$D", "client_id": "svc_FROM_SERVICE_ADD", "secret_file": "../credentials/mcp-client-secret"},
     "access": [{"subject": "pond", "role": "writer", "author": "pond"},
                {"subject": "garden", "role": "reader", "author": "garden"}]}
    EOF
    sudo sh -c "umask 077 && boring-crocodile-admin service-add $S --url https://$D/services/$S/mcp --ponds pond,garden > /root/$S-config/service-add.txt" &&
    sudo python3 - /root/$S-config credentials/mcp-client-secret <<'PY' &&
    import json, os, pathlib, sys          # the secret into its file, the client id into the JSON files
    d, secret = pathlib.Path(sys.argv[1]), sys.argv[2]; os.umask(0o077); added = json.loads((d / "service-add.txt").read_text())
    (d / secret).parent.mkdir(parents=True, exist_ok=True); (d / secret).write_text(added["client_secret"])
    for f in d.rglob("*.json"): f.write_text(f.read_text().replace("svc_FROM_SERVICE_ADD", added["client_id"]))
    (d / "service-add.txt").unlink()
    PY
    sudo /root/releases/boring-$S-$V/deploy --install --config-dir /root/$S-config

#### Sourcecache

It serves a snapshot you built first ([Building a snapshot](sourcecache-d365/README.md#building-a-snapshot));
`I` below is that file.

    V=0.1.18 S=sourcecache-d365 D=crocodile-url.example.test I=/root/snapshot.db
    f=boring-$S-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-services/releases/download/$S-v$V
    mkdir -p ~/Downloads && cd ~/Downloads &&
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&            # must print OK; if not, the block stops here
    sudo mkdir -p /root/releases && sudo tar xzf "$f" -C /root/releases &&
    sudo install -d -m 0700 /root/$S-config &&
    sudo tee /root/$S-config/config.json > /dev/null <<EOF &&
    {"url": "https://$D/services/$S/mcp", "bind": "0.0.0.0", "port": 18513,
     "oauth": {"issuer": "https://$D", "introspection_url": "https://$D/introspect",
               "client_id": "svc_FROM_SERVICE_ADD", "secret_file": "../credentials/mcp-client-secret"},
     "pond": {"url": "https://$D", "client_id": "svc_FROM_SERVICE_ADD", "secret_file": "../credentials/mcp-client-secret"},
     "access": [{"subject": "pond", "role": "reader"},
                {"subject": "garden", "role": "reader"}]}
    EOF
    sudo sh -c "umask 077 && boring-crocodile-admin service-add $S --url https://$D/services/$S/mcp --ponds pond,garden > /root/$S-config/service-add.txt" &&
    sudo python3 - /root/$S-config credentials/mcp-client-secret <<'PY' &&
    import json, os, pathlib, sys          # the secret into its file, the client id into the JSON files
    d, secret = pathlib.Path(sys.argv[1]), sys.argv[2]; os.umask(0o077); added = json.loads((d / "service-add.txt").read_text())
    (d / secret).parent.mkdir(parents=True, exist_ok=True); (d / secret).write_text(added["client_secret"])
    for f in d.rglob("*.json"): f.write_text(f.read_text().replace("svc_FROM_SERVICE_ADD", added["client_id"]))
    (d / "service-add.txt").unlink()
    PY
    sudo /root/releases/boring-$S-$V/deploy --install --config-dir /root/$S-config --index "$I"

#### Fishmarket

It has no host port: it joins the Docker network your proxy runs on (`N`
below, which must exist) and the proxy reaches it as `fishmarket:8000`.

    V=0.1.14 S=fishmarket D=crocodile-url.example.test N=edge
    f=boring-$S-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-services/releases/download/$S-v$V
    mkdir -p ~/Downloads && cd ~/Downloads &&
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&            # must print OK; if not, the block stops here
    sudo mkdir -p /root/releases && sudo tar xzf "$f" -C /root/releases &&
    sudo install -d -m 0700 /root/$S-config &&
    sudo tee /root/$S-config/config.json > /dev/null <<EOF &&
    {"url": "https://$D/fishmarket/mcp", "bind": "0.0.0.0", "port": 8000,
     "oauth": {"issuer": "https://$D", "introspection_url": "https://$D/introspect",
               "client_id": "svc_FROM_SERVICE_ADD", "secret_file": "../credentials/mcp-client-secret"},
     "pond": {"url": "https://$D", "client_id": "svc_FROM_SERVICE_ADD", "secret_file": "../credentials/mcp-client-secret"},
     "access": [{"subject": "pond", "role": "writer", "author": "pond"},
                {"subject": "garden", "role": "writer", "author": "garden"}]}
    EOF
    sudo sh -c "umask 077 && boring-crocodile-admin service-add $S --url https://$D/fishmarket/mcp --ponds pond,garden > /root/$S-config/service-add.txt" &&
    sudo python3 - /root/$S-config credentials/mcp-client-secret <<'PY' &&
    import json, os, pathlib, sys          # the secret into its file, the client id into the JSON files
    d, secret = pathlib.Path(sys.argv[1]), sys.argv[2]; os.umask(0o077); added = json.loads((d / "service-add.txt").read_text())
    (d / secret).parent.mkdir(parents=True, exist_ok=True); (d / secret).write_text(added["client_secret"])
    for f in d.rglob("*.json"): f.write_text(f.read_text().replace("svc_FROM_SERVICE_ADD", added["client_id"]))
    (d / "service-add.txt").unlink()
    PY
    sudo /root/releases/boring-$S-$V/deploy --install --config-dir /root/$S-config --edge-network "$N"

Add `--unsafe-url-auth` only if receipts should also carry `open_url`, a link with the token in it.

#### MCP Ferry

The ferry carries third-party MCP servers ("backends"). The block installs it
towards the Pond (without a Pond: [mcp-ferry/README, Without a Pond](mcp-ferry/README.md#without-a-pond))
with one backend, `B`, whose front end is
`https://crocodile-url.example.test/services/B/mcp` on port 18520; the
backend's own address, with any secret path, goes into
`credentials/B-url` (replace the example line). The backend itself is a
container you add to the ferry's `compose.yaml` afterwards, on a network only
the two share ([mcp-ferry/README](mcp-ferry/README.md); the template's comments
show one for Home Assistant), and the next `deploy` starts it. Until then the
ferry runs and the service answers `unavailable` to the Ponds; on the host,
`sudo docker compose logs ferry` in `/opt/boring-mcp-ferry` shows the
Crocodile's pings answered 503, and 200 once the backend runs.

    V=0.1.4 S=mcp-ferry D=crocodile-url.example.test B=homeassistant
    f=boring-$S-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-services/releases/download/$S-v$V
    mkdir -p ~/Downloads && cd ~/Downloads &&
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&            # must print OK; if not, the block stops here
    sudo mkdir -p /root/releases && sudo tar xzf "$f" -C /root/releases &&
    sudo install -d -m 0700 /root/$S-config &&
    sudo install -d -m 0700 /root/$S-config/services /root/$S-config/credentials &&
    sudo tee /root/$S-config/config.json > /dev/null <<EOF &&
    {"backends": {"$B": {"service": "services/$B.json", "upstream": {"url_file": "../credentials/$B-url"}}}}
    EOF
    sudo tee /root/$S-config/services/$B.json > /dev/null <<EOF &&
    {"url": "https://$D/services/$B/mcp", "bind": "172.16.46.2", "port": 18520,
     "oauth": {"issuer": "https://$D", "introspection_url": "https://$D/introspect",
               "client_id": "svc_FROM_SERVICE_ADD", "secret_file": "../../credentials/$B-client-secret"},
     "pond": {"url": "https://$D", "client_id": "svc_FROM_SERVICE_ADD", "secret_file": "../../credentials/$B-client-secret"},
     "access": [{"subject": "pond", "role": "reader"}, {"subject": "garden", "role": "reader"}]}
    EOF
    sudo sh -c "umask 077 && echo http://$B:8086/YOUR-SECRET-PATH > /root/$S-config/credentials/$B-url" &&
    sudo sh -c "umask 077 && boring-crocodile-admin service-add $B --url https://$D/services/$B/mcp --ponds pond,garden > /root/$S-config/service-add.txt" &&
    sudo python3 - /root/$S-config credentials/$B-client-secret <<'PY' &&
    import json, os, pathlib, sys          # the secret into its file, the client id into the JSON files
    d, secret = pathlib.Path(sys.argv[1]), sys.argv[2]; os.umask(0o077); added = json.loads((d / "service-add.txt").read_text())
    (d / secret).parent.mkdir(parents=True, exist_ok=True); (d / secret).write_text(added["client_secret"])
    for f in d.rglob("*.json"): f.write_text(f.read_text().replace("svc_FROM_SERVICE_ADD", added["client_id"]))
    (d / "service-add.txt").unlink()
    PY
    sudo /root/releases/boring-$S-$V/deploy --install --config-dir /root/$S-config

### First-install settings

| Service | First-install settings |
|---|---|
| all | `--config-dir DIR`: the service's configuration (`config.json`), the files it names and `credentials/` (the secrets) |
| Canvas | `--public-url https://DOMAIN`, `--web-bind ADDRESS` (default 127.0.0.1), `--unsafe-url-auth` |
| Fishmarket | `--edge-network NAME` (the Docker network the reverse proxy uses), `--unsafe-url-auth` |
| MCP Ferry | none beyond `--config-dir` (`config.json`, `services/`, `credentials/`); backends are blocks in its `compose.yaml` ([mcp-ferry/README](mcp-ferry/README.md)) |
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
`service_list` now shows the service.

### Updating a service

Read the release's entry in [CHANGELOG.md](CHANGELOG.md) first. Then the service's block, in one
shell: download, check, unpack, the deploy's read-only `--check`, and the
deploy, which runs only when the check passed. The versions are the tested set;
put in the one you update to.

Canvas:

    V=0.2.19 S=canvas
    f=boring-$S-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-services/releases/download/$S-v$V
    mkdir -p ~/Downloads && cd ~/Downloads &&
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&            # must print OK; if not, the block stops here
    sudo mkdir -p /root/releases && sudo tar xzf "$f" -C /root/releases &&
    sudo /root/releases/boring-$S-$V/deploy --check &&
    sudo /root/releases/boring-$S-$V/deploy

Knowledge:

    V=0.2.15 S=knowledge
    f=boring-$S-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-services/releases/download/$S-v$V
    mkdir -p ~/Downloads && cd ~/Downloads &&
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&            # must print OK; if not, the block stops here
    sudo mkdir -p /root/releases && sudo tar xzf "$f" -C /root/releases &&
    sudo /root/releases/boring-$S-$V/deploy --check &&
    sudo /root/releases/boring-$S-$V/deploy

Sourcecache:

    V=0.1.18 S=sourcecache-d365
    f=boring-$S-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-services/releases/download/$S-v$V
    mkdir -p ~/Downloads && cd ~/Downloads &&
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&            # must print OK; if not, the block stops here
    sudo mkdir -p /root/releases && sudo tar xzf "$f" -C /root/releases &&
    sudo /root/releases/boring-$S-$V/deploy --check &&
    sudo /root/releases/boring-$S-$V/deploy

Fishmarket:

    V=0.1.14 S=fishmarket
    f=boring-$S-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-services/releases/download/$S-v$V
    mkdir -p ~/Downloads && cd ~/Downloads &&
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&            # must print OK; if not, the block stops here
    sudo mkdir -p /root/releases && sudo tar xzf "$f" -C /root/releases &&
    sudo /root/releases/boring-$S-$V/deploy --check &&
    sudo /root/releases/boring-$S-$V/deploy

MCP Ferry:

    V=0.1.4 S=mcp-ferry
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
