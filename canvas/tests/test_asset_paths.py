"""The committed assets must fit ordinary Windows installations."""
import hashlib
import json
from pathlib import Path

import boring_canvas

ASSETS = Path(boring_canvas.__file__).parent / "assets"


def test_asset_names_and_site_packages_paths_are_bounded():
    package_root = ASSETS.parent.parent
    files = [p for p in ASSETS.rglob("*") if p.is_file()]
    assert files
    assert max(len(p.name) for p in files) <= 100
    assert max(len(p.relative_to(package_root).as_posix()) for p in files) <= 160


def test_every_bundled_lock_entry_names_its_notices():
    dependencies = json.loads((ASSETS / "DEPENDENCIES.json").read_text())
    files = set()
    for entry in dependencies:
        assert entry["package"].startswith("node_modules/")
        if entry["bundled"]:
            assert entry["license_files"], entry["package"]
        for name in entry["license_files"]:
            assert name.startswith("licenses/") and (ASSETS / name).is_file()
            files.add(name)
    assert files == {p.relative_to(ASSETS).as_posix() for p in (ASSETS / "licenses").iterdir()}


def test_asset_inventory_matches_the_shipped_bytes():
    build = json.loads((ASSETS / "BUILD.json").read_text())
    dependencies = {item["package"]: item["version"]
                    for item in json.loads((ASSETS / "DEPENDENCIES.json").read_text())}
    assert build["transform"].startswith("Excalidraw " + dependencies["node_modules/@excalidraw/excalidraw"] + ":")
    assert build["tool"] == "esbuild " + dependencies["node_modules/esbuild"]
    manifest = build["artifacts"]
    actual = {p.relative_to(ASSETS).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in ASSETS.rglob("*") if p.is_file() and p.name != "BUILD.json"}
    assert manifest == actual
