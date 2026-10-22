# Changelog

Notable changes per release, newest first ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
[SemVer](https://semver.org/); tags `vX.Y.Z`, candidates `vX.Y.Z-rc.N`).

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
