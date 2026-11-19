# Changelog

Notable changes per release, newest first ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
[SemVer](https://semver.org/); each service is tagged on its own, `SERVICE-vX.Y.Z`, e.g. `canvas-v0.2.19`).

## Canvas 0.3.0 — 2026-11-19

### Upgrading

<a id="shared-linux-service-upgrade"></a>
**Shared Linux service upgrade for Canvas, Knowledge, Fishmarket, Sourcecache
and MCP Ferry.** These releases bundle Common 0.8.0 and pair with Crocodile
0.5.0. Choose one authentication mode, `crocodile`, `keys` or `issuer`.
Standalone issuers require approval secrets of at least 24 characters after
trimming whitespace and check them at startup and approval. See
[checking and generating a secret](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/CHANGELOG.md#upgrading).

For legacy Crocodile roles, the Services check compares service-local callers
with Crocodile membership. It lists callers present on only one side for
an explicit decision and proposes old roles only for callers present on
both sides. Resolve every difference before applying a complete grant map.
Review the preview, apply with `--apply` and typed `APPLY`, and verify the
intended reader and writer roles. See the
[family update guide](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/updating.md)
and [configuration reference](docs/CONFIGURATION.md).

Remove `bind`, `port` and `behind_proxy` from container JSON. Compose now
supplies container listeners. The deploy checks unpublished listeners on
the declared proxy network and published listeners on loopback.

**Canvas.** Route browser, sign-in and MCP traffic to the same listener.
The resource URL supplies the browser path prefix. Review saved Compose
and proxy configuration before the switch.

### Changes

- One app serves MCP and the Canvas editor. The editor distinguishes
  expired credentials and catches drawings pushed before its first poll.
- Crocodile installs take the service URL and caller roles and write the
  generated private credentials directly into the service HOME.

## Knowledge 0.3.0 — 2026-11-19

### Upgrading

Follow the [shared Linux service upgrade](#shared-linux-service-upgrade).
Verify that the container subnet can reach the Crocodile's HTTPS proxy.

### Changes

- The installer registers the selected caller roles, writes private
  credentials and checks the Crocodile connection before the switch.

## Fishmarket 0.2.0 — 2026-11-19

### Upgrading

Follow the [shared Linux service upgrade](#shared-linux-service-upgrade).
Keep the public proxy routes for MCP and browser transfers together.

### Changes

- Uploads distinguish rejected lengths, stale transfers and failures after
  streaming begins. Browser routes omit raw URL access logs.

## D365 Sourcecache 0.2.0 — 2026-11-19

### Upgrading

Follow the [shared Linux service upgrade](#shared-linux-service-upgrade).
Keep the source index and configure only reader grants.

### Changes

- The installer copies the supplied index from a checked path and registers
  the selected readers with Crocodile.

## MCP Ferry 0.2.0 — 2026-11-19

### Upgrading

Follow the [shared Linux service upgrade](#shared-linux-service-upgrade).
Review the Compose listener mapping for every selected frontend. For
Crocodile registration, provide the URL for each configured frontend.

### Changes

- Each frontend has its own credential and validated listener configuration.
  Tool discovery reports listing limits, timeouts and backend failures
  consistently.

## Boring Tunnel 0.1.0 — 2026-11-19

### Upgrading

Schedule the tunnel update with Crocodile 0.5.0. The default tunnel port
range is 18530–18539. Every forwarded port must be free and covered by the
Crocodile's rendered bind policy. Add custom ports to `server.protected_ports`
and apply the site render first. To change an installed tunnel's ports,
remove it and install it again with the new selection.

### Changes

- The optional [Boring Tunnel](tunnel/README.md) manages the Linux account,
  SSH drop-in and Crocodile service registrations. Preflight checks the
  selected Windows server names and explains missing bind protection.

## 2026-11-08: MCP Ferry 0.1.5, Fishmarket 0.1.15, Knowledge 0.2.16, D365 Sourcecache 0.1.19, Canvas 0.2.20

Every service is relocked on Pond Common 0.7.13 (`issuer` with `pond` refused by the kit; deploy
step lines flushed).

### MCP Ferry 0.1.5

- Gateway tools explain how to discover, call and collect a service result.
- MCP Ferry uses the effective call deadline for backend tool reads. Calls
  without a configured deadline have no fixed read timeout.
- A `guide_file` that cannot be read is left out with a warning: the gateway still starts (a
  renamed handbook page kept it, and every backend behind it, down).
- Built on Pond Common 0.7.13. Its own refusal of `issuer` with `pond` is gone: the kit refuses
  the pair for every service now.
- Reports its own version to backends again (it said 0.1.3 since 0.1.4).

### Fishmarket 0.1.15

- Tool descriptions explain how to deposit, share, collect and acknowledge a file.
- First install: `--loopback-port` also publishes it on the host's `127.0.0.1:18518`, for a
  reverse proxy on the host. Its first deploy then passes without editing `compose.yaml`
  (without it, nothing changes: no host port, the edge reaches it on its Docker network).

### Knowledge 0.2.16

- Tool descriptions and schemas explain notebook search, paging, confidence and revisions.

### D365 Sourcecache 0.1.19

- Tool descriptions and schemas explain source filters, line ranges and how to continue reads.

### Canvas 0.2.20

- Relocked on Pond Common 0.7.13; no Canvas change (a test keeps its published tool list within budget).

## 2026-11-01: MCP Ferry 0.1.4

### MCP Ferry 0.1.4

- Every front end is a kit front end: a backend's own front end, like a gateway, takes the
  Crocodile's tokens (`oauth`), its own sign-in (`issuer`) or access keys (`keys`). Without a Pond,
  the ferry and a few MCP servers serve a person's client (Claude on the web) directly; the README's
  "Without a Pond" has the install block, the proxy route and how to list and remove approved
  clients. A backend's own front end towards a Pond and a gateway with its own sign-in can carry
  the same backend at once.
- A front end with its own sign-in and `pond` registration is refused: a Pond calls only with the
  Crocodile's tokens. A gateway configured that way in 0.1.3 started, registered and refused every
  Pond call; it now refuses to start: drop `pond` (a person's client) or use `oauth` (a Pond).
- A backend's front end that does not register with a Pond no longer answers the heartbeat
  question (`server/discover`) with its backend's state: that answer comes before the token check.
- Without a Pond only a backend's `timeout_seconds` bounds a call (the README says so and the
  example sets it).
- No change for a ferry that serves a Pond: same configuration, state and deployment; upgrade and
  roll back as for 0.1.3.

## 2026-10-31: Canvas 0.2.19

Built on Pond Common 0.7.11, like the other services of 2026-10-30 (unchanged).

### Canvas 0.2.19

- The bundled editor's KaTeX (Mermaid's math labels) is 0.18.10 instead of 0.16.47, past the
  prototype-pollution advisory GHSA-238p-pmpm-9mq7 (fixed in 0.18.2). Mermaid 11.17.2 asks for
  KaTeX 0.16, so an override raises it; Mermaid 12 requires 0.18 itself. Rebuilt editor bundle;
  a browser check converts a Mermaid diagram with a math label under the page's CSP.
- No change to state, configuration or deployment: upgrade and roll back as for 0.2.18.

## 2026-10-30: Canvas 0.2.18, Knowledge 0.2.15, D365 Sourcecache 0.1.18, Fishmarket 0.1.14, MCP Ferry 0.1.3

Everything since 2026-10-22. All services build on Pond Common 0.7.11; own-issuer deployments
accept public-client token revocation without a secret field.

All services use Pond Common 0.7.12. Shared HTTP startup waits up to
20 seconds for an occupied listener during a restart and logs the wait.

### Canvas 0.2.18

- State lives in one SQLite database, `state/canvas.sqlite`, instead of one JSON file per canvas;
  every write, create, delete and expiry sweep is one transaction, for the server and
  `boring-canvas-admin` alike. The first start imports the existing canvas files once and leaves
  them unchanged; a file that is not a complete canvas, or two files claiming one name, stop the
  start with their names. Upgrade and rollback notes:
  [canvas/README.md](canvas/README.md#state-and-backup).
- Removed: the `boring-canvas` command and the web routes only it used. Pushing, reading back and
  new browser tokens are MCP tools and `boring-canvas-admin token`; the web port serves only the
  browser.
- Sign-in: old query links no longer sign in; `open_url` carries the token in the fragment.
- `get_elements` gives each element's box in the pushed SVG's own coordinates (`svg`) and the
  viewBox (`drawing`), or says why that mapping is unknown; `get_edits` pages back with `seq` and
  `before`. Coordinates are reported only for a placement saved for the current SVG.
- Editor dependencies: Excalidraw 0.18.1 (Mermaid 11, DOMPurify 3.4.17); nanoid and lodash-es
  raised past their upstream pins for published advisories.

### Knowledge 0.2.15

- Built on Pond Common 0.7.11; reports its package version.

### D365 Sourcecache 0.1.18

- The build stamps the D365 release from build arguments (`--application-version`,
  `--platform-version`, `--release-version`, `--version-evidence`); without them the versions say
  `unknown`. `source_info` names the `version_source`.
- `build --readers N` (default 16) reads files in parallel with one writer: the same snapshot for
  any N; a reader's failure publishes nothing.

### Fishmarket 0.1.14

- Built on Pond Common 0.7.11.

### MCP Ferry 0.1.3 (new)

- New service: carries MCP servers you did not write to a Crocodile's Ponds as ordinary
  services. Each backend gets its own front end with the Pond's tokens, grants, registration and
  heartbeat, and runs in its own container on an internal network only the ferry joins. Optional
  gateways offer four tools (`service_list`, `service_describe`, `service_read`, `service_call`)
  over several backends. See [mcp-ferry/README.md](mcp-ferry/README.md).
- Deploy: a stack may hold more containers than the service it builds; the smoke check covers every
  backend's endpoint and registration.

## 2026-10-22: Fishmarket 0.1.12

- Collection by token: `collection_receipt` also returns a token for that one
  upload, valid ten minutes from issue (never past the transfer), for any number
  of reads and resumes. `GET .../api/ID/collect` accepts it as
  `X-Fishmarket-Token`, so an agent inside a Pond, which never holds the service
  bearer, can collect uploads; before, it got 403. A new receipt replaces the
  token; acknowledging or revoking ends it. The helper's bearer route is
  unchanged. Tokens live in their own table; existing state needs no migration.

## 2026-10-21: Canvas 0.2.12, Knowledge 0.2.13, Fishmarket 0.1.11, D365 Sourcecache 0.1.15

First public release of Canvas, Knowledge, Fishmarket and D365 Sourcecache, independent MCP services for Linux and Windows.
