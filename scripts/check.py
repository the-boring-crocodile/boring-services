#!/usr/bin/env python3
"""The one check for boring-services: a fresh environment from the hash lock, then the tests.

    python scripts/check.py --common-wheels /path/to/boring-pond-common/dist [--python /usr/bin/python3.12]

Installs the locked dependencies (--require-hashes) into a new virtual
environment, then boring-services from a copy of its tracked files, and runs the
tests there. Each service gets its own environment from its own lock; the
shared deploy and integration tests in tests/ run with knowledge, canvas and
sourcecache-d365. Ferry's deploy tests have their own environment because its
lock pins a different truststore version. Test tools are pinned by version in
requirements-dev.txt. Needs uv (PATH or $UV) and the package index (or uv's cache)
for the locked wheels and pytest. Exit status 0 means every step passed.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from _develop import load

_, develop = load()

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("knowledge", "canvas", "sourcecache-d365", "fishmarket", "mcp-ferry")
TOGETHER = ("knowledge", "canvas", "sourcecache-d365")
FERRY_DEPLOY_TESTS = (
    "tests/test_deploy_config.py",
    "tests/test_ferry_config_validation.py",
    "tests/test_legacy_access.py",
)


def deploy_wheel(args):
    """The deploy engine, for the tests of deploy/ (tests/test_install.py)."""
    return [str(p) for p in sorted(args.common_wheels.glob("boring_deploy-*.whl"))]


def main():
    args = develop.arguments(__doc__).parse_args()
    constraints = ROOT / "scripts/build-constraints.txt"
    failed = []
    with tempfile.TemporaryDirectory(prefix="services-check-") as tmp:
        tmp = Path(tmp)
        runs = [(s, (s,), ROOT / s, ("tests",)) for s in SERVICES]
        runs += [
            ("ferry-deploy", ("mcp-ferry",), ROOT, FERRY_DEPLOY_TESTS),
            ("together", TOGETHER, ROOT, ("tests", *(f"--ignore={p}" for p in FERRY_DEPLOY_TESTS))),
        ]
        for name, members, where, tests in runs:
            print(f"== {name}", flush=True)
            python = develop.environment(develop.uv(), args.python, tmp / f"venv-{name}",
                                         [ROOT / m / "requirements.lock" for m in members], args.common_wheels,
                                         [develop.package(ROOT, tmp / f"src-{name}-{m}", m) for m in members],
                                         constraints, ["-r", str(ROOT / "requirements-dev.txt"), *(deploy_wheel(args) if where == ROOT else [])])
            try:
                develop.run(python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests, cwd=where)
            except subprocess.CalledProcessError:
                failed.append(name)
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print("all boring-services checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
