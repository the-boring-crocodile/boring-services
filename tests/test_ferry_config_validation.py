"""Ferry's shared startup/candidate frontend constraints, without Docker or network."""
import json
import sqlite3
from unittest.mock import Mock

import pytest
import boring_deploy

from boring_mcp_ferry import server
from boring_mcp_ferry.config import ConfigError
from test_deploy_config import candidate, service_config


def configured(tmp_path, mode="mixed", *, issuer=False):
    paths = [service_config(tmp_path / (name + ".json"), issuer=issuer) for name in ("a", "b")]
    for index, path in enumerate(paths):
        data = json.loads(path.read_text())
        data.update(url=f"https://frontend-{index}.example.test/mcp", port=18520 + index)
        if not issuer:
            data["crocodile"]["client_id"] = f"client-{index}"
        path.write_text(json.dumps(data))
    data = {"backends": {name: {"upstream": {"url": f"http://{name}.invalid/mcp"}}
                         for name in ("one", "two")}}
    if mode == "backends":
        for name, path in zip(("one", "two"), paths):
            data["backends"][name]["service"] = path.name
    elif mode == "gateways":
        data["gateways"] = {name: {"service": path.name, "backends": [backend]}
                            for name, path, backend in zip(("a", "b"), paths, ("one", "two"))}
    else:
        data["backends"]["one"]["service"] = paths[0].name
        data["gateways"] = {"shared": {"service": paths[1].name, "backends": ["two"]}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path, paths


def break_config(paths, problem):
    first, second = [json.loads(p.read_text()) for p in paths]
    if problem == "client_id":
        second["crocodile"]["client_id"] = first["crocodile"]["client_id"]
    elif problem in ("port", "URL"):
        key = "url" if problem == "URL" else "port"
        second[key] = first[key]
    else:
        second.update(bind=problem, behind_proxy=True)
    paths[1].write_text(json.dumps(second))
    return "same " + problem if problem in ("port", "URL", "client_id") else "not every address"


@pytest.mark.parametrize("mode", ["backends", "gateways", "mixed"])
@pytest.mark.parametrize("problem", ["port", "URL", "client_id", "0.0.0.0", "::", "0", "0.0"])
def test_candidate_rejects_ferry_frontend_conflicts_before_reach(tmp_path, monkeypatch, mode, problem):
    path, paths = configured(tmp_path, mode)
    message = break_config(paths, problem)
    steps, ctx, calls, reached, said, image_env = candidate(
        monkeypatch, tmp_path, "boring_mcp_ferry", path)
    with pytest.raises(boring_deploy.Failure, match=message):
        steps.check(ctx)
    assert len(calls) == 1 and not reached
    assert image_env.read_text() == "BORING_IMAGE=installed:old\n"


@pytest.mark.parametrize("problem", ["port", "URL", "client_id", "0.0.0.0"])
def test_startup_checks_frontend_constraints_before_creating_upstreams(tmp_path, monkeypatch, problem):
    path, paths = configured(tmp_path)
    message = break_config(paths, problem)
    upstream = Mock(side_effect=AssertionError("created HTTP client before validating config"))
    monkeypatch.setattr(server, "Upstream", upstream)
    with pytest.raises(ConfigError, match=message):
        server.prepare(path, None, False)
    upstream.assert_not_called()


@pytest.mark.parametrize("mode", ["backends", "gateways", "mixed"])
def test_ferry_validation_has_no_issuer_client_or_app_effects(tmp_path, monkeypatch, mode):
    path, paths = configured(tmp_path, mode, issuer=True)
    forbidden = Mock(side_effect=AssertionError("runtime effect during validation"))
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    for name in ("Upstream", "front", "gateway_front", "CallLog"):
        monkeypatch.setattr(server, name, forbidden)
    assert server.validate(path) is None
    forbidden.assert_not_called()
    assert not (tmp_path / "private").exists()
