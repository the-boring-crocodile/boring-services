# boring-mcp-ferry

Carries MCP servers you did not write to a Crocodile's Ponds, as ordinary
services: the Pond's tokens in front, the server on a private network behind.

Most MCP servers use MCP's authorization not at all. They run over stdio, or
over HTTP with no authentication or a static secret. That suits one person on
one machine. On a shared host, any local process could use such a server. The
ferry gives each one what the Pond expects from a service:

- every call carries a token the Crocodile minted for that Pond and that
  service, checked by introspection, and `access` decides who may call;
- the Crocodile's grants decide which Ponds see it;
- registration and the heartbeat (the service shows as unavailable while its
  server is down).

Behind the ferry the server stays as its authors made it, in the setting it was
made for: a container on an internal network that only the ferry joins.

## What crosses

Only `tools/list` and `tools/call`; that is all a Pond's worker asks for.

- The server's tools are listed as it states them, minus any the ferry's
  `tools` filter leaves out.
- A tool the server marks read-only needs the caller's `reader` role, any
  other tool `writer`. The marks are the server's own hints: configure only
  servers whose marks you accept.
- The server's answer, its own errors included, is passed on unchanged.
- One deadline per call (the backend's `timeout_seconds`, at most the Pond's
  call time), counted from the request's arrival, covers checking the tool list
  and the call. The tool list is cached for a minute and fetched once at a time.
- Server unreachable, or its tool list not to be had: refused `unavailable`,
  nothing was sent. No time left before sending: refused `limit`. No answer in
  time, a broken connection or a protocol error: failed, the call may have run.
  Nothing is retried. A new or removed tool shows within a minute.
- Its texts (descriptions, answers) are the server's, not ours: data to check.

## Configuration

One ferry carries several backends. Each backend is one Pond service with its
own front end: `config/services/NAME.json`, a boring-service-kit configuration
like every service's (its URL and port, `oauth` with the Crocodile, `access`,
`pond`). `config/config.json` lists the backends:

```json
{"backends": {
  "homeassistant": {
    "service": "services/homeassistant.json",
    "upstream": {"url_file": "../credentials/homeassistant-url"},
    "tools": {"deny": ["ha_restart"]}
  }
}}
```

| Key | What |
|---|---|
| `service` | the backend's front end (kit configuration); its port one of 18520-18529, `bind` the ferry's own address (`172.16.46.2` in the template; a wildcard bind is refused), so its `url` is the `https` one the reverse proxy serves (the kit takes plain `http` only on a loopback bind) |
| `upstream.url` / `upstream.url_file` | where the ferry reaches the backend; `url_file` when the URL is itself a secret (a secret path) |
| `upstream.headers_file` | optional JSON object of HTTP headers, for a server with a static secret |
| `upstream.timeout_seconds` | optional; at most the Pond's call time, which is the default |
| `tools.allow` / `tools.deny` | optional; a tool left out is neither listed nor callable |

Relative paths resolve next to the file naming them. Unknown keys are refused,
so a typo cannot drop a restriction silently. Two backends with the same port,
URL or client_id are refused: each is its own service.

## Gateways (optional)

As you connect more services, the client's tool list grows: source search,
drawings, browser actions, builds and warehouse tools all compete for attention.
A gateway gives the agent a smaller starting point. It sees four tools, asks
which services are available, and reads the relevant backend's tool descriptions
when it needs them. You can keep several services available without presenting
all their tools up front.

Tool definitions also consume context tokens: names, descriptions and input
schemas are text the model receives. When a client sends the whole tool list
up front, a larger list adds input tokens even before a tool is called.
Ferry can reduce that initial overhead by exposing four tools and loading a
backend's descriptions when requested. This can reduce token-based cost;
actual savings depend on the client's own tool discovery, prompt caching
and how many backends the task uses. Discovery calls and their results also
consume tokens. [Anthropic's tool-use documentation](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview#pricing)
explains how tool definitions contribute to API usage; a Copilot subscription
need not bill that usage in the same way.

The trade-off is an extra discovery step. The model must choose a service,
inspect its tools and then call one through the gateway. Direct connections
are simpler when you use only a few tools or your client handles their full
list well. A smaller initial list does not by itself prove lower total token
use or better answers; that depends on the client, model and task.

For example, pondNT can keep core and Knowledge directly available while
putting source search, Canvas and D365 tools behind Ferry. The agent can start
with its workspace and notes, then discover specialist tools as needed.

A gateway is one front end over the selected backends. Configure those
backends and name which ones it carries:

```json
{"backends": {
   "d365": {"upstream": {"url": "http://127.0.0.1:18514/mcp", "headers_file": "d365.headers.json"},
            "guide_file": "../kit/d365.md"}},
 "gateways": {"pondnt-services": {"service": "services/gateway.json", "backends": ["d365"]}}}
```

| Tool | Role | What |
|---|---|---|
| `service_list` | reader | the backends, and whether each answers now |
| `service_describe(service)` | reader | its tools (name, description, input schema, read_only), its MCP instructions, its `guide_file` |
| `service_read(service, tool, arguments)` | reader | a tool the backend marks read-only; refused otherwise |
| `service_call(service, tool, arguments)` | the tool's | any offered tool; reader for read-only tools, writer otherwise |

- Calls follow the passthrough contract above (filter, role, one deadline, answers and errors unchanged,
  `status: running` included, nothing retried).
- Nested `arguments` may be omitted to use an empty object. When supplied, it must be an object;
  null, arrays and other values are refused before the backend is called.
- Linux deployment checks enumerate backend frontends and gateways from the runtime configuration.
  Reach checks cover their authority URLs; smoke checks each frontend endpoint and any configured
  Pond registration. Backend availability remains the heartbeat's responsibility.
- A backend needs `service` (its own front end) only when it is not behind a gateway.
- A gateway's `service` may use access keys only (`keys`, loopback bind): Copilot on a dev box. A backend's
  own front end still needs the Crocodile (`oauth`).

## Installing

Follow [Installing on Linux](../README.md#installing-on-linux), with
`--config-dir` holding `config.json`, `services/` and `credentials/`. The stack
is the ferry plus one container per backend; the template's comments show a
backend block. For each backend:

1. Its container in `compose.yaml`, image pinned by digest, on its own network
   with `internal: true`, which the ferry joins; nothing published;
   `cap_drop: [ALL]` (without `NET_RAW` it cannot craft packets to the ferry's
   own address on that network: the bind alone keeps out ordinary traffic only).
2. Its entry in `config.json` and its front end in `services/NAME.json`.
3. `sudo boring-crocodile-admin service-add NAME --url <front end URL> --ponds ...`,
   its client secret into `credentials/`.
4. Its own secrets (an API token, the URL with a secret path) into
   `credentials/`, root's and the service account's only.

The reverse proxy routes each front end's path and its metadata path to its
loopback port, as for any service. The ferry's container must reach the
Crocodile's public URL (introspection, registration): its network is
`172.16.46.0/24`.

## Example: ha-mcp for a Home Assistant

ha-mcp ([homeassistant-ai/ha-mcp](https://github.com/homeassistant-ai/ha-mcp))
in its HTTP mode, `ha-mcp-web`, checks nothing but its URL path. Behind the
ferry:

- its container joins the ferry's internal network and `hass-mcp`, an internal
  network that Home Assistant's stack joins as well, nothing else;
- `HOMEASSISTANT_TOKEN` belongs to a dedicated Home Assistant user without
  admin rights: Home Assistant itself is the hard limit;
- `READ_ONLY_MODE=true` to start; `HA_MCP_DISABLE_SETTINGS_UI=true` (that page
  could switch read-only mode off); `HA_MCP_DISABLE_UPDATE_CHECK=true` (it has
  no way out); a random `MCP_SECRET_PATH` anyway;
- the ferry's `upstream.url_file` holds `http://homeassistant:8086/<secret path>`.

## Limits

- One process carries every backend. A front end that cannot listen (its port
  taken) stops the ferry, so every backend; a backend that is down affects only
  its own service.
- No answer size limit of its own beyond the tool list's (at most 1000 tools):
  the container's `mem_limit` bounds what a backend can make it hold.
- Read-only marks are the backend's hints (see What crosses).
- The deploy's checks treat the stack as one: `deploy` runs `compose up -d`
  on all of it and waits for every container; a backend that will not start
  fails the deploy of all of them.
- The heartbeat is answered while the backend answers a cheap check (cached for
  one ping interval), not after a real tool call.

## Development

`python scripts/check.py --common-wheels DIR` (repository root) runs its tests
with the others.
