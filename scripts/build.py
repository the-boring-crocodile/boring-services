#!/usr/bin/env python3
"""Build offline, hash-locked install sets for the services; no deployment.

For each selected service, OUT/<service>/ receives the service wheel, every
dependency wheel its requirements.lock pins for the target (Linux or Windows
x86_64, one CPython version), requirements.txt (the lock plus the service
wheel's own hash), SHA256SUMS and, for Linux, its Dockerfile with the matching
base image. Pond Common comes from --common-wheels. Requires uv (PATH or $UV).
Output must not exist; source copies keep setuptools clutter out of the tree.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _develop import load  # noqa: E402  (artifact and develop are loaded by build(), after its preflight)

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("knowledge", "canvas", "sourcecache-d365", "fishmarket")
DOCKERFILE = {"fishmarket": "fishmarket/deploy/Dockerfile"}
WINDOWS_PLATFORMS = ("win_amd64",)

def run(*args, **kw):
    return subprocess.run(args, cwd=ROOT, check=True, capture_output=True, text=True, **kw).stdout


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preflight(services, common_wheels):
    """Every input is checked before the output directory exists."""
    if common_wheels is None or not common_wheels.is_dir():
        raise FileNotFoundError(f"Common wheels directory missing: {common_wheels}")
    for service in services:
        if not (ROOT / service / "requirements.lock").is_file():
            raise FileNotFoundError(f"{service}/requirements.lock missing (scripts/lock.py)")
    return {"services": list(services), "common_wheels": str(common_wheels)}


def build(out, target, services=SERVICES, python_version="3.14", common_wheels=None, python_image=None):
    if out.exists():
        raise SystemExit("output already exists; choose a new versioned directory")
    inputs = preflight(services, common_wheels)
    artifact, develop = load(common_wheels)
    platforms = develop.LINUX_PLATFORMS if target == "linux" else WINDOWS_PLATFORMS
    tool = develop.uv()
    out.mkdir(parents=True)
    image = python_image or f"python:{python_version}-slim"
    record = {"inputs": inputs, "target": target, "python": python_version, "uv": run(tool, "--version").strip(),
              "build_constraints_sha256": digest(ROOT / "scripts/build-constraints.txt"),
              "commit": run("git", "rev-parse", "HEAD").strip(),
              "dirty": bool(run("git", "status", "--porcelain").strip()), "services": {}}
    with tempfile.TemporaryDirectory(prefix="boring-services-build-") as temp:
        temp = Path(temp)
        run(tool, "venv", "--quiet", "--seed", str(temp / "pip"))
        for service in services:
            root, dest = ROOT / service, out / service
            dest.mkdir()
            copy = temp / service
            copy.mkdir()
            shutil.copyfile(root / "pyproject.toml", copy / "pyproject.toml")
            if (root / "README.md").exists():
                shutil.copyfile(root / "README.md", copy / "README.md")
            develop.plain_copy(root / "src", copy / "src")
            run(tool, "build", "--build-constraint", str(ROOT / "scripts/build-constraints.txt"),
                "--wheel", "--out-dir", str(dest), str(copy),
                env=dict(os.environ, SOURCE_DATE_EPOCH=run("git", "log", "-1", "--format=%ct").strip()))
            (own,) = dest.glob("*.whl")
            command = [str(temp / "pip/bin/python"), "-m", "pip", "download", "--quiet", "--no-deps",
                       "--require-hashes", "--only-binary=:all:", "--python-version", python_version,
                       "--implementation", "cp", "--find-links", str(common_wheels),
                       "--dest", str(dest), "-r", str(develop.target_lock(
                           temp / "pip/bin/python", root / "requirements.lock", temp / f"{service}.lock",
                           python_version, {"linux": "linux", "windows": "win32"}[target]))]
            for platform in platforms:
                command += ["--platform", platform]
            run(*command)
            name, version = own.name.split("-")[:2]
            lock = (root / "requirements.lock").read_text()
            lock += f"{name.replace('_', '-')}=={version} \\\n    --hash=sha256:{digest(own)}\n"
            (dest / "requirements.txt").write_text(lock)
            if target == "linux":
                dockerfile = (ROOT / DOCKERFILE.get(service, f"{service}/Dockerfile")).read_text()
                dockerfile = re.sub(r"(?m)^ARG PYTHON_IMAGE=.*$", f"ARG PYTHON_IMAGE={image}", dockerfile, count=1)
                (dest / "Dockerfile").write_text(dockerfile)
            wheels = {p.name: digest(p) for p in sorted(dest.glob("*.whl"))}
            (dest / "SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n, h in wheels.items()))
            record["services"][service] = {"wheel": own.name, "requirements_sha256": digest(dest / "requirements.txt"),
                                           "wheels": wheels}
    (out / "MANIFEST.json").write_text(json.dumps(record, indent=2) + "\n")
    if target == "linux":
        for service in services:
            print(release_artifact(artifact, out, service, record))
    print(json.dumps({"target": target, "python": python_version, "services": list(record["services"])}))
    return record


DEPLOY = {  # what each service's deploy needs to know (RELEASE.json)
    "knowledge": {"module": "boring_knowledge",
                  "data": {"file": "state/notebook.db", "query": "SELECT value FROM meta WHERE key = 'schema'"},
                  "data_generation": 3, "upgrades_from": 1},
    "canvas": {"module": "boring_canvas"},
    "fishmarket": {"module": "boring_fishmarket"},
    "sourcecache-d365": {"module": "boring_sourcecache_d365"},
}


INSTALL = {  # services that install themselves (deploy/install.py): the release's install/ files
    "canvas": {"compose.yaml": "canvas/deploy/compose.yaml", "canvas-admin": "canvas/deploy/canvas-admin"},
    "knowledge": {"compose.yaml": "knowledge/deploy/compose.yaml"},
    "fishmarket": {"compose.yaml": "fishmarket/deploy/compose.yaml"},
    "sourcecache-d365": {"compose.yaml": "sourcecache-d365/deploy/compose.yaml"},
}


def release_artifact(artifact, out, service, record):
    """OUT/boring-<service>-<version>-linux-x86_64.tar.gz: the install set as the
    image's build context, plus the deploy (boring-deploy from the Common wheels)
    and, for a service that installs itself, its install steps and templates."""
    dest = out / service
    version = record["services"][service]["wheel"].split("-")[1]
    files = {f"image/{p.name}": p for p in sorted(dest.iterdir()) if p.is_file()}
    files.update({f"install/{name}": ROOT / source for name, source in INSTALL.get(service, {}).items()})
    files.update({name: ROOT / name for name in ("LICENSE", "COPYING.md")})
    extra = dict(DEPLOY[service])
    release = {"component": f"boring-{service}", "version": version, "runtime": "docker",
               "default_home": f"/opt/boring-{service}", "image": f"boring-{service}",
               "context": "image", "commit": record["commit"], **extra}
    install = ROOT / "deploy" / "install.py" if service in INSTALL else None
    return artifact.assemble(out / "artifacts", release, ROOT / "deploy" / "steps.py", files, install=install)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--target", choices=["linux", "windows"], required=True)
    parser.add_argument("--service", choices=SERVICES, action="append", help="Build selected services; default all")
    parser.add_argument("--python-version", choices=["3.10", "3.12", "3.13", "3.14"], default="3.14")
    parser.add_argument("--common-wheels", type=Path, required=True, help="Pond Common release wheels")
    parser.add_argument("--python-image", help="Linux base image (pin it by digest for deployments); "
                                               "default python:<version>-slim")
    args = parser.parse_args()
    build(args.out.resolve(), args.target, tuple(args.service) if args.service else SERVICES,
          args.python_version, args.common_wheels.resolve(), args.python_image)
