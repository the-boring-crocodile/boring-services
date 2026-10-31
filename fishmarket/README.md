# Boring Fishmarket

Temporary one-file handovers between an agent and a person's browser: the
agent puts a file in and gives the person a link, or gives the person an
upload link and collects what they put in.

## Why

The agent's workspace and the person's browser may be on different machines.
A chat attachment does not necessarily reach the machine where the file is
needed, and sending megabytes through tool results is wasteful. Files still
have to move: a release bundle to a Windows machine
that cannot reach the Pond's host, a log or an export from a person's laptop
into a Pond, a zip of results back. Fishmarket is the market stall between
them: one file, one link, verified on both ends, gone when done. And as at
any good fish stall, there is a strict no-returns policy on fish.

## How it works

The MCP tools carry only receipts; the bytes use separate HTTP endpoints
(HTTPS at the public URL), streamed by a
small helper (`boring-fishmarket-transfer`) that reads and writes its own
local files and never asks the server to read a path or fetch a URL.

- **Agent to person:** the helper deposits the file with its exact size and
  SHA-256; the agent gets a link and, separately, a token for the person. The
  person opens the link and presses Download; that browser then has ten
  minutes (retries and resumes included), and no second browser can claim
  it. Then the file is gone.
- **Person to agent:** the agent asks for an upload link (with a size limit)
  and gives it to the person; the upload counts only once every byte arrived.
  The agent collects the file, checks size and SHA-256, publishes it without
  overwriting anything, and only then acknowledges, which deletes it on the
  server. An agent inside a Pond, which never holds the service bearer,
  collects with the token from `collection_receipt`: one upload, ten minutes
  from issue (never past the transfer), any number of reads and resumes; a new
  receipt replaces it, acknowledging ends it.
- Every transfer expires (24 hours by default), and its owner can revoke it at
  any time. One process with one private state folder; SQLite keeps the
  bookkeeping.

## Details

Runs independently on Linux/Windows; no Pond required. Service-kit supplies
OAuth/access-key verification and reader/writer permissions. No file bytes in MCP.

## Use

Choose [agent-to-person or person-to-agent](#how-it-works), then use the helper
below. [MCP tools](#mcp-tools) lists permissions; [Lifecycle and limits](#lifecycle-and-limits)
explains expiry and recovery. Fishmarket is a handover, not an archive: keep the
source file until the recipient has confirmed it arrived.

Install: [README](../README.md#installing-on-linux) (`--edge-network`,
`--unsafe-url-auth`), or natively ([CONFIGURATION](../docs/CONFIGURATION.md)),
then `boring-fishmarket --config config.json --state-dir state`. Its public
URL is the browser base plus `/mcp`; HTTPS, or HTTP on loopback for tests.

The helper streams from/to its own local filesystem, never asks the server to
read local paths or fetch arbitrary URLs. Put the service bearer/access key in
a private token file, then (with the Pond family's
[example site](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/example-site.md)):

```text
boring-fishmarket-transfer --url https://crocodile-url.example.test/fishmarket/mcp --token-file token.txt deposit file.zip
boring-fishmarket-transfer --url https://crocodile-url.example.test/fishmarket/mcp --token-file token.txt upload-link --max-bytes 104857600
boring-fishmarket-transfer --url https://crocodile-url.example.test/fishmarket/mcp --token-file token.txt ready
boring-fishmarket-transfer --url https://crocodile-url.example.test/fishmarket/mcp --token-file token.txt collect TRANSFER_ID received.zip
boring-fishmarket-transfer --url https://crocodile-url.example.test/fishmarket/mcp --token-file token.txt ack TRANSFER_ID received.zip
```

Without the helper, as an agent inside a Pond: call `collection_receipt`, then
fetch `download_url` with its token into a new destination, compare the local
size and SHA-256 with the receipt, and only then acknowledge through MCP.
For example, in a new empty working directory:

```text
curl -fL -H "X-Fishmarket-Token: TOKEN" -o received.zip DOWNLOAD_URL
wc -c < received.zip
sha256sum received.zip
```

Give the receipt's clean `url` plus the separate `token` to the intended person.
With `--unsafe-url-auth` (opt-in, as Canvas) receipts also carry `open_url`, the
link with the token in its fragment; anyone holding it before claim can win. Do not put either
capability in notes or logs. ZIP folders yourself; nothing is unpacked here.

Upload and download links open separate pages showing only the relevant action.
The page direction is carried by /upload/ID or /download/ID; rendering never
looks up private transfer state, even for unknown IDs.

Download needs an explicit button. GET/HEAD previews do not claim. First claim
binds to that browser's cookie; same-browser retry/range access lasts exactly
ten minutes. The original token cannot admit a second browser. At the deadline
access stops and payload/session are removed, including stalled readers.
This window is not proof that the browser saved the file.

Upload completes only after the entire declared body arrives. Its capability
is then consumed. A ready inbound file stays until its creator or an explicitly
granted collector saves, verifies and acknowledges it. Collection uses a local
temporary file (fsynced), SHA256/size verification and atomic hard-link
publication without overwrite; on Linux the destination directory is then
fsynced, so the new name survives a crash, before the ack lets the service
delete its copy. If that flush fails nothing is acknowledged and the remote
copy stays (`AcknowledgementWithheld`); `ack` flushes the same way. The
service likewise fsyncs its payload directory after renaming an upload into
place and before marking it ready. A lost ack leaves the local file published:
retry `ack` with that same ID/file. Filesystems without hard-link support
refuse publication and retain the remote file. On Windows Python cannot flush
a directory: collection there acknowledges after the NTFS link without a
flush and reports `directory_flushed: false`, claiming no crash guarantee for
the new name. Native Windows behavior still needs acceptance.

## MCP tools

| Tool | Permission and purpose |
|---|---|
| create_transfer | Writer; upload slot or exact-size/digest outbound deposit |
| open_download | Owner writer; issue/rotate a ready outbound link |
| transfer_status | Reader; owned or explicitly granted transfer |
| list_transfers | Reader; up to 100 owned receipts, recovering lost responses |
| list_ready | Reader; up to 100 inbound files the caller may collect |
| collection_receipt | Reader; authorized byte endpoint and verification receipt |
| acknowledge | Authorized reader; verified inbound collection, idempotent retry |
| grant_collector | Owner writer; one exact verified author, grant/revoke |
| revoke_transfer | Owner writer; invalidate and remove transfer |

`acknowledge` needs reader access only, and only the transfer's creator or a
granted collector may call it: collecting ends with the acknowledgement, so a
collector with reader access can finish its own collection; the tool is still
marked as a write because it deletes the service's copy.

Verified author is normally JSON `[issuer,subject]`, never an ownership argument.
A grant does not enroll someone into service access. Lists are bounded to 100.
Owner recovery lists show active work first, then terminal history, oldest first
within each group. Ready lists are oldest first. No pagination in this milestone. Revoke abandoned pending
slots after lost creation receipts; their original upload tokens are not recovered
from hashes. Ready download links can be rotated. Discovery carries no private
readiness: existing Pond heartbeat is unauthenticated availability only, and
does not wake a model. Poll authenticated `list_ready` when appropriate.

## Lifecycle and limits

Defaults: 24h from creation, 2 GiB per file, 10 GiB total live/reserved bytes.
Upload slots reserve their maximum before receiving bytes; completed files use
actual size. Empty files work. Claims replace the original deadline with their
fixed ten-minute window. Upload requests also have a fixed ten-minute deadline
bounded by original transfer expiry. Status/polling/retries never extend expiry.

One ASGI process, private SQLite metadata and opaque-ID payloads. Process lock
rejects a second writer. Startup removes interrupted/unpublished partials and
resets interrupted upload slots for retry; it fails visibly for missing published
files. In-process sweep every five seconds, plus operation/startup checks. Failed
deletion retains quota and logs a safe error category; retry or operator inspection
is needed. Terminal receipts/grants remain until original expiry + 24h for ack
retry, then are pruned. Runtime state is temporary and must be excluded from backup.

Tokens are hashed at rest. Browser links carry fragments; JS clears them before
an action. Cookies are HttpOnly/SameSite Strict, prefix scoped and Secure on HTTPS.
The fragment parameter is named `this-is-unsafe-dont-do-this-auth`, matching
Canvas's explicit warning. Existing `#token=` links remain readable by the
browser page during this change, but new links use the warning name.
Origin checks protect mutations; no CORS. Downloads are attachments; filename is
display-only. No access logs, tool argument logs, uploaded HTML previews or extraction.
Payload contents, display metadata and hashes remain private runtime state.

