"""Disposable Windows acceptance; no admin, global install or external listener."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent


def run(*args):
    subprocess.run(args, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--acl-only", action="store_true")
    selection.add_argument("--transport-only", action="store_true")
    args = parser.parse_args()
    if os.name != "nt" or sys.version_info[:2] != (3, 13):
        raise SystemExit(
            "Requires Windows and Python 3.13 from the local fixture venv."
        )
    # Only our newly created descendant gets a restricted DACL.
    root = Path(tempfile.mkdtemp(prefix="fishmarket-private-", dir=HERE))
    try:
        sid = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout.strip()
        if not sid.startswith("S-1-5-") or any(c not in "S-0123456789" for c in sid):
            raise RuntimeError("Cannot establish current user SID")
        subprocess.run(
            [
                "icacls.exe",
                str(root),
                "/inheritance:r",
                "/grant:r",
                "*" + sid + ":(OI)(CI)F",
                "*S-1-5-18:(OI)(CI)F",
                "*S-1-5-32-544:(OI)(CI)F",
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
        import boring_fishmarket

        package = Path(boring_fishmarket.__file__).resolve()
        if Path(sys.prefix).resolve() not in package.parents:
            raise RuntimeError("Acceptance must import the installed fixture wheel")
        print(
            "PASS: fixture wheel imports; private test-folder DACL established.",
            flush=True,
        )
        tests = HERE / "tests"
        if not args.transport_only:
            run(
                sys.executable,
                "-I",
                "-m",
                "pytest",
                "-q",
                "-ra",
                "--tb=short",
                "--basetemp",
                str(root / "pytest"),
                str(tests),
                "-k",
                "collected_file_inherits_private_parent_acl"
                if args.acl_only
                else "not vendored_kit and not tcp_",
            )
        if args.acl_only:
            print("PASS: focused DACL acceptance.")
            return
        # Transport and browser report independently; an unavailable browser
        # cannot turn successful streaming assertions into a skipped test.
        run(
            sys.executable,
            "-I",
            "-m",
            "pytest",
            "-q",
            "-ra",
            "-s",
            "--tb=short",
            "--basetemp",
            str(root / "tcp"),
            str(tests / "test_tcp.py"),
        )
        run(
            sys.executable,
            "-I",
            str(HERE / "rehearse.py"),
            "--self-test",
            "--temp-parent",
            str(root),
        )
    finally:
        shutil.rmtree(root)
    print(
        "PASS: TCP and child-process handover; private state removed."
        if args.transport_only
        else "PASS: Windows helper/lock/DACL/recovery and loopback rehearsal; private state removed."
    )


if __name__ == "__main__":
    main()
