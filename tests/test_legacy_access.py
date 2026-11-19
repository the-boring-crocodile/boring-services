"""Legacy roles become exact advice; no grants, configs or credentials change."""
import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr
from types import SimpleNamespace

import pytest
import boring_deploy
from test_deploy_config import candidate, ferry_config
from test_install import host, release, load, deploy  # noqa: F401
from test_deploy_smoke import load_steps

AUTHORITY = "https://authority.example.test"
ADMIN = "/opt/boring-crocodile/current/bin/boring-crocodile-admin"
MEMBERS = {"pond": "reader", "garden": "writer"}
COMMAND = f"{ADMIN} service-grants custom-name --callers garden=reader,pond=writer"


def old_config(path, client="svc-old", *, author=None):
    grants = [{"subject": "pond", "role": "writer"}, {"subject": "garden", "role": "reader"}]
    if author is not None:
        grants[0]["author"] = author
    path.write_text(json.dumps({"url": "https://service.example.test/mcp",
        "oauth": {"issuer": AUTHORITY + "/", "client_id": client, "secret_file": "do-not-read"},
        "access": grants, "bind": "0.0.0.0", "port": 18520}))
    return path


def run_check(tmp_path, monkeypatch, module, path, *, records=None):
    steps, ctx, calls, reached, said, image = candidate(monkeypatch, tmp_path, module, path)
    def capture(ctx, name, code, *args, **kwargs):
        monkeypatch.setattr(sys, "argv", ["-c", *args])
        output = io.StringIO()
        with redirect_stdout(output):
            exec(compile(code, "<candidate-legacy-check>", "exec"), {})
        calls.append(code)
        return SimpleNamespace(stdout=output.getvalue())
    monkeypatch.setattr(steps, "_in_candidate", capture)
    admin_calls = []
    def admin(args, **kwargs):
        admin_calls.append(args)
        assert args in (["list", "--json"], ["service-list"])
        return ({"public_base_url": AUTHORITY} if args[0] == "list" else
                records if records is not None else [{"name": "custom-name", "client_id": "svc-old", "callers": MEMBERS}])
    monkeypatch.setitem(sys.modules, "install_steps", SimpleNamespace(CROCODILE=ADMIN, _admin_json=admin))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(boring_deploy.Failure) as stopped:
        steps.check(ctx)
    assert not reached
    assert all(p.read_bytes() == data for p, data in before.items())
    return str(stopped.value), admin_calls


@pytest.mark.parametrize("module", ["boring_canvas", "boring_knowledge",
    "boring_fishmarket", "boring_sourcecache_d365"])
def test_check_prints_complete_grant_from_access_for_custom_service_name(tmp_path, monkeypatch, module):
    path = old_config(tmp_path / "config.json")
    message, calls = run_check(tmp_path, monkeypatch, module, path)
    assert "Preview: " + COMMAND in message
    assert "Apply:   " + COMMAND + " --apply" in message
    assert "type APPLY" in message and "harmless write" in message
    assert "Nothing was applied" in message
    assert calls == [["list", "--json"], ["service-list"]]


@pytest.mark.parametrize("author,custom", [
    ('["https://authority.example.test","pond"]', False), ("old-canvas-owner", True)])
def test_custom_author_is_named_as_an_explicit_data_decision(tmp_path, monkeypatch, author, custom):
    message, _ = run_check(tmp_path, monkeypatch, "boring_canvas",
                           old_config(tmp_path / "config.json", author=author))
    assert COMMAND in message
    assert ("explicit decision" in message) is custom
    if custom:
        assert "old-canvas-owner" in message and "pond" in message and "stranded" in message


def test_ferry_prints_every_selected_frontend_and_ignores_unselected(tmp_path, monkeypatch):
    routing, selected = ferry_config(tmp_path, "mixed")
    for i, path in enumerate(selected):
        old_config(path, client=f"svc-{i}")
    (tmp_path / "unused.json").write_text("not JSON and not selected")
    records = [{"name": f"registered-{i}", "client_id": f"svc-{i}", "callers": MEMBERS} for i in range(2)]
    message, _ = run_check(tmp_path, monkeypatch, "boring_mcp_ferry", routing, records=records)
    for i in range(2):
        assert f"service-grants registered-{i} --callers garden=reader,pond=writer" in message


@pytest.mark.parametrize("arguments", [[], ["--check"]])
def test_real_deploy_and_check_cli_refuse_with_grant_before_switch(host, monkeypatch, arguments):
    import boring_deploy
    directory = release(host, "knowledge")
    installer = load(directory, host, monkeypatch)
    home = host / "knowledge"
    assert deploy(directory, installer, str(home), "--install",
                  "--config-dir", str(host / "site-config"))[0] == 0
    path = home / "config/config.json"
    path.chmod(0o600)  # fixture operator edits the installed read-only file
    old_config(path)
    path.chmod(0o400)
    before = (home / "boring-image.env").read_bytes()
    calls_before = (host / "calls.log").read_text()
    steps = load_steps()
    service = {"command": ["http", "--config", str(path)],
               "ports": [{"host_ip": "127.0.0.1", "published": "18512", "target": 18512}]}
    monkeypatch.setattr(steps, "_service", lambda ctx, image=None: ("managed", service))
    def capture(ctx, name, code, *args, **kwargs):
        assert ctx.image is None  # installed image, before any build/preparation
        monkeypatch.setattr(sys, "argv", ["-c", *args])
        output = io.StringIO()
        with redirect_stdout(output):
            exec(compile(code, "<candidate-cli>", "exec"), {})
        return SimpleNamespace(stdout=output.getvalue())
    monkeypatch.setattr(steps, "_in_candidate", capture)
    def child(command, **kwargs):
        action = command[1:]
        assert action in (["list", "--json"], ["service-list"])
        data = ({"public_base_url": AUTHORITY} if action[0] == "list" else
                [{"name": "custom-name", "client_id": "svc-old", "callers": MEMBERS}])
        return SimpleNamespace(returncode=0, stdout=json.dumps(data))
    monkeypatch.setattr(installer, "root_child", child)
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        status = boring_deploy.main(directory, steps, [str(home), *arguments], install=installer)
    assert status == 1
    assert COMMAND in output.getvalue()
    assert (home / "boring-image.env").read_bytes() == before
    assert "docker build" not in (host / "calls.log").read_text()[len(calls_before):]


@pytest.mark.parametrize("records", [[], [
    {"name": "one", "client_id": "svc-old"}, {"name": "two", "client_id": "svc-old"}]])
def test_missing_or_ambiguous_identity_never_guesses_a_service_name(tmp_path, monkeypatch, records):
    message, _ = run_check(tmp_path, monkeypatch, "boring_knowledge",
                           old_config(tmp_path / "config.json"), records=records)
    assert "svc-old" in message
    assert "service-grants " not in message
    assert "unmapped" in message or "ambiguous" in message


@pytest.mark.parametrize("extra", [{}, {"pirate/research": "reader"}])
def test_migration_will_not_reinstate_a_withdrawn_writer(tmp_path, monkeypatch, extra):
    path = old_config(tmp_path / "config.json")
    data = json.loads(path.read_text())
    data["access"][1]["role"] = "writer"
    path.write_text(json.dumps(data))
    records = [{"name": "custom-name", "client_id": "svc-old",
                "callers": {"pond": "reader", **extra}}]
    message, _ = run_check(tmp_path, monkeypatch, "boring_knowledge", path, records=records)
    assert "pond: suggested role writer (present in both)" in message
    assert "garden: local only; explicit decision required" in message
    if extra:
        assert "pirate/research: Crocodile only; explicit decision required" in message
    assert "service-grants " not in message


def test_default_author_keeps_published_issuer_spelling(tmp_path):
    steps = load_steps()
    path = old_config(tmp_path / "config.json")
    data = json.loads(path.read_text())
    issuer = "https://AUTHORITY.example.test:443"
    data["oauth"]["issuer"] = issuer + "/"
    data["access"][0]["author"] = json.dumps([issuer, "pond"], separators=(",", ":"))
    path.write_text(json.dumps(data))
    info = steps.legacy_access(path)
    assert info["authority_url"] == issuer
    assert info["aliases"] == []


@pytest.mark.parametrize("problem", ["bad-role", "foreign-issuer", "missing-client"])
def test_incomplete_or_foreign_grants_are_never_guessed(tmp_path, monkeypatch, problem):
    path = old_config(tmp_path / "config.json")
    data = json.loads(path.read_text())
    if problem == "bad-role":
        data["access"][1]["role"] = "admin"
    elif problem == "foreign-issuer":
        data["oauth"]["issuer"] = "https://elsewhere.example.test"
    else:
        del data["oauth"]["client_id"]
    path.write_text(json.dumps(data))
    message, _ = run_check(tmp_path, monkeypatch, "boring_knowledge", path)
    assert "service-grants " not in message
    assert "Nothing was applied" in message


def test_first_install_stopped_at_reach_can_retry_plain_deploy(host, monkeypatch):
    directory = release(host, "knowledge")
    installer = load(directory, host, monkeypatch)
    steps = load_steps()
    home = host / "knowledge"
    path = home / "config/config.json"
    (host / "site-config/config.json").write_text(json.dumps({
        "url": "https://service.example.test/mcp", "mode": "crocodile",
        "crocodile": {"url": AUTHORITY, "client_id": "svc-fixture",
                      "secret_file": "../credentials/mcp-client-secret"},
    }))
    authority_reachable = False
    run = boring_deploy.Context.run

    def docker(ctx, *command, **kwargs):
        if command[:2] == ("docker", "compose"):
            if command[-1] == "json" and "config" in command:
                image = kwargs["env"]["BORING_IMAGE"]
                return SimpleNamespace(returncode=0, stdout=json.dumps({"services": {"knowledge": {
                    "image": image, "command": ["http", "--config", str(path)],
                    "ports": [{"host_ip": "127.0.0.1", "target": 18520}],
                }}}))
            if "run" in command:
                if kwargs.get("env") is None and not (home / "boring-image.env").exists():
                    raise boring_deploy.Failure("neither an image nor a build context")
                code = command[command.index("-c") + 1]
                if "answers =" in code:
                    answer = {"url": AUTHORITY, "problem": None if authority_reachable else "name: fixture DNS"}
                    return SimpleNamespace(stdout=json.dumps({"frontends": [answer]}))
                monkeypatch.setattr(sys, "argv", ["-c", *command[command.index("-c") + 2:]])
                output = io.StringIO()
                with redirect_stdout(output):
                    exec(compile(code, "<candidate-retry>", "exec"), {})
                return SimpleNamespace(stdout=output.getvalue())
        return run(ctx, *command, **kwargs)

    monkeypatch.setattr(boring_deploy.Context, "run", docker)
    monkeypatch.setattr(steps, "smoke", lambda ctx: None)

    def deploy_cli(*arguments):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            status = boring_deploy.main(directory, steps, [str(home), *arguments], install=installer)
        return status, output.getvalue()

    status, output = deploy_cli("--install", "--config-dir", str(host / "site-config"))
    assert status == 1 and "FAILED at check" in output and "cannot reach the Crocodile" in output, output
    assert "run the deploy again" in output
    assert path.is_file() and not (home / "boring-image.env").exists()
    assert not (home / "release").exists()
    authority_reachable = True

    status, output = deploy_cli()
    assert status == 0, output
    assert "configuration validates" in output and "reaches" in output and "7 record" in output
    assert (home / "boring-image.env").read_text() == "BORING_IMAGE=boring-knowledge:1.0.0\n"
    assert (home / "release/VERSION").read_text().strip() == "1.0.0"
