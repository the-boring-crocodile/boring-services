"""Candidate-image configuration checks; Docker is fake, generated parsing code is real."""
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import boring_deploy

from deploy_context import deploy_context
from test_deploy_smoke import load_steps


def service_config(path, *, problem=None, issuer=False):
    value = {"url": "https://service.example.test/mcp", "port": 18520,
             "mode": "issuer" if issuer else "crocodile"}
    if issuer:
        value |= {"issuer": {"subject": "owner", "database": "private/oauth.sqlite3",
                              "approval_secret_file": "approval", "role": "writer"}}
    else:
        (path.parent / "secret").write_text("synthetic-secret")
        value["crocodile"] = {"url": "https://authority.example.test",
            "client_id": "synthetic-client", "secret_file": "secret"}
    if problem == "unknown":
        value["typo"] = True
    elif problem == "bad-auth":
        value["crocodile"]["client_id"] = ""
    elif problem == "bad-port":
        value["port"] = True
    elif problem == "missing-secret":
        value["crocodile"]["secret_file"] = "not-present"
    path.write_text(json.dumps(value))
    return path


def candidate(monkeypatch, tmp_path, module, path, *, fixed=False):
    steps = load_steps()
    service = {"command": ["http", "--config=" + str(path)],
               "ports": [{"host_ip": "127.0.0.1", "published": "18520", "target": 18520}]}
    if fixed:
        service["networks"] = {"default": {"ipv4_address": "172.16.46.2"}, "backend": None}
    monkeypatch.setattr(steps, "_service", lambda ctx, image=None: ("managed", service))
    calls, reached, said = [], [], []
    monkeypatch.setattr(steps, "_check_crocodile_reach",
                        lambda *args: reached.append(args))
    ctx = deploy_context(home=tmp_path, image="candidate:test",
        release=SimpleNamespace(details={"module": module}), log=said.append)
    image_env = tmp_path / "boring-image.env"
    image_env.write_text("BORING_IMAGE=installed:old\n")

    def compose(ctx, *args, image=None, check=True, files=()):
        assert args[:6] == ("run", "--rm", "--no-deps", "-T", "--entrypoint", "python")
        assert args[6] == "managed" and image == "candidate:test"
        assert "--service-ports" not in args
        override = [Path(p).read_text() for p in files]
        calls.append((args, image, override))
        index = args.index("-c")
        # The exact code/argv prepared for the candidate; no server or HTTP client runs.
        monkeypatch.setattr(sys, "argv", ["-c", *args[index + 2:]])
        try:
            exec(compile(args[index + 1], "<candidate-check>", "exec"), {})
        except (OSError, ValueError, KeyError, SystemExit) as error:
            raise boring_deploy.Failure(f"candidate configuration: {error}") from error
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(steps, "_compose", compose)
    return steps, ctx, calls, reached, said, image_env


@pytest.mark.parametrize("module", [
    "boring_canvas", "boring_knowledge", "boring_fishmarket", "boring_sourcecache_d365"])
@pytest.mark.parametrize("problem", ["unknown", "bad-auth", "bad-port", "missing-secret"])
def test_invalid_config_stops_candidate_check_before_reach_or_switch(tmp_path, monkeypatch, module, problem):
    path = service_config(tmp_path / "config.json", problem=problem)
    steps, ctx, calls, reached, said, image_env = candidate(monkeypatch, tmp_path, module, path)
    before = image_env.read_bytes()
    with pytest.raises(boring_deploy.Failure):
        steps.check(ctx)
    assert len(calls) == 1 and not reached
    assert image_env.read_bytes() == before
    assert not any("configuration validates" in message for message in said)


@pytest.mark.parametrize("issuer", [False, True])
def test_valid_candidate_config_is_checked_without_opening_state(tmp_path, monkeypatch, issuer):
    path = service_config(tmp_path / "config.json", issuer=issuer)
    steps, ctx, calls, reached, said, image_env = candidate(
        monkeypatch, tmp_path, "boring_knowledge", path)
    monkeypatch.setattr(sqlite3, "connect", Mock(side_effect=AssertionError("opened live issuer state")))
    steps.check(ctx)
    assert len(calls) == 1 and len(reached) == 1
    assert any("configuration validates" in message for message in said)
    assert not (tmp_path / "private").exists()
    assert image_env.read_text() == "BORING_IMAGE=installed:old\n"
    assert ctx.run.call_args.args[:6] == (
        "docker", "run", "--rm", "--entrypoint", "python", "candidate:test")


def ferry_config(tmp_path, mode):
    backends = {"backend": {"upstream": {"url": "http://backend.invalid/mcp"}}}
    value = {"backends": backends}
    selected = []
    if mode != "gateway":
        selected.append(service_config(tmp_path / "backend.json"))
        backends["backend"]["service"] = "backend.json"
    if mode != "backend":
        selected.append(service_config(tmp_path / "gateway.json"))
        value["gateways"] = {"shared": {"service": "gateway.json", "backends": ["backend"]}}
    for index, frontend in enumerate(selected):
        data = json.loads(frontend.read_text())
        data.update(url=f"https://frontend-{index}.example.test/mcp", port=18520 + index)
        data["crocodile"]["client_id"] = f"frontend-{index}"
        frontend.write_text(json.dumps(data))
    (tmp_path / "unused.json").write_text("deliberately invalid, unselected")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    return path, selected


@pytest.mark.parametrize("mode", ["backend", "gateway", "mixed"])
def test_ferry_validates_all_and_only_selected_frontends(tmp_path, monkeypatch, mode):
    path, selected = ferry_config(tmp_path, mode)
    steps, ctx, calls, reached, said, _ = candidate(
        monkeypatch, tmp_path, "boring_mcp_ferry", path, fixed=True)
    steps.check(ctx)
    assert len(calls) == 1 and len(reached) == 1
    assert calls[0][2] == ['services:\n  "managed":\n    networks: !override\n'
                          '      "default": {}\n      "backend": {}\n']
    bad = json.loads(selected[-1].read_text())
    bad["unknown"] = True
    selected[-1].write_text(json.dumps(bad))
    calls.clear()
    reached.clear()
    with pytest.raises(boring_deploy.Failure):
        steps.check(ctx)
    assert len(calls) == 1 and not reached


def test_ferry_routing_parser_is_used_before_frontend_validation(tmp_path, monkeypatch):
    path, _ = ferry_config(tmp_path, "gateway")
    config = json.loads(path.read_text())
    config["gateways"]["shared"]["backends"] = ["not-configured"]
    path.write_text(json.dumps(config))
    steps, ctx, calls, reached, said, _ = candidate(
        monkeypatch, tmp_path, "boring_mcp_ferry", path)
    with pytest.raises(boring_deploy.Failure, match="no backend"):
        steps.check(ctx)
    assert len(calls) == 1 and not reached
