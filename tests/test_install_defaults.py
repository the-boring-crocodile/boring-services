"""Standard Crocodile installs take URL/callers without a prepared config folder."""
import json
import pytest
from test_install import host, release, load, deploy  # noqa: F401
from test_install_registration import admin, URL, PUBLIC


@pytest.mark.parametrize("service", ["knowledge", "canvas", "fishmarket", "sourcecache-d365"])
def test_one_install_command_owns_auth_config_and_ignores_unselected_inputs(host, monkeypatch, service):
    directory = release(host, service)
    steps = load(directory, host, monkeypatch, account=False)
    calls, issued = admin(steps, monkeypatch)
    # This directory is never selected: the standard path must not read/copy it.
    (host / "site-config/config.json").write_text("{unselected")
    home = host / ("new-" + service)
    args = [str(home), "--install", "--url", URL, "--callers", "pond=reader"]
    if service == "fishmarket":
        args += ["--edge-network", "fixture-proxy"]
    elif service == "sourcecache-d365":
        (host / "index.db").write_bytes(b"fixture-index")
        args += ["--index", str(host / "index.db")]
    code, text = deploy(directory, steps, *args, "--check")
    assert code == 0, text
    assert not home.exists() and not (host / "account").exists() and not issued
    code, text = deploy(directory, steps, *args)
    assert code == 0, text
    data = json.loads((home / "config/config.json").read_text())
    assert data == {"url":URL, "mode":"crocodile", "crocodile":{
        "url":PUBLIC, "client_id":"svc_fixture", "secret_file":"../credentials/mcp-client-secret"}}
    secret = home / "credentials/mcp-client-secret"
    assert secret.read_text().strip() == issued[0]
    assert secret.stat().st_mode & 0o777 == 0o400
    assert all(value not in text for value in issued)
    assert (home / "compose.yaml").exists()
    assert (host / "site-config/credentials/mcp-client-secret").read_text() == "synthetic"


@pytest.mark.parametrize("service", ["knowledge", "mcp-ferry"])
def test_missing_auth_choice_or_ferry_routing_is_refused_before_effects(host, monkeypatch, service):
    directory = release(host, service)
    steps = load(directory, host, monkeypatch, account=False)
    calls, issued = admin(steps, monkeypatch)
    home = host / ("missing-" + service)
    args = [str(home), "--install"]
    if service == "mcp-ferry":
        args += ["--url", URL]
    code, text = deploy(directory, steps, *args)
    assert code == 1 and "--config-dir" in text
    assert not home.exists() and not (host / "account").exists() and not calls and not issued
