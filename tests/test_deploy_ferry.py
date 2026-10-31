"""deploy/steps.py for stacks with more than one container (the ferry and its backends):
the deploy's own service is the one with image ${BORING_IMAGE}; a ferry's smoke checks
every backend's endpoint and registration. Docker and HTTP are fakes."""
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

import boring_deploy
from test_deploy_smoke import METADATA, RIGHT, STARTED, URL, load_steps, web


@pytest.fixture
def steps(monkeypatch):
    module = load_steps()
    monkeypatch.setattr(module, "SMOKE_SECONDS", 0)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    return module


def rendered(services):
    return types.SimpleNamespace(stdout=json.dumps({"services": services}))


def test_the_deploys_service_is_the_one_with_its_image(steps, monkeypatch):
    def compose(ctx, *args, image=None, check=True):
        assert args[:1] == ("config",)
        # The stand-in names the ferry's own built image literally: only ${BORING_IMAGE}
        # (here rendered as `image`) marks the deploy's service.
        return rendered({"ferry": {"image": image, "command": ["http"]},
                         "standin": {"image": "boring-mcp-ferry:0.1.0"},
                         "homeassistant": {"image": "ghcr.io/example/ha-mcp@sha256:abc"}})
    monkeypatch.setattr(steps, "_compose", compose)
    assert steps._service(None)[0] == "ferry"
    name, service = steps._service(None, image="boring-mcp-ferry:0.1.0")
    assert (name, service["image"]) == ("ferry", "boring-mcp-ferry:0.1.0")


def test_a_stack_without_exactly_one_deploy_service_is_refused(steps, monkeypatch):
    monkeypatch.setattr(steps, "_compose", lambda ctx, *a, image=None, check=True: rendered(
        {"a": {"image": "x"}, "b": {"image": "y"}}))
    with pytest.raises(boring_deploy.Failure, match="exactly one service"):
        steps._service(None)


def ferry_smoke(steps, monkeypatch, logs, pond=True):
    front = {"url": URL, **({"pond": {"url": "https://x"}} if pond else {})}
    config = {"backends": {"homeassistant": {"service": "services/homeassistant.json", "upstream": {"url": "http://h/mcp"}}}}
    read = []

    def compose(ctx, *args, **kwargs):
        if args[0] == "exec" and "-c" in args:
            read.append("/run/config/services/homeassistant.json")
            return types.SimpleNamespace(stdout=json.dumps({"homeassistant": front}))
        if args[0] == "exec":
            read.append(args[-1])
            return types.SimpleNamespace(stdout=json.dumps(config if args[-1].endswith("config.json") else front))
        if args[0] == "ps":
            return types.SimpleNamespace(stdout="0123abcd\n")
        if args[0] == "logs":
            return types.SimpleNamespace(stdout=logs)
        raise AssertionError(args)

    monkeypatch.setattr(steps, "_service", lambda ctx: ("ferry", {"command": ["http", "--config=/run/config/config.json"]}))
    monkeypatch.setattr(steps, "_compose", compose)
    monkeypatch.setattr(steps.urllib.request, "urlopen", web(RIGHT))
    said = []
    steps.smoke(types.SimpleNamespace(home=Path("/opt/docker/boring-mcp-ferry"), log=said.append,
                                      run=lambda *c, **k: types.SimpleNamespace(stdout=STARTED + "\n")))
    return said, read


FERRY_LOG = (f"2026-10-18T10:00:03.000000000Z 2026-10-18 10:00:03,000 boring_mcp_ferry INFO "
             f"MCP registration of homeassistant: registered\n")


def test_a_ferry_smoke_checks_each_backend(steps, monkeypatch):
    said, read = ferry_smoke(steps, monkeypatch, FERRY_LOG)
    assert read == ["/run/config/config.json", "/run/config/services/homeassistant.json"]
    assert said == [f"homeassistant: {URL} is its MCP endpoint (401 naming {METADATA})",
                    f"Pond: homeassistant registered by ferry (started {STARTED})"]


def test_a_ferry_backend_without_registration_fails(steps, monkeypatch):
    other = FERRY_LOG.replace("homeassistant", "nextcloud")
    with pytest.raises(boring_deploy.Failure, match="no Pond registration of homeassistant"):
        ferry_smoke(steps, monkeypatch, other)


def test_the_reach_check_reads_a_ferrys_backend_frontend(tmp_path):
    steps = load_steps()
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "a.json").write_text(json.dumps({"pond": {"url": "https://croc.example.test"}}))
    (tmp_path / "config.json").write_text(json.dumps({"backends": {"a": {"service": "services/a.json", "upstream": {"url": "http://a/mcp"}}}}))
    captured = {}

    def compose(ctx, *args, image=None, check=True):
        captured["code"] = args[args.index("-c") + 1]
        return types.SimpleNamespace(stdout="")
    steps._compose = compose
    with pytest.raises(Exception):
        steps._check_crocodile_reach(types.SimpleNamespace(image="i", log=print), "ferry", str(tmp_path / "config.json"))
    code = captured["code"].replace("import httpx2", "import types as httpx2") + "\n"
    source = str(Path(__file__).resolve().parents[1] / "mcp-ferry/src")
    stub = ("import sys, types\nsys.path.insert(0, " + repr(source) + ")\n"
            "types.get = lambda u, **k: types.SimpleNamespace(status_code=401)\n")
    result = subprocess.run([sys.executable, "-I", "-c", stub + code, str(tmp_path / "config.json")],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "frontends": [{"frontend": "a", "url": "https://croc.example.test", "problem": None}]}


def test_the_reach_check_drops_fixed_addresses_for_its_one_off_container():
    steps = load_steps()
    assert steps._without_fixed_addresses("canvas", {"networks": {"default": None}}) is None
    override = steps._without_fixed_addresses("ferry", {"networks": {
        "default": {"ipv4_address": "172.16.46.2"}, "homeassistant": None}})
    assert override == ('services:\n  "ferry":\n    networks: !override\n'
                        '      "default": {}\n      "homeassistant": {}\n')
