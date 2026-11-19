"""Installer registration protocol; no host account, Docker or live Crocodile."""
import json
import os
import pwd
from types import SimpleNamespace

import pytest
from test_install import host, release, load, deploy  # noqa: F401

PUBLIC = "https://controller.example.test"
URL = "https://service.example.test/mcp"


def admin(steps, monkeypatch, *, confirmed=True, broken=False):
    calls, issued = [], []
    def child(command, **options):
        if os.fspath(command[0]) != "/opt/boring-crocodile/current/bin/boring-crocodile-admin":
            raise FileNotFoundError(command[0])
        calls.append((list(command), options))
        if command[1:] == ["list", "--json"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"public_base_url": PUBLIC,
                "ponds": ["pond"], "pirates": {}, "pond_workers": {"pond": 1200}}))
        if "--apply" not in command:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"name": command[2],
                "url": URL, "callers": {"pond": "reader"}, "roles": ["reader"],
                "default_role": "reader", "plan_digest": "fixture-plan"}))
        if not confirmed:
            return SimpleNamespace(returncode=2, stdout="")
        assert options["stdout"] is steps.subprocess.PIPE
        assert options.get("stderr") is None and options.get("stdin") is None
        issued.append("fixture-secret-never-log")
        return SimpleNamespace(returncode=0, stdout="malformed-secret-never-log" if broken else json.dumps({
            "client_id": "svc_fixture", "client_secret": issued[-1]}))
    monkeypatch.setattr(steps, "root_child", child, raising=False)
    return calls, issued


@pytest.mark.parametrize("service", ["knowledge", "canvas", "fishmarket", "sourcecache-d365"])
def test_install_registers_with_url_callers_and_keeps_secret_private(host, monkeypatch, service):
    directory = release(host, service)
    steps = load(directory, host, monkeypatch)
    calls, issued = admin(steps, monkeypatch)
    home = host / ("boring-" + service)
    source = host / "site-config"
    (source / "credentials/mcp-client-secret").unlink()
    settings = {"config_dir": str(source), "url": URL, "callers": "pond=reader"}
    if service == "canvas":
        settings.update(public_url="https://service.example.test", web_bind="127.0.0.1")
    elif service == "sourcecache-d365":
        (host / "index.db").write_bytes(b"fixture-index")
        settings["index"] = str(host / "index.db")
    # All root-input checks and the service install hook are real.
    steps.ready(SimpleNamespace(release=SimpleNamespace(component="boring-"+service),
        home=home, run=lambda *a, **k: SimpleNamespace(returncode=0)), settings)
    said = []
    from deploy_context import deploy_context
    ctx = deploy_context(home=home, release=SimpleNamespace(component="boring-"+service,
        directory=directory, details={}), log=said.append, run=lambda *a, **k: SimpleNamespace(returncode=0))
    proposed = steps.plan(ctx, settings)
    assert not issued
    steps.install(ctx, settings)
    data = json.loads((home / "config/config.json").read_text())
    assert data["url"] == URL and data["mode"] == "crocodile"
    assert data["crocodile"] == {"url": PUBLIC, "client_id": "svc_fixture",
                                "secret_file": "../credentials/mcp-client-secret"}
    secret = home / "credentials/mcp-client-secret"
    assert secret.read_text().strip() == issued[0] and secret.stat().st_mode & 0o777 == 0o400
    assert issued[0] not in json.dumps(proposed + said) + json.dumps(data)
    add = [c for c, _ in calls if c[1] == "service-add"]
    assert add and all(c[c.index("--callers")+1] == "pond=reader" for c in add)
    assert "--apply" in add[-1]
    assert "--url" in {"--"+s.name for s in steps.SETTINGS(ctx.release)}
    assert "--callers" in {"--"+s.name for s in steps.SETTINGS(ctx.release)}


@pytest.mark.parametrize("problem", ["unconfirmed", "bad-reply", "legacy-access"])
def test_registration_failure_has_no_compose_or_secret_in_diagnostics(host, monkeypatch, problem):
    directory = release(host, "knowledge")
    steps = load(directory, host, monkeypatch)
    calls, issued = admin(steps, monkeypatch, confirmed=problem!="unconfirmed", broken=problem=="bad-reply")
    home = host / "home"
    source = host / "site-config"
    (source / "credentials/mcp-client-secret").unlink()
    if problem == "legacy-access":
        (source / "config.json").write_text(json.dumps({"url": URL, "access": []}))
    from deploy_context import deploy_context
    ctx = deploy_context(home=home, release=SimpleNamespace(component="boring-knowledge",
        directory=directory, details={}), log=lambda _: None)
    with pytest.raises(steps.Failure) as error:
        steps.install(ctx, {"config_dir": str(source), "url": URL, "callers": "pond=reader"})
    assert not (home / "compose.yaml").exists()
    assert "secret-never-log" not in str(error.value)
    if problem == "legacy-access":
        assert not calls and "--callers" in str(error.value)
