"""deploy/steps.py check: before the switch, the new image must reach the Crocodile from
the stack's network. The GET and docker are fakes; the classification is what is tested."""
import importlib.util
import inspect
import json
import socket
import ssl
import types
from pathlib import Path

import pytest

import boring_deploy

ROOT = Path(__file__).resolve().parents[1]
CROCODILE = "https://crocodile-url.example.test"


def load_steps():
    spec = importlib.util.spec_from_file_location("service_steps_reach", ROOT / "deploy" / "steps.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


steps = load_steps()


class ConnectError(Exception):
    """Stands in for httpx2.ConnectError, which chains the socket or ssl error."""


def failing(cause):
    def get(url):
        raise ConnectError(str(cause)) from cause
    return get


@pytest.mark.parametrize("status", [200, 301, 401, 404, 405, 500])
def test_any_http_answer_proves_name_path_and_tls(status):
    assert steps.crocodile_reach(CROCODILE, lambda url: status) is None


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_proxy_without_its_crocodile_is_not_reached(status):
    assert steps.crocodile_reach(CROCODILE, lambda url: status).startswith("connection: the proxy answers")


@pytest.mark.parametrize("cause, kind", [
    (socket.gaierror(-2, "Name or service not known"), "name"),
    (ssl.SSLCertVerificationError(1, "certificate verify failed: unable to get local issuer certificate"), "TLS"),
    (ConnectionRefusedError(111, "Connection refused"), "connection"),
    (TimeoutError("timed out"), "connection"),
    (OSError(113, "No route to host"), "connection"),
])
def test_no_answer_names_what_failed(cause, kind):
    assert steps.crocodile_reach(CROCODILE, failing(cause)).startswith(kind + ": ConnectError")


def test_its_source_runs_alone_as_in_the_image():
    namespace = {}
    exec(inspect.getsource(steps.crocodile_reach), namespace)
    assert namespace["crocodile_reach"](CROCODILE, failing(socket.gaierror(-2, "x"))).startswith("name:")


def check(monkeypatch, answer):
    calls, said = [], []

    def compose(ctx, *args, image=None, check=True):
        calls.append((args, image))
        return types.SimpleNamespace(stdout="Creating network\n" + json.dumps(answer) + "\n")
    monkeypatch.setattr(steps, "_compose", compose)
    ctx = types.SimpleNamespace(image="boring-canvas:9.9.9", log=said.append)
    steps._check_crocodile_reach(ctx, "canvas", "/run/config/config.json")
    return calls, said


def test_check_runs_in_the_new_image_on_the_stacks_network(monkeypatch):
    calls, said = check(monkeypatch, {"url": CROCODILE, "problem": None})
    (args, image), = calls
    assert image == "boring-canvas:9.9.9"
    assert args[:6] == ("run", "--rm", "--no-deps", "-T", "--entrypoint", "python")
    assert args[6] == "canvas" and args[-1] == "/run/config/config.json"
    assert said == [f"canvas: the new image reaches {CROCODILE} from the stack's network"]


def test_check_refuses_with_the_failed_part_and_the_readme(monkeypatch):
    with pytest.raises(boring_deploy.Failure, match="cannot reach the Crocodile.*name: ") as refused:
        check(monkeypatch, {"url": CROCODILE, "problem": "name: ConnectError: Name or service not known"})
    assert "README" in refused.value.hint


def test_a_config_without_a_crocodile_is_not_checked(monkeypatch):
    calls, said = check(monkeypatch, {"url": None, "problem": None})
    assert said == []


@pytest.mark.parametrize("command, inside", [
    (["--config=/run/config/config.json"], "/run/config/config.json"),
    (["http", "--config", "/run/config/config.json"], "/run/config/config.json"),
    (["--state-dir=/data/state"], None),
])
def test_the_check_step_reaches_with_the_config_the_compose_file_names(monkeypatch, command, inside):
    # The step itself, not only its helper: a VM acceptance once found
    # check() using a name it had not bound.
    reached, ran, said = [], [], []
    monkeypatch.setattr(steps, "_service", lambda ctx, image=None: ("canvas", {"command": command}))
    monkeypatch.setattr(steps, "_check_crocodile_reach", lambda ctx, name, path: reached.append((name, path)))
    ctx = types.SimpleNamespace(image="boring-canvas:9.9.9", log=said.append,
                                release=types.SimpleNamespace(details={"module": "boring_canvas"}),
                                run=lambda *args, **kw: ran.append(args))
    steps.check(ctx)
    assert ran and ran[0][-1] == "import boring_canvas"
    assert said == ["canvas: compose renders with boring-canvas:9.9.9; boring_canvas imports"]
    assert reached == ([("canvas", inside)] if inside else [])
