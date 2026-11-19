# Configuring a service

Canvas, Knowledge, Fishmarket and D365 Sourcecache read `--config FILE`
through boring-service-kit's `ServiceConfig`; relative paths resolve beside it.
Ferry reads its routing configuration and one `ServiceConfig` file per selected
frontend.
This page owns the shared settings; each service's README adds its own
options. Configuration-file changes take effect on a service restart.
Crocodile grant and role changes take effect within the token cache (at most
60 seconds), without editing service files or restarting the service. For a Linux artifact,
find its HOME in `/etc/boring-pond/homes.json` and edit
`HOME/config/config.json`; a site may choose a different HOME from the
release's `/opt/boring-SERVICE` default. Deploying the same image again
can leave the running container on its earlier configuration.
A Crocodile service grant is the owner of its callers and their roles.
Linux installers take --url/--callers and generate this configuration without
an input config directory. Ferry additionally needs its routing config;
keys/own-issuer installs take --config-dir explicitly.

Before switching, a Linux deploy validates the configuration named by
`--config` in the Compose command using the candidate image, with the
service's account and mounts. Ferry validates its routing configuration and
every selected backend or gateway frontend, including its Compose-owned
listener settings and unique port, URL and Crocodile client ID. Startup uses
these same checks. Invalid configuration stops the check before the authority reachability probe. Validation does not open or
initialize issuer databases, test their contents or prove that the server
will start. It does not restart a service to apply a configuration change.

## Where tokens come from

The configuration selects exactly one `mode`, with only that mode's block.
Examples in [../examples](../examples):

| Example | Mode | Role owner |
|---|---|---|
| `standalone.json` | `issuer` | the issuer's single `role` for approved clients |
| `family.json`, `canvas-family.json` | `crocodile` | the Crocodile service grant |
| `keys.json` | `keys` | each static access key |

Modes cannot be combined. The former `oauth`, `pond` and `access` blocks
are refused with their replacements named. A Crocodile configuration has one
`crocodile` object containing `url`, `client_id` and `secret_file`;
registration and introspection use that same authority. Generic external
issuer introspection and key/OAuth fallback have been removed.

**Its own issuer.** The service serves `/authorize`, `/token`, `/revoke`,
`/approve` and the authorization-server metadata at the root of its URL.
Clients presenting a Client ID Metadata Document on an allowed host can start
with the URL alone (`metadata_document_hosts`, default `claude.ai` and
`chatgpt.com`). A client may instead use an administrator-issued ID
(`boring-service-oauth --db STORE client-add --name ... --redirect-uri ...`).
The family's [client guide](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/clients.md)
shows registration and the VS Code JSON, and distinguishes vendor versions.
Open dynamic registration is
off (`"dynamic_registration": true` turns it on). Each sign-in ends on the
approval page, which shows where the code goes and asks for the approval
secret. Open that page from the client you are connecting. Five wrong tries
cancel that sign-in. The secret's length protects it: use at least 24
characters and generate it with `openssl rand -hex 32`. The store's
directory must be private (0700) to the service account; the store file is
created 0600.
Behind a proxy that serves the service under a path, set `issuer.url` to that
path and route the issuer's paths too.

**Crocodile mode.** The Crocodile owns the caller grant and its `reader`
or `writer` role; writer includes reader. The service reads the role from
authenticated introspection. It keeps no local caller list. Introspection
uses HTTP Basic resource-client credentials and checks active, future exp,
exact aud (string/list), nonempty sub, role and issuer if returned. Missing
iss is accepted only as an authenticated response from the trusted Crocodile.
Positive verification caches at most 60 seconds; revocation and role changes
can take that long. Failed introspection is an infrastructure error.
A service gets its `svc_` id and secret from
`/opt/boring-crocodile/current/bin/boring-crocodile-admin service-add NAME --url URL --roles reader,writer --default-role reader --callers NAME=ROLE`.
Its Crocodile mode also enables registration and heartbeat; tools always
require a token. Never forward public Pond tokens downstream.

**Identity and access.** The author/owner is the stable JSON pair [issuer,
subject], verified by the selected mode. Author aliases and local access lists
are removed. Identity never comes from arguments or OAuth client_id. All
Knowledge writers may revise any entry with expected_version; history remains
append-only. Canvas retains owner isolation even between writers. Global and
premise notebooks remain separate; no sync.
A Pirate's service grant is also owned by the Crocodile:
[Let a Pirate call a service](https://github.com/the-boring-crocodile/boring-crocodile/blob/main/docs/SERVICES_AND_PIRATES.md#let-a-pirate-call-a-service).

## Launch and HTTPS

With the Pond family's [example site](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/example-site.md):

    boring-knowledge http --config config.json --db notebook.db --role global
    boring-sourcecache-d365 serve --config source.json --db snapshot-v1.db
    boring-canvas-server --config canvas.json --state-dir canvas-state
    boring-fishmarket --config config.json --state-dir state

Give each service its own MCP port and URL. A trusted reverse proxy terminates
HTTPS and routes `/mcp` and the SDK's `/.well-known` paths to the service,
keeping the Host and Authorization headers (with its own issuer, also
`/authorize`, `/token`, `/revoke`, `/approve` and authorization-server metadata;
route `/register` as well when dynamic registration is enabled). That includes
the URL's own metadata path (for `https://crocodile-url.example.test/services/canvas/mcp`:
`/.well-known/oauth-protected-resource/services/canvas/mcp`): the deploy's
smoke checks that the MCP URL answers 401 naming it, and that it names the
URL as its `resource`. Canvas's browser routes use the MCP URL's base plus
`/canvas`, on the same listener (for `/services/canvas/mcp`, route
`/services/canvas/canvas/*` there too). Service images declare container mode.
Compose owns their listeners through `BORING_SERVICE_LISTENERS`, keyed by
absolute config path. Container JSON carries URL and authentication only:
`bind`, `port` and `behind_proxy` are refused with the Compose replacement named.
The deploy checks actual host publications: loopback, or no publications and
the Compose label `boring.proxy-network` naming an attached network. A JSON
proxy assertion cannot permit public ports. Ferry assigns frontend addresses
from the template's normalized published target ports and fixed default-network
address; its frontend URLs and identities still come from their service configs.
The resulting Compose environment serves both startup and read-only checks.

Native runs retain their JSON bind/port settings and loopback guard. A native
non-loopback listener requires explicit `behind_proxy=true` and network access
restricted to a trusted proxy; the flag adds neither TLS nor firewall rules.
An HTTPS public URL does not add TLS to a listener. Plain HTTP public URLs
remain limited to loopback fixtures. Browser and MCP stop together.

Native state defaults follow the kit (Windows `LOCALAPPDATA`, Linux
`XDG_STATE_HOME`); pass `--state-dir` explicitly on servers.

## Native installation

Use this route for Windows or when you want the host to run a service without
Docker. You own its process startup and restart. For an integrated Windows
installation, [pondNT setup](https://github.com/the-boring-crocodile/boring-pondnt/blob/main/docs/INSTALL.md)
bundles the services and manages that wiring.

Build from the Services repository with `uv` and a directory of Pond Common
release wheels matching its locks. The default target Python is 3.14. Choose
the target you will install on; these commands build files, not running
services:

    python scripts/build.py --target linux --common-wheels /absolute/common-dist --out /absolute/new-release-linux
    python scripts/build.py --target windows --common-wheels /absolute/common-dist --out /absolute/new-release-windows

Each output has a top-level `MANIFEST.json` (source commit, wheel and lock
hashes) and one folder per service: `knowledge/`, `canvas/`,
`sourcecache-d365/`, `fishmarket/` and `mcp-ferry/`. Each service folder holds
its wheels and `requirements.txt`. `--service knowledge` builds just that
service. Keeping each service's lock separate lets you install or update one
without replacing another service's environment.

On the target, install the selected folder into its own venv. For Knowledge
on Linux, with Python 3.14 available:

    uv venv /absolute/knowledge-venv --python 3.14
    uv pip sync --python /absolute/knowledge-venv/bin/python --require-hashes \
        --find-links /absolute/new-release-linux/knowledge /absolute/new-release-linux/knowledge/requirements.txt

On Windows, use `C:\knowledge-venv\Scripts\python.exe` and the corresponding
Windows output's `knowledge` folder. Launch the installed command with its
configuration as shown [above](#launch-and-https), then check it from the
intended MCP client. Building or installing the wheels alone does not prove
that authentication and proxy routing work.

Services may use separate accounts as well as venvs. Never overwrite a release
of the same name: build a new version. pondNT's build takes `--services-root`
and bundles the service wheels.

A Windows build made on Linux proves nothing about running on Windows: test
the build on Windows before relying on it.
