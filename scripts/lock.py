#!/usr/bin/env python3
"""Regenerate a service's requirements.lock: runtime dependencies pinned with hashes.

One universal lock per service (Python 3.10-3.14, all platforms), next to its
pyproject.toml. Pond Common comes from its released wheels (--common-wheels,
until Common is on a package index); everything else from the package index.
Existing pins and hashes are kept unless you pass --upgrade-package NAME or
--fresh.

    python scripts/lock.py --common-wheels /path/to/boring-pond-common/dist [--service knowledge ...]
"""
import sys
from pathlib import Path

from _develop import load

_, develop = load()

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("knowledge", "canvas", "sourcecache-d365", "fishmarket", "mcp-ferry")


def main():
    parser = develop.lock_arguments(__doc__)
    parser.add_argument("--service", choices=SERVICES, action="append", help="default: all")
    args = parser.parse_args()
    for service in args.service or SERVICES:
        develop.lock(args, ROOT / service / "requirements.lock", [ROOT / service / "pyproject.toml"],
                     how=f"python scripts/lock.py --common-wheels <Common dist> --service {service}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
