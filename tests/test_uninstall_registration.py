"""Uninstall revokes only installed Crocodile client identities, after stop."""
import json
from types import SimpleNamespace

import pytest
from test_install import host, release, load, deploy, uninstall, rendered  # noqa: F401
from test_install_registration import PUBLIC, URL


@pytest.mark.parametrize("case", ["active", "parked", "already-removed", "other-client", "keys", "declined", "other-authority"])
def test_uninstall_matches_installed_identity_before_revoking_and_keeps_files(host, monkeypatch, case):
    directory = release(host, "knowledge")
    steps = load(directory, host, monkeypatch)
    home = host / "boring-knowledge"
    assert deploy(directory, steps, str(home), "--install", "--config-dir", str(host/"site-config"))[0] == 0
    rendered(home, host)
    info = {"url": URL, "authority_url": PUBLIC, "client_id": "svc_fixture"}
    if case == "keys":
        info.update(authority_url=None, client_id=None)
    if case == "other-authority":
        info["authority_url"] = "https://other.example.test"
    monkeypatch.setattr(steps, "_installed_frontends", lambda ctx: {"": info}, raising=False)
    before = (home/"config/config.json").read_bytes()
    records = [] if case == "already-removed" else [{"name": "custom-name", "url": URL,
        "client_id": "svc_other" if case == "other-client" else "svc_fixture",
        "callers": {"pond": "reader"}, "roles": ["reader"], "default_role": "reader", "ping_timeout": 5}]
    calls = []
    def child(command, **options):
        calls.append(command[1:])
        action = command[1]
        if action == "list":
            out = {"public_base_url": PUBLIC}
        elif action == "service-list":
            out = list(records)
        else:
            assert command[2] == "custom-name"
            assert json.loads(command[command.index("--expected-definition")+1]) == records[0]
            if "--apply" in command:
                assert " down" in (host/"calls.log").read_text()
                if case == "declined":
                    return SimpleNamespace(returncode=2, stdout="")
                records.clear()
                out = {"status": "removed", "name": "custom-name"}
            else:
                out = {"name": "custom-name", "plan_digest": "fixture"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(out))
    monkeypatch.setattr(steps, "root_child", child, raising=False)
    if case == "parked":
        (host/"stack").write_text('{"Service":"knowledge","State":"exited","Health":""}')
    code, text = uninstall(directory, steps, str(home), "--yes", "--delete-data")
    if case in ("other-authority", "declined"):
        assert code == 1 and (home/"compose.yaml").exists()
        assert records and "userdel" not in (host/"calls.log").read_text()
    else:
        assert code == 0, text
        assert not (home/"compose.yaml").exists()
    assert (home/"config/config.json").read_bytes() == before
    if case in ("active", "parked"):
        assert not records and any("--apply" in c for c in calls)
    elif case in ("other-client", "keys", "already-removed"):
        assert not any(c[0] == "service-remove" for c in calls)


def test_installed_metadata_uses_owner_without_starting_services_or_publishing_ports(host, monkeypatch):
    import importlib
    import io
    import sys
    from contextlib import redirect_stdout
    directory = release(host, "knowledge")
    install = load(directory, host, monkeypatch, metadata=False)
    owner = importlib.import_module("steps")
    path = host / "keys-config.json"
    path.write_text(json.dumps({"url": URL, "mode": "keys", "keys": {"file": "keys.json"}}))
    service = {"command": ["--config", str(path)],
               "networks": {"default": {"ipv4_address": "172.16.43.2"}}}
    monkeypatch.setattr(owner, "_service", lambda ctx: ("managed", service))
    calls = []
    def compose(ctx, *args, image=None, files=(), **kwargs):
        assert args[:4] == ("run", "--rm", "--no-deps", "-T")
        assert args[args.index("--user")+1] == "0:0"
        assert args[args.index("--cap-add")+1] == "DAC_READ_SEARCH"
        assert image is None and "--service-ports" not in args
        assert files and "!override" in files[0].read_text()
        assert "ipv4_address" not in files[0].read_text()
        calls.append(args)
        index = args.index("-c")
        monkeypatch.setattr(sys, "argv", ["-c", *args[index+2:]])
        out = io.StringIO()
        with redirect_stdout(out):
            exec(compile(args[index+1], "<installed-owner-metadata>", "exec"), {})
        return SimpleNamespace(stdout=out.getvalue())
    monkeypatch.setattr(owner, "_compose", compose)
    from deploy_context import deploy_context
    info = install._installed_frontends(deploy_context(home=host/"home", image=None))
    assert len(calls) == 1 and info[""]["url"] == URL
    assert info[""]["client_id"] is None and info[""]["authority_url"] is None


@pytest.mark.parametrize("failure", ["seal", "userdel"])
def test_retry_keeps_installed_reader_and_reads_partly_sealed_identity(host, monkeypatch, failure):
    """Real config owner; Docker/root-UID effects are modelled, never executed."""
    import importlib
    import io
    import os
    import sys
    from contextlib import redirect_stdout
    import boring_deploy
    directory = release(host, "knowledge")
    steps = load(directory, host, monkeypatch, metadata=False)
    owner = importlib.import_module("steps")
    home = host / "retry-home"
    for part in ("config", "credentials", "state", "release"):
        (home/part).mkdir(parents=True, exist_ok=True)
    (home/"compose.yaml").write_text("services: {managed: {image: ${BORING_IMAGE}}}")
    (home/"boring-image.env").write_text("BORING_IMAGE=boring-knowledge:1.0.0\n")
    (home/"credentials/mcp-client-secret").write_text("fixture-private-only")
    path = home/"config/config.json"
    path.write_text(json.dumps({"url": URL, "mode": "crocodile",
        "crocodile": {"url": PUBLIC, "client_id": "svc_fixture",
                      "secret_file": "../credentials/mcp-client-secret"}}))
    state = {"image": True, "sealed": False, "failed": False, "root_read": False, "removed": 0}
    records = [{"name": "custom", "client_id": "svc_fixture", "url": URL,
                "callers": {}, "roles": ["reader"], "default_role": "reader", "ping_timeout": 5}]
    events = []
    real_read = type(path).read_text
    def guarded_read(source, *args, **kwargs):
        # Models root-owned0700 ancestors and account-owned0400 credential:
        # the service reader loses access after sealing. The dedicated metadata
        # profile must request root UID and read/search capability.
        if str(source).startswith(str(home)) and state["sealed"] and not state["root_read"]:
            raise PermissionError("fixture: service UID cannot read sealed identity")
        return real_read(source, *args, **kwargs)
    monkeypatch.setattr(type(path), "read_text", guarded_read)
    monkeypatch.setattr(owner, "_service", lambda ctx: ("managed", {
        "command": ["--config", str(path)], "networks": {"default": {"ipv4_address": "172.16.43.2"}}}))
    def compose(ctx, *args, image=None, files=(), **kwargs):
        assert state["image"] and (home/"boring-image.env").exists(), "installed metadata image lost before retry"
        assert args[0] == "run" and "--service-ports" not in args and "--no-deps" in args
        state["root_read"] = ("--user" in args and args[args.index("--user")+1] == "0:0"
                              and "--cap-add" in args and args[args.index("--cap-add")+1] == "DAC_READ_SEARCH")
        index = args.index("-c")
        monkeypatch.setattr(sys, "argv", ["-c", *args[index+2:]])
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                exec(compile(args[index+1], "<sealed-owner-metadata>", "exec"), {})
        finally:
            state["root_read"] = False
        events.append("metadata")
        return SimpleNamespace(stdout=out.getvalue())
    monkeypatch.setattr(owner, "_compose", compose)
    def child(command, **options):
        if command[1] == "list":
            result = {"public_base_url": PUBLIC}
        elif command[1] == "service-list":
            result = list(records)
        elif "--apply" in command:
            records.clear()
            state["removed"] += 1
            result = {"status": "removed", "name": "custom"}
        else:
            result = {"plan_digest": "fixture", "name": "custom"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(result))
    monkeypatch.setattr(steps, "root_child", child)
    real_seal = steps.seal
    def seal(ctx, kept, uid):
        if failure == "seal" and not state["failed"]:
            real_seal(ctx, [home/"config", home/"credentials"], uid)
            state.update(sealed=True, failed=True)
            raise OSError("fixture: sealing interrupted")
        real_seal(ctx, kept, uid)
        state["sealed"] = True
    monkeypatch.setattr(steps, "seal", seal)
    ctx = boring_deploy.Context(SimpleNamespace(component="boring-knowledge",
        details={"image": "boring-knowledge"}), home, "1.0.0", events.append)
    def run(*command, **kwargs):
        events.append(command)
        if command[:3] == ("docker", "image", "ls"):
            return SimpleNamespace(returncode=0, stdout="1.0.0")
        if command[:3] == ("docker", "image", "rm"):
            state["image"] = False
        if command[0] == "userdel" and failure == "userdel" and not state["failed"]:
            state["failed"] = True
            raise steps.Failure("fixture: userdel interrupted")
        return SimpleNamespace(returncode=0, stdout="")
    ctx.run = run
    with pytest.raises((OSError, steps.Failure), match="interrupted"):
        steps.uninstall(ctx, True)
    assert state["image"] and (home/"boring-image.env").exists() and (home/"release").exists()
    assert not records and (home/"compose.yaml").exists()
    steps.uninstall(ctx, True)
    assert not (home/"compose.yaml").exists() and (home/"compose.yaml.uninstalled").exists()
    assert state["removed"] == 1 and events.count("metadata") == 2
    assert (home/"config/config.json").is_file() and (home/"credentials/mcp-client-secret").is_file()
