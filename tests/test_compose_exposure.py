"""Actual normalized Compose exposure, independent of service JSON assertions."""
from types import SimpleNamespace
import json
import shutil

DOCKER = shutil.which("docker", path="/usr/bin:/bin")

import pytest
import boring_deploy
from test_deploy_smoke import load_steps
from deploy_context import deploy_context
from test_install import host, release, load  # noqa: F401


@pytest.mark.parametrize("ip", ["127.0.0.1", "127.12.2.3", "::1"])
def test_rendered_loopback_publications_are_shown(ip):
    steps = load_steps()
    said = steps.exposure({"ports":[{"host_ip":ip,"published":"18512","target":8000,"protocol":"tcp"}]})
    assert ip in said and "18512" in said and "8000" in said


@pytest.mark.parametrize("ip", [None, "", "0.0.0.0", "::", "192.168.1.4"])
def test_public_port_is_refused_even_with_proxy_label_and_json_flag(ip):
    steps = load_steps()
    with pytest.raises(boring_deploy.Failure, match="loopback"):
        steps.exposure({"ports":[{"host_ip":ip,"published":"18512","target":8000}],
                        "labels":{"boring.proxy-network":"edge"}, "networks":{"edge":{}},
                        "behind_proxy":True})


def test_named_proxy_network_must_actually_be_attached_and_have_no_public_ports():
    steps = load_steps()
    assert "proxy network edge" in steps.exposure({"labels":{"boring.proxy-network":"edge"},
                                                  "networks":{"edge":{}}})
    with pytest.raises(boring_deploy.Failure, match="proxy network"):
        steps.exposure({"labels":{"boring.proxy-network":"missing"}, "networks":{"edge":{}}})
    with pytest.raises(boring_deploy.Failure, match="proxy network"):
        steps.exposure({"networks":{"edge":{}}})


@pytest.mark.parametrize("mode", ["host", "service:other", "container:other"])
def test_shared_network_namespace_does_not_bypass_exposure(mode):
    steps = load_steps()
    with pytest.raises(boring_deploy.Failure, match="network_mode"):
        steps.exposure({"network_mode":mode, "labels":{"boring.proxy-network":"edge"},
                        "networks":{"edge":{}}})


def test_installed_preflight_checks_compose_before_service_config_or_image_work(tmp_path, monkeypatch):
    steps = load_steps()
    (tmp_path/"compose.yaml").write_text("fixture")
    monkeypatch.setattr(steps, "_service", lambda ctx, image=None: ("managed",
        {"ports":[{"published":"18512","target":8000}]}))
    ctx = deploy_context(home=tmp_path)
    assert "loopback" in steps.preflight(ctx)[0]
    assert not ctx.run.called


def test_first_install_plan_uses_rendered_template_and_refuses_before_home(host, monkeypatch):
    directory = release(host, "knowledge")
    install = load(directory, host, monkeypatch, exposure=False)
    replies = []
    def run(*command, **kwargs):
        text = __import__("pathlib").Path(command[command.index("-f")+1]).read_text()
        assert "@" not in text.split("services:")[1]
        replies.append(command)
        return SimpleNamespace(stdout=json.dumps({"services":{"knowledge":{
            "image":"boring-deploy-marker:render", "ports":[{"published":"18512","target":18512}]}}}))
    home = host/"not-created"
    ctx = deploy_context(home=home, release=SimpleNamespace(component="boring-knowledge",
        directory=directory), run=run)
    with pytest.raises(install.Failure, match="loopback"):
        install.plan(ctx, {"config_dir":str(host/"site-config")})
    assert replies and not home.exists() and not (host/"account").exists()


@pytest.mark.skipif(DOCKER is None, reason="Docker Compose parser is not installed")
@pytest.mark.parametrize("service", ["canvas", "knowledge", "sourcecache-d365", "fishmarket", "mcp-ferry"])
def test_actual_compose_parser_renders_the_same_install_template_without_a_home(host, monkeypatch, service):
    import subprocess
    directory = release(host, service)
    install = load(directory, host, monkeypatch, exposure=False)
    if service == "mcp-ferry":
        source = host / "site-config"
        (source / "one.json").write_text("{}")
        (source / "config.json").write_text(json.dumps({"backends":{"one":{
            "service":"one.json", "upstream":{"url":"http://backend.invalid/mcp"}}}}))
    home = host / "does-not-exist"
    def run(*command, **kwargs):
        # Use the installed CLI's parser, never Docker's daemon/build/run. The
        # rest of this fixture intentionally has fake Docker/user tools on PATH.
        assert command[:2] == ("docker", "compose") and command[-3:] == ("config", "--format", "json")
        return subprocess.run([DOCKER, *command[1:]], env={**__import__("os").environ,
            **kwargs.get("env", {})}, capture_output=True, text=True, check=True)
    ctx = deploy_context(home=home, release=SimpleNamespace(component="boring-"+service,
        directory=directory), run=run)
    settings = {"config_dir":str(host/"site-config"), "web_bind":"127.0.0.1",
                "public_url":"https://canvas.example.test", "edge_network":"phase20-fixture-proxy",
                "index":str(host/"fixture.db")}
    result = install._proposed_exposure(ctx, settings)
    assert ("proxy network edge" if service == "fishmarket" else "loopback") in result
    assert not home.exists()


def test_preflight_reads_compose_not_a_second_service_json_policy(tmp_path, monkeypatch):
    steps = load_steps()
    (tmp_path / "compose.yaml").write_text("fixture")
    config = tmp_path / "config/config.json"
    config.parent.mkdir()
    config.write_text('{"bind":"0.0.0.0"}')
    before = config.read_bytes()
    monkeypatch.setattr(steps, "_service", lambda ctx, image=None: ("managed", {
        "ports":[{"host_ip":"127.0.0.1","published":"18512","target":18512}]}))
    assert steps.preflight(deploy_context(home=tmp_path, release=SimpleNamespace(component="boring-knowledge"))) == []
    assert config.read_bytes() == before
