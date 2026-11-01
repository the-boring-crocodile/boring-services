# boring-sourcecache-d365

Search D365 source and labels through MCP, from a snapshot of your development
environment. The agent can inspect code without scanning the source tree on
every question or having D365 installed on its own machine.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../docs/sourcecache-dark.svg">
  <img alt="Sourcecache, build a snapshot once and search it anywhere: Copilot on the dev box and a Linux Pond with no D365 installed call the read-only Sourcecache MCP server. An administrator runs the build once per D365 version; it reads the D365 source in PackagesLocalDirectory, which is yours under the terms you got it on, and writes an immutable SQLite snapshot with a trigram index that names its D365 version. The server serves that snapshot and older ones side by side with four tools: source_info, source_search, source_element and label_lookup." src="../docs/sourcecache.svg">
</picture>

## Why

Developing for Dynamics 365 Finance and Supply Chain means reading the
standard code all the time: which table has this field, what this class
does, which label says that. The standard source is hundreds of thousands of
XML files in `PackagesLocalDirectory`. Repeatedly scanning that tree can make
storage and file scanning the slow part of a simple lookup. Sourcecache builds
a searchable snapshot once: Copilot on the development box gets it as a tool, and an agent in a
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
tree that contains it. Building writes a local file; the administrator decides
where to serve it and who can query it.

The trade-off is freshness: a snapshot does not follow source edits or upgrades.
Use `source_info` to check its identity and versions before relying on an answer,
and [build a new snapshot](#building-a-snapshot) when the source changes.
Compiled references belong to pondNT's
[D365 service](https://github.com/the-boring-crocodile/boring-pondnt/blob/main/docs/D365_SERVICE.md#compiled-references-and-dependencies),
not this source-text index.

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

    boring-sourcecache-d365 build --source PackagesLocalDirectory --db snapshot-v2.db --name application-v2 \
        --application-version 10.0.2790.46 --platform-version 7.0.8199.32 --release-version 10.0.49 \
        --version-evidence "InstallationRecords on the VHD"

Replace the example paths and version values with this environment's own.
A snapshot gets a UUID `snapshot_id` and a receipt (name, application and
platform versions, build time); names are unique. Check the file, label and
skipped counts as well: a successful build can omit unsupported or oversized
files, and searching cannot recover content that was not indexed.

**Versions.** The release a snapshot is stamped with is what you read from the
installation's own records (InstallationRecords; decode their JSON by its BOM,
some are UTF-16) and give as `--application-version`, `--platform-version` and
optionally `--release-version` and `--version-evidence` (one line: where you
read them). The builder does not guess: model descriptors carry model versions
(ApplicationSuite 10.40.3708.49257 on 10.0.49, whose build is 10.0.2790.46),
so without the arguments `source_info` says `unknown`, with `version_source`.
The descriptor values are kept, labelled, as `builder_descriptor_versions`.
Snapshots from builders before 0.1.16 carry descriptor versions;
`source_info` says so in `version_source`.

**Readers.** `--readers N` (1-64, default 16) reads N files at once; one SQLite
connection writes them in the tree's order, so the snapshot's content is the
same for any N. A full PackagesLocalDirectory build is disk-bound on network
storage (measured on Azure storage: 16 readers 18.5x, 32 readers
36.4x the reads of one); on a local disk with the files cached it changes
little, since parsing and indexing then dominate. At most 2N files are read
ahead. The build writes to a
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
