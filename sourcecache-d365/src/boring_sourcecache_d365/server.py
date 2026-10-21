"""boring-sourcecache-d365: the MCP server over one or more source indexes.

    boring-sourcecache-d365 build --source <PackagesLocalDirectory> --db <file> --name <name> [--locales en-US,de]
    boring-sourcecache-d365 serve --config <config.json> --db <file> [--db <file> ...]

An index is one SQLite file (`--db`) with a name stored inside it (`--name`,
e.g. `d365scm-10.0.49` or an environment's name). `build` reads the source in
place and writes the file atomically. `serve` answers over HTTP as the
configuration says (boring-service-kit's ServiceConfig: its own issuer, an
external one such as the Crocodile, or access keys); with several files, the
tools' `index` parameter picks one by its name (see `source_info`), the first
being the default. Every result names its index.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__, sourcecache
from boring_service_kit.calllog import CallLog
from boring_service_kit.access import permitted
from boring_service_kit.http import ServiceConfig, add_config_argument
from boring_service_kit.pond import attach_heartbeat
from boring_service_kit.tools import READ_ONLY, as_mcp_tool, default_state_dir, refuse_unknown_arguments
from boring_service_kit.outcomes import Outcome, Refused

INSTRUCTIONS = (
    "Search a Dynamics 365 source index: text in all elements, an element by type and name, "
    "labels by text or ID with their exact references. Results name the index and its version. "
    "Working well: find with source_search (narrow with element_type and scope rather than paging "
    "deep), then read with source_element. Labels: label_lookup by text gives the ID; the ID with "
    "references=true gives every use in code. Who calls, uses or extends what is a cross-reference "
    "question, not a text search. Cite the index, path and indexed-file line numbers. "
    "For XML elements these are XML-file lines, not X++ editor or method-relative lines. "
    "Follow next_line to continue; line_cut means one oversized line was only partly returned."
)


def create_server(index_paths: list[str], state_dir: Path | None = None, log_calls: bool = False,
                  token_verifier: Any = None, auth: Any = None,
                  on_discover: Any = None, stall_seconds: float = sourcecache.STALL_SECONDS_DEFAULT,
                  warm_up: bool = False, enforce_access: bool = True,
                  auth_server_provider: Any = None, pond_bound: bool = False) -> MCPServer:
    """The tools. Every tool checks the verified caller's role. `enforce_access=False`
    drops that check; it is only for in-process use without a caller (tests)."""
    if not index_paths:
        raise Refused("invalid_request", "at least one --db is required")
    indexes: dict[str, sourcecache.Index] = {}
    for path in index_paths:
        opened = sourcecache.Index(path, stall_seconds, pond_bound=pond_bound)
        if opened.name in indexes:
            raise Refused("invalid_request", "index names must be unique", index=opened.name)
        indexes[opened.name] = opened
        if warm_up:
            opened.start_warm_up()
    default = next(iter(indexes))
    mcp = MCPServer("boring-sourcecache-d365", instructions=INSTRUCTIONS, version=__version__,
                    token_verifier=token_verifier, auth=auth, auth_server_provider=auth_server_provider)
    attach_heartbeat(mcp, on_discover)
    log = CallLog((state_dir or default_state_dir("boring-sourcecache-d365")) / "calls-sourcecache.jsonl" if log_calls else None)

    def pick(index: str | None) -> sourcecache.Index:
        name = index or default
        if name not in indexes:
            raise Refused("not_found", "no such index", index=name, available=sorted(indexes))
        return indexes[name]

    def register(fn: Any) -> None:
        mcp.tool(annotations=READ_ONLY)(as_mcp_tool(permitted(fn, "reader") if enforce_access else fn, log))

    def source_info(index: str | None = None) -> dict[str, Any]:
        """Indexes available; with `index`: its versions, counts and models (layer, publisher)."""
        if index is None:
            return {"indexes": [{**i.stamp(), "files": i.meta.get("files"), "labels": i.meta.get("labels"),
                                 "warm": i.warm, "default": name == default} for name, i in indexes.items()]}
        return pick(index).info()

    def source_search(query: str, package: str | None = None, model: str | None = None,
                      element_type: str | None = None, scope: str = "all", limit: int = sourcecache.SEARCH_DEFAULT,
                      offset: int = 0, index: str | None = None) -> dict[str, Any]:
        """Find text (3+ chars, case-insensitive) in all files; exact lines. scope: all|standard|custom. Microsoft publishers are standard; unknown model/publisher provenance appears only in all, with scope=unknown."""
        return pick(index).search(query, package, model, element_type, scope, limit, offset)

    def source_element(element_type: str, name: str, package: str | None = None, model: str | None = None,
                       start_line: int = 1, line_count: int = sourcecache.ELEMENT_LINES_DEFAULT,
                       index: str | None = None) -> dict[str, Any]:
        """Indexed file text by type/name; candidates if ambiguous. XML-file line numbers, not method-relative. Follow next_line; line_cut marks an oversized partial line."""
        return pick(index).element(element_type, name, package, model, start_line, line_count)

    def label_lookup(text: str | None = None, label_id: str | None = None, locale: str | None = None,
                     references: bool = False, limit: int = sourcecache.LABELS_DEFAULT,
                     index: str | None = None) -> dict[str, Any]:
        """Labels by text, or by ID (@SYS1, @File:Id) with references=true for every exact use in code."""
        return pick(index).labels(text, label_id, locale, references, limit)

    for fn in (source_info, source_search, source_element, label_lookup):
        register(fn)
    refuse_unknown_arguments(mcp)
    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="boring-sourcecache-d365", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="index a PackagesLocalDirectory")
    build.add_argument("--source", required=True)
    build.add_argument("--db", required=True, help="the index file to write")
    build.add_argument("--name", required=True, help="e.g. d365scm-10.0.49 or the environment's name")
    build.add_argument("--locales", help="label languages to keep, comma-separated (e.g. en-US,de); default: all")
    serve = commands.add_parser("serve", help="answer over HTTP, configured by --config")
    add_config_argument(serve)
    serve.add_argument("--db", action="append", required=True, help="an index file; repeat for more")
    serve.add_argument("--state-dir", type=Path)
    serve.add_argument("--no-call-log", action="store_true")
    serve.add_argument("--stall-seconds", type=float, default=sourcecache.STALL_SECONDS_DEFAULT,
                       help="stop a query after this long (default %(default)s; 0 = never)")
    serve.add_argument("--no-warm-up", action="store_true",
                       help="do not read the index files into the OS cache at start")
    args = parser.parse_args(argv)
    if args.command == "serve" and (not math.isfinite(args.stall_seconds) or args.stall_seconds < 0):
        parser.error("--stall-seconds must be finite and nonnegative")
    if args.command == "serve" and args.config is None:
        parser.error("serve requires --config")
    try:
        if args.command == "build":
            report = sourcecache.build(args.source, args.db, args.name,
                                     progress=lambda n: print(f"{n} files", file=sys.stderr, flush=True),
                                     locales=args.locales.split(",") if args.locales else None)
            print(json.dumps(report, indent=2))
            return
        config = ServiceConfig(args.config)
        # Behind a Pond a query must end within the Pond's call bound (config.call_limit);
        # on its own (Copilot on loopback) only --stall-seconds applies.
        limit, stall = config.call_limit, args.stall_seconds
        pond_bound = limit is not None and (stall == 0 or stall > limit)
        if pond_bound:
            stall = limit
        config.serve(lambda auth, discover: create_server(
            args.db, args.state_dir, not args.no_call_log, on_discover=discover, stall_seconds=stall,
            warm_up=not args.no_warm_up, enforce_access=True, pond_bound=pond_bound, **auth))
    except Outcome as outcome:
        print(json.dumps(outcome.as_dict()), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
