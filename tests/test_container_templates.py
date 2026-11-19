"""Compose supplies container addresses; the shared reader validates the same data."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from boring_service_kit.http import ServiceConfig
from deploy_context import deploy_context
from test_install import host, release, load  # noqa: F401
from test_compose_exposure import DOCKER

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(DOCKER is None, reason="Docker Compose parser is not installed")
@pytest.mark.parametrize("service, filename", [
    ("canvas", "one"), ("knowledge", "one"), ("sourcecache-d365", "one"),
    ("fishmarket", "one"), ("mcp-ferry", "one"), ("mcp-ferry", "o\'ne"),
    ("mcp-ferry", "dollar$POND_TEST_SLOT"), ("mcp-ferry", "both\'$POND_TEST_SLOT"),
    ("mcp-ferry", "braces${POND_TEST_SLOT}")])
def test_rendered_deploy_owns_runtime_and_candidate_listener(host, monkeypatch, service, filename):
    monkeypatch.setenv("POND_TEST_SLOT", "must-not-replace")
    directory = release(host, service)
    install = load(directory, host, monkeypatch, exposure=False)
    if service == "mcp-ferry":
        source = host / "site-config"
        (source / "services").mkdir()
        (source / "config.json").write_text(json.dumps({
            "backends":{"one":{"service":f"services/{filename}.json","upstream":{"url":"http://backend.invalid/mcp"}}},
            "gateways":{"shared":{"service":"services/shared.json","backends":["one"]}}}))
        for name in (filename, "shared"):
            (source / f"services/{name}.json").write_text("{}")
    captured = []
    def run(*command, **options):
        reply = subprocess.run([DOCKER, *command[1:]], capture_output=True, text=True,
            env={**__import__("os").environ, **options.get("env", {})}, check=True)
        captured.append(json.loads(reply.stdout))
        return reply
    ctx = deploy_context(home=host/"not-created", release=SimpleNamespace(
        component="boring-"+service, directory=directory), run=run)
    install._proposed_exposure(ctx, {"config_dir":str(host/"site-config"),
        "edge_network":"fixture-proxy", "index":str(host/"index.db")})
    managed, = captured[-1]["services"].values()
    listeners = json.loads(managed["environment"]["BORING_SERVICE_LISTENERS"])
    assert len(listeners) == (2 if service == "mcp-ferry" else 1)
    assert len({x["port"] for x in listeners.values()}) == len(listeners)
    if service == "mcp-ferry":
        assert set(listeners) == {f"/run/config/services/{filename}.json", "/run/config/services/shared.json"}
    path = host / "frontend.json"
    path.write_text(json.dumps({"url":"https://service.example.test/mcp",
        "mode":"keys", "keys":{"file":"keys.json"}}))
    # Model only Docker's read-only config mount; the parser and metadata owner
    # are real. No container, image build or root file is needed.
    resolve = Path.resolve
    monkeypatch.setenv("BORING_SERVICE_CONTAINER", "1")
    monkeypatch.setenv("BORING_SERVICE_LISTENERS", json.dumps(listeners))
    for target in listeners:
        monkeypatch.setattr(Path, "resolve", lambda self, *a, **kw:
            Path(target) if self == path else resolve(self, *a, **kw))
        info = ServiceConfig.inspect(path)
        assert (info.bind, info.port) == (listeners[target]["bind"], listeners[target]["port"])
        assert not hasattr(info, "behind_proxy")
        ports = managed.get("ports", [])
        assert any(p["target"] == info.port for p in ports) or "boring.proxy-network" in managed["labels"]
    image = ROOT / service / ("deploy/Dockerfile" if service == "fishmarket" else "Dockerfile")
    assert "BORING_SERVICE_CONTAINER=1" in image.read_text()
    assert not ctx.home.exists()
