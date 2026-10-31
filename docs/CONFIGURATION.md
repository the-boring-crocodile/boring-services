# Configuring a service

Every service starts from one JSON file, `--config FILE` (boring-service-kit's
`ServiceConfig`); relative paths in it resolve beside it. This page is the
reference for all four services; each service's README adds only its own
options.

## Where tokens come from

The configuration names one or two sources; examples in [../examples](../examples):

| Example | Tokens come from | Use |
|---|---|---|
| `standalone.json` (`issuer`) | the service itself | a lone server for Claude, ChatGPT, VS Code |
| `family.json`, `canvas-family.json` (`oauth` + `pond`) | the Crocodile, by introspection | a Pond family (Knowledge, Canvas) |
| `other-issuer.json` (`oauth`) | another issuer with RFC 7662 introspection | Keycloak, Entra |
| `keys.json` (`keys`) | static access keys (`boring-service-keys`) | scripts, local clients |

`keys` combines with `issuer` or `oauth`; `issuer` and `oauth` exclude each other.

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
secret; five wrong secrets cancel that request, never the next. The store's
directory must be private (0700) to the service account; the store file is
created 0600.
Behind a proxy that serves the service under a path, set `issuer.url` to that
path and route the issuer's paths too.

**An external issuer** (the Crocodile included). Use one when an existing
authority should decide who the caller is, so each service does not need its
own sign-in process. The service still decides what that caller may do through
its `access` list. It must be able to reach the issuer to verify tokens.

Introspection uses HTTP Basic
resource-client credentials; checks active, future exp, exact aud
(string/list), nonempty sub, issuer if returned, and configured scopes. Missing
iss is accepted only as an authenticated response from the explicitly trusted
endpoint. Positive verification caches at most 60 seconds; revocation can take
that long. Failed introspection is an infrastructure error, not a bad-token
refusal. A family service gets its `svc_` id and secret from
`boring-crocodile-admin service-add`; the optional `pond` section adds the
registration and heartbeat (the modern `server/discover` then passes without a
token; tools always require one). Never forward public Pond tokens downstream.

**Access.** `access` lists the subjects the issuer vouches for, each `reader` or
`writer`; empty access denies everyone. Writer includes reader. Default
author/owner is the stable JSON pair [issuer, subject]; a grant's unique
`author` names it instead (`"author": "pond"`), so authorship and Canvas
ownership survive a change of issuer. Identity
never comes from arguments or OAuth client_id. All Knowledge writers may
revise any entry with expected_version; history remains append-only. Canvas
retains owner isolation even between writers. Global and premise notebooks
remain separate; no sync.

## Launch and HTTPS

With the Pond family's [example site](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/example-site.md):

    boring-knowledge http --config config.json --db notebook.db --role global
    boring-sourcecache-d365 serve --config source.json --db snapshot-v1.db
    boring-canvas-server --config canvas.json --state-dir canvas-state --host 127.0.0.1 --port 18510 --public-base-url https://crocodile-url.example.test
    boring-fishmarket --config config.json --state-dir state

Give each service its own MCP port and URL. A trusted reverse proxy terminates
HTTPS and routes `/mcp` and the SDK's `/.well-known` paths to the service,
keeping the Host and Authorization headers (with its own issuer, also
`/authorize`, `/token`, `/revoke`, `/approve` and authorization-server metadata;
route `/register` as well when dynamic registration is enabled). That includes
the URL's own metadata path (for `https://crocodile-url.example.test/services/canvas/mcp`:
`/.well-known/oauth-protected-resource/services/canvas/mcp`): the deploy's
smoke checks that the MCP URL answers 401 naming it, and that it names the
URL as its `resource`. Canvas's `/canvas` browser routes go to its
separate web listener. Listeners bind loopback by default; in a container,
bind 0.0.0.0 inside and publish only to the proxy. Plain HTTP URLs are for
loopback only. Browser and MCP stop together.

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
