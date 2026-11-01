# Changelog

Notable changes per release, newest first ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
[SemVer](https://semver.org/); each service is tagged on its own, `SERVICE-vX.Y.Z`, e.g. `canvas-v0.2.19`).

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
