"""Generated container checks and smoke against synthetic frontend files; no Docker/network."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import boring_deploy
from test_deploy_smoke import load_steps

FERRY_SRC = str(Path(__file__).resolve().parents[1] / "mcp-ferry/src")
PREFIX = ("import sys, types\nsys.path.insert(0, " + repr(FERRY_SRC) + ")\n"
          "types.get=lambda u, **k: types.SimpleNamespace(status_code=503 if 'bad' in u else 401)\n")
START = "2026-10-28T00:00:00Z"


def configuration(tmp_path, mode, bad=False, pond=True):
    backend = {"upstream": {"url": "http://backend.invalid/mcp"}}
    config = {"backends": {"backend": backend}}
    if mode != "gateway":
        backend["service"] = "backend.json"
    if mode != "backend":
        config["gateways"] = {"gateway": {"service": "gateway.json", "backends": ["backend"]}}
    for name in ("backend", "gateway"):
        (tmp_path / (name + ".json")).write_text(json.dumps({
            "url": f"https://{name}.invalid/mcp",
            **({"pond": {"url": f"https://{'bad-' if bad and name == 'gateway' else ''}{name}-authority.invalid"}}
               if pond else {})}))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


def generated(args):
    code = args[args.index("-c") + 1].replace("import httpx2", "import types as httpx2")
    return subprocess.run([sys.executable, "-I", "-c", PREFIX + code, args[-1]],
                          capture_output=True, text=True, check=True)


@pytest.mark.parametrize("mode", ["backend", "gateway", "mixed"])
def test_reach_checks_all_frontend_authorities(tmp_path, monkeypatch, mode):
    path = configuration(tmp_path, mode)
    steps = load_steps()
    monkeypatch.setattr(steps, "_compose", lambda ctx, *args, **kw: generated(args))
    said = []
    steps._check_crocodile_reach(SimpleNamespace(image="fixture", log=said.append), "ferry", str(path))
    assert len(said) == (2 if mode == "mixed" else 1)
    assert any(("gateway-authority" if mode == "gateway" else "backend-authority") in s for s in said)


def test_bad_gateway_authority_fails_before_switch(tmp_path, monkeypatch):
    path = configuration(tmp_path, "mixed", bad=True)
    steps = load_steps()
    monkeypatch.setattr(steps, "_compose", lambda ctx, *args, **kw: generated(args))
    with pytest.raises(boring_deploy.Failure, match="bad-gateway-authority"):
        steps._check_crocodile_reach(SimpleNamespace(image="fixture", log=lambda s: None), "ferry", str(path))


def test_reach_checks_registration_and_introspection_authorities(tmp_path, monkeypatch):
    path = configuration(tmp_path, "gateway")
    front_path = tmp_path / "gateway.json"
    front = json.loads(front_path.read_text())
    front["oauth"] = {"introspection_url": "https://bad-introspection.invalid"}
    front_path.write_text(json.dumps(front))
    steps = load_steps()
    monkeypatch.setattr(steps, "_compose", lambda ctx, *args, **kw: generated(args))
    with pytest.raises(boring_deploy.Failure, match="bad-introspection"):
        steps._check_crocodile_reach(SimpleNamespace(image="fixture", log=lambda s: None), "ferry", str(path))


def test_frontend_without_authority_skips_reach_only(tmp_path, monkeypatch):
    path = configuration(tmp_path, "gateway", pond=False)
    steps = load_steps()
    monkeypatch.setattr(steps, "_compose", lambda ctx, *args, **kw: generated(args))
    steps._check_crocodile_reach(SimpleNamespace(image="fixture", log=lambda s: pytest.fail(s)),
                                 "ferry", str(path))


@pytest.mark.parametrize("mode", ["backend", "gateway", "mixed"])
@pytest.mark.parametrize("problem", [None, "endpoint", "registration"])
def test_smoke_covers_all_frontends(tmp_path, monkeypatch, mode, problem):
    path = configuration(tmp_path, mode)
    steps = load_steps()
    monkeypatch.setattr(steps, "SMOKE_SECONDS", 0)
    monkeypatch.setattr(steps.time, "sleep", lambda _: None)
    monkeypatch.setattr(steps, "_service", lambda ctx: ("ferry", {"command": ["--config", str(path)]}))
    from datetime import datetime, timezone
    monkeypatch.setattr(steps, "_started", lambda *a: (START, datetime(2026, 10, 28, tzinfo=timezone.utc)))
    seen = []

    def endpoint(url):
        seen.append(url)
        return "gateway unavailable" if problem == "endpoint" and "gateway" in url else None

    monkeypatch.setattr(steps, "endpoint_identity", endpoint)

    def compose(ctx, *args, **kwargs):
        if args[0] == "logs":
            names = ["backend"] + ([] if problem == "registration" else ["gateway"])
            return SimpleNamespace(stdout="".join(
                f"2026-10-28T00:00:01Z MCP registration of {n}: registered\n" for n in names))
        if "-c" in args:
            return generated(args)
        return SimpleNamespace(stdout=Path(args[-1]).read_text())

    monkeypatch.setattr(steps, "_compose", compose)
    ctx = SimpleNamespace(home=tmp_path, log=lambda _: None)
    if mode != "backend" and problem:
        with pytest.raises(boring_deploy.Failure, match="gateway"):
            steps.smoke(ctx)
    else:
        steps.smoke(ctx)
    expected = (["https://backend.invalid/mcp"] if mode != "gateway" else [])
    if mode != "backend":
        expected += ["https://gateway.invalid/mcp"]
    assert seen == expected
