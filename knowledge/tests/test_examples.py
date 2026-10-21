"""The configuration examples in ../examples are what the kit actually accepts."""
import json
import shutil
from pathlib import Path

import pytest
from boring_service_kit import keys
from boring_service_kit.http import ServiceConfig

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


@pytest.mark.parametrize("name", ["family.json", "canvas-family.json", "standalone.json", "keys.json", "other-issuer.json"])
def test_example_loads(tmp_path, name):
    data = json.loads((EXAMPLES / name).read_text())
    if "issuer" in data:  # its store lives in the container's state; here in a private temp dir
        (tmp_path / "oauth").mkdir(mode=0o700)
        data["issuer"]["database"] = str(tmp_path / "oauth" / "oauth.db")
    folder = tmp_path / "config"  # as installed: HOME/config/ beside HOME/credentials/
    folder.mkdir()
    for section in ("oauth", "pond", "issuer"):
        for field in ("secret_file", "approval_secret_file"):
            if field in data.get(section, {}):
                secret = folder / data[section][field]
                secret.parent.mkdir(exist_ok=True)
                secret.write_text("example-secret")
    if "keys" in data:
        keys.add(folder / data["keys"]["file"], "script@host", "reader", 1, issuer="key:example")
    (folder / "config.json").write_text(json.dumps(data))
    config = ServiceConfig(folder / "config.json")
    assert config.url == data["url"].rstrip("/")
    assert ("issuer" in data) == (config.issuer is not None)
    assert ("pond" in data) == (config.watchdog is not None)
