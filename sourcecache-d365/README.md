# boring-sourcecache-d365

D365 source snapshots as an MCP service.

## Why

Developing for Dynamics 365 Finance and Supply Chain means reading the
standard code all the time: which table has this field, what this class
does, which label says that. The standard source is hundreds of thousands of
XML files in `PackagesLocalDirectory`; searching it with ordinary tools takes
minutes or worse (on one development box, reading the whole source took 82
minutes with the virus scanner checking every file, 5 without), and it exists only on
a machine with D365 installed. Sourcecache indexes it once and answers in
moments: Copilot on the development box gets it as a tool, and an agent in a
Linux Pond can look up standard code without any D365 installation.

## Where the source comes from

Not from here. Sourcecache ships no Microsoft code and no index built from
it. You bring your own: usually the `PackagesLocalDirectory` of a D365
development environment you already work on. What you may do with that
source -- index it, and let excerpts reach the AI service your client uses
-- is decided by the terms you received it under, not by this project.

## How it works

An administrator builds a **snapshot** from a source tree: one SQLite file
with a trigram full-text index over the elements, their XML, the labels (in
the languages chosen) and the models from their descriptors (package, layer,
publisher, version). A snapshot is immutable: a new D365 version gets a new
file, and several can be served side by side; every answer names the
snapshot it came from. The service serves them read-only with four tools:
`source_info` (what is indexed), `source_search` (text across elements,
narrowed by package or model), `source_element` (one element's source) and
`label_lookup` (a label by id or text). Standard and custom code are told
apart by the model's publisher. The index never holds more than the tree it
was built from; customer code goes in only if the administrator builds from a
tree that contains it, and the snapshot stays where it was built.

## Details

Build an explicitly supplied source tree into a new SQLite snapshot; serve it
without a D365 installation. Index writes remain local admin CLI operations.
Both native targets share one package. The 24 MiB per-file limit is large
enough for the biggest standard report; files above it are counted as
skipped in the build receipt.

Start: `boring-sourcecache-d365 serve --config config.json --db snapshot.db`
(`--help` lists options). Configuration: [CONFIGURATION](../docs/CONFIGURATION.md);
install (`--index FILE`): [README](../README.md#installing-on-linux).

## Building a snapshot

Building is a local administrator operation, never an MCP tool:

    boring-sourcecache-d365 build --source PackagesLocalDirectory --db snapshot-v2.db --name application-v2

A snapshot gets a UUID `snapshot_id` and a receipt (name, application and
platform versions, build time); names are unique. The build writes to a
unique staging file and publishes with an atomic no-overwrite hard link, so an
interrupted build never activates a partial snapshot (the filesystem must
support hard links). Never replace an open index: build a new file, check it,
then restart the service with its path. Older snapshots stay readable.
Before serving a new one, check it: SQLite's `PRAGMA quick_check`, the
counts in its receipt, one identifier search and one label lookup.

## Model provenance and scope

Descriptor package/model keys match folder keys ignoring ASCII case, preserving
original path spelling. Ambiguous descriptor keys are refused. Microsoft and
Microsoft Corporation publishers are standard; other nonblank publishers are
custom. Missing descriptor/publisher provenance is unknown and remains visible
in scope=all, excluded from standard/custom. Search and element results expose
scope alongside layer. Existing immutable snapshots need no rewrite: readers
index the small descriptor table in temporary connection-local storage.

## Query deadlines

HTTP query execution is capped at 28 seconds (or a smaller positive
`--stall-seconds`). The Pond shared service client allows 30 seconds including
authentication/transport; its controller waits another two seconds for the worker
result. Earlier caller cancellation/deadlines can still end the wait. Heartbeats
and authority calls retain their short independent bounds. HTTP never disables
the query guard, even with `--stall-seconds 0`.
