#!/usr/bin/env python3
"""Assemble the optional Linux tunnel release; no application, wheel or container."""
import argparse
from pathlib import Path
from _develop import load

ROOT = Path(__file__).resolve().parents[1]


def release_artifact(artifact, out):
    version = (ROOT / "tunnel/VERSION").read_text().strip()
    release = {"component": "boring-tunnel", "version": version, "runtime": "files",
               "default_home": "/opt/boring-tunnel",
               "requires": {"boring-crocodile": ">=0.5.0,<0.6"}}
    files = {name: ROOT / name for name in ("LICENSE", "COPYING.md")}
    files["deploy.d/tunnel_model.py"] = ROOT / "tunnel/deploy/model.py"
    return artifact.assemble(out, release, ROOT / "tunnel/deploy/steps.py", files,
                             install=ROOT / "tunnel/deploy/install.py")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--common-wheels", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("--out already exists")
    artifact, _ = load(args.common_wheels)
    print(release_artifact(artifact, args.out))


if __name__ == "__main__":
    main()
