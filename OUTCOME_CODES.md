# Service outcome causes

Local producers use the closed shared [Common cause vocabulary](https://github.com/the-boring-crocodile/boring-pond-common/tree/main/packages/boring-outcomes)
plus the component additions below. A cause is independent of status: refused
means nothing started or changed; failed means work started and may have effects.
Valid received outcomes retain their original cause and details.

The declarations and reviewed dynamic exceptions live in
[the source test](tests/test_local_cause_codes.py). It scans src for literal
codes in Refused/Failed calls. A new local cause needs a meaning and must not
be a synonym of a shared cause.

Canvas, Knowledge, Sourcecache and Ferry add no local causes. Fishmarket owns:

| Extra cause | Meaning |
|---|---|
| range | an unsupported or unsatisfiable HTTP byte range (HTTP 416) |
| checksum | a deposit does not match its declared size or SHA-256; no publication |
| storage | upload publication could not be made durable |
| upload | an unexpected upload interruption; inspect transfer state |

An upload that is no longer active uses failed/stale; transfer history still
uses expired as a lifecycle state. Length bounds use limit. These outcomes
preserve publication/effect status. Ferry configuration refusal is invalid_request;
its transport/protocol mapping uses unavailable/upstream_error, with raw backend
details retained. Backend tool outcomes are forwarded unchanged.
