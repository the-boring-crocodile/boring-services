# Changelog

Notable changes per release, newest first ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
[SemVer](https://semver.org/); tags `vX.Y.Z`, candidates `vX.Y.Z-rc.N`).

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
