"""Ferry installs register the frontend names chosen by its routing owner."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_install import host, release, load  # noqa: F401
from test_install_registration import admin, URL, PUBLIC
from deploy_context import deploy_context


def configured(host, monkeypatch, *, multiple=False, url=URL):
    directory = release(host, "mcp-ferry")
    steps = load(directory, host, monkeypatch)
    calls, issued = admin(steps, monkeypatch)
    source = host / "site-config"
    (source / "credentials/mcp-client-secret").unlink()
    (source / "services").mkdir()
    names = ["one", "shared"] if multiple else ["one"]
    routing = {"backends": {"one": {"upstream": {"url": "http://backend.invalid/mcp"},
                                   "service": "services/one.json"}}}
    if multiple:
        routing["gateways"] = {"shared": {"service": "services/shared.json", "backends": ["one"]}}
    (source / "config.json").write_text(json.dumps(routing))
    for index, name in enumerate(names):
        (source / f"services/{name}.json").write_text("{}")
    ctx = deploy_context(home=host/"home", release=SimpleNamespace(component="boring-mcp-ferry",
        directory=directory, details={}), log=lambda _: None)
    return steps, ctx, {"config_dir": str(source), "url": url, "callers": "pond=reader"}, calls, issued


@pytest.mark.parametrize("multiple", [False, True])
def test_ferry_registers_each_selected_frontend_and_writes_relative_secret(host, monkeypatch, multiple):
    urls = "one=https://one.example.test/mcp,shared=https://shared.example.test/mcp" if multiple else URL
    steps, ctx, settings, calls, issued = configured(host, monkeypatch, multiple=multiple, url=urls)
    steps.plan(ctx, settings)
    assert not issued
    steps.install(ctx, settings)
    assert isinstance(ctx.changed, list) and ctx.changed
    applied = [c for c, _ in calls if "--apply" in c]
    assert [c[2] for c in applied] == (["one", "shared"] if multiple else ["one"])
    routing = json.loads((ctx.home/"config/config.json").read_text())
    assert "mode" not in routing and "backends" in routing
    for name in (["one", "shared"] if multiple else ["one"]):
        path = ctx.home / f"config/services/{name}.json"
        data = json.loads(path.read_text())
        assert data["mode"] == "crocodile" and data["crocodile"]["url"] == PUBLIC
        expected = "https://"+name+".example.test/mcp" if multiple else URL
        assert data["url"] == expected
        secret = (path.parent / data["crocodile"]["secret_file"]).resolve()
        assert secret.parent == ctx.home/"credentials"
        assert secret.read_text().strip() == issued[0]
    assert len({json.loads((ctx.home/f"config/services/{c[2]}.json").read_text())["crocodile"]["secret_file"]
                for c in applied}) == len(applied)


@pytest.mark.parametrize("url", [URL, "one=https://one.example/mcp", "unknown=https://x.example/mcp",
                               "one=https://x.example/mcp,one=https://y.example/mcp"])
def test_multiple_frontends_need_an_exact_explicit_url_selection_before_effects(host, monkeypatch, url):
    steps, ctx, settings, calls, issued = configured(host, monkeypatch, multiple=True, url=url)
    with pytest.raises(steps.Failure, match="--url"):
        steps.install(ctx, settings)
    assert not calls and not ctx.home.exists()
