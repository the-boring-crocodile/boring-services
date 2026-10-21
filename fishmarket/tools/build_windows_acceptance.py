"""Build offline Windows 3.13 acceptance from the shared service builder."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]


def run(*args):
    subprocess.run(args, check=True, cwd=ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists():
        raise SystemExit("Choose a new output directory.")
    out.mkdir(parents=True)
    release = out / "release"
    run(
        sys.executable,
        str(ROOT / "scripts/build.py"),
        "--service",
        "fishmarket",
        "--target",
        "windows",
        "--python-version",
        "3.13",
        "--out",
        str(release),
    )
    bundle = out / "fishmarket-windows"
    bundle.mkdir()
    wheels = bundle / "wheels"
    wheels.mkdir()
    for wheel in release.glob("*.whl"):
        shutil.copyfile(wheel, wheels / wheel.name)
    inputs = out / "acceptance.in"
    inputs.write_text("-r " + str(release / "requirements.txt") + "\npytest==9.1.1\n")
    lock = bundle / "requirements.txt"
    run(
        "uv",
        "pip",
        "compile",
        "--python-platform",
        "x86_64-pc-windows-msvc",
        "--python-version",
        "3.13",
        "--only-binary",
        ":all:",
        "--find-links",
        str(wheels),
        "--generate-hashes",
        "-o",
        str(lock),
        str(inputs),
    )
    run(
        sys.executable,
        "-m",
        "pip",
        "download",
        # uv already resolved the complete target lock; pip must not evaluate
        # transitive markers against this Linux build interpreter.
        "--no-deps",
        "--only-binary",
        ":all:",
        "--platform",
        "win_amd64",
        "--python-version",
        "3.13",
        "--implementation",
        "cp",
        "--abi",
        "cp313",
        "--require-hashes",
        "--find-links",
        str(wheels),
        "--dest",
        str(wheels),
        "-r",
        str(lock),
    )
    # Resolve again offline against the downloaded wheels for the actual target.
    # Missing target dependencies or incompatible wheels fail before publication.
    run(
        "uv",
        "pip",
        "compile",
        "--no-index",
        "--find-links",
        str(wheels),
        "--python-platform",
        "x86_64-pc-windows-msvc",
        "--python-version",
        "3.13",
        "--only-binary",
        ":all:",
        "-o",
        str(out / "offline-verified.txt"),
        str(lock),
    )
    # Reuse source-only copying: archive content, not host ownership/ACL metadata.
    sys.path.insert(0, str(ROOT / "scripts"))
    from build import plain_copy

    (bundle / "tests").mkdir()
    plain_copy(ROOT / "fishmarket/tests", bundle / "tests")
    (bundle / "deploy").mkdir()
    shutil.copyfile(
        ROOT / "fishmarket/deploy/config.example.json",
        bundle / "deploy/config.example.json",
    )
    for source, target in [
        ("windows_acceptance.py", "windows_acceptance.py"),
        ("rehearse.py", "rehearse.py"),
        ("install-windows.cmd", "install.cmd"),
        ("rehearse-windows.cmd", "rehearse.cmd"),
    ]:
        data = (TOOLS / source).read_text()
        (bundle / target).write_bytes(
            data.replace("\n", "\r\n").encode()
            if target.endswith(".cmd")
            else data.encode()
        )
    shutil.copyfile(
        ROOT / "fishmarket/docs/WINDOWS_ACCEPTANCE.md", bundle / "README.md"
    )
    manifest = json.loads((release / "MANIFEST.json").read_text())
    if manifest["dirty"]:
        raise RuntimeError("Acceptance artifacts require a clean source checkpoint.")
    manifest["acceptance_files"] = {
        str(p.relative_to(bundle)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(bundle.rglob("*"))
        if p.is_file()
    }
    (bundle / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    archive = out / "fishmarket-windows-py313.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(bundle.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(out))
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
    print("Bundle SHA256: " + hashlib.sha256(archive.read_bytes()).hexdigest())
    print("Offline Windows acceptance bundle prepared; native acceptance is pending.")


if __name__ == "__main__":
    main()
