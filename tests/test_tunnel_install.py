"""Optional tunnel effects on fixtures only; no host accounts, SSH or Crocodile."""
import importlib.util
import json
import os
import socket
from functools import partial
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import boring_deploy
from boring_deploy import install as engine_install
from test_tunnel_model import model

sys.modules["boring_install"] = engine_install
sys.modules["tunnel_model"] = model
SOURCE = Path(__file__).resolve().parents[1] / "tunnel/deploy/install.py"
spec = importlib.util.spec_from_file_location("tunnel_install", SOURCE)
install = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = install
spec.loader.exec_module(install)
check_free_ports = install.require_free_ports


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "root_input", partial(boring_deploy.root_input, owner=os.getuid()))
    monkeypatch.setattr(install, "_directory_fd", partial(install._directory_fd, owner=os.getuid()))
    ssh = tmp_path / "sshd_config"
    dropins = tmp_path / "sshd_config.d"
    dropins.mkdir()
    ssh.write_text(f"# operator fixture\nInclude \"{dropins}/*.conf\"\n")
    ssh.chmod(0o640)
    monkeypatch.setattr(install, "SSH_CONFIG", ssh)
    key = tmp_path / "box.pub"
    key.write_text("ssh-ed25519 c3ludGhldGlj fixture\n")
    # The fixture uses symbolic example ports, never the host's service ports.
    monkeypatch.setattr(install, "require_free_ports", lambda config: None)
    events, records = [], []
    protected_ports = [18530, 18532]
    settings = dict(box="203.0.113.7", key=str(key), services="core=18530,d365=18532",
                    callers="pond=writer", ssh_host="ssh.example.test")
    identity = boring_deploy.AccountIdentity(901, 902)
    def create(*args):
        events.append(("create", args[:3]))
        return identity
    monkeypatch.setattr(install, "create_system_account", create)
    monkeypatch.setattr(install, "system_account", lambda *args: identity)
    monkeypatch.setattr(install, "lock_system_account", lambda *args: events.append(("lock", args[:4])))
    monkeypatch.setattr(os, "fchown", lambda *args: None)
    ctx = SimpleNamespace(home=tmp_path / "home", instance="box", changed=[], log=lambda text: None, active=False)
    def run(*args, **kwargs):
        events.append(("run", args))
        stdout = ""
        if args[:2] == ("sshd", "-T"):
            stdout = ("allowtcpforwarding remote\nallowstreamlocalforwarding no\npermitopen none\n"
                "maxsessions 0\npasswordauthentication no\nkbdinteractiveauthentication no\n"
                "gatewayports no\nusepam yes\npermitlisten 127.0.0.1:18530 127.0.0.1:18532\n"
                "authenticationmethods publickey\nauthorizedkeysfile .ssh/authorized_keys\n"
                "authorizedkeyscommand none\ntrustedusercakeys none\nallowagentforwarding no\n"
                "x11forwarding no\npermittty no\npermittunnel no\n")
        return SimpleNamespace(stdout=stdout, returncode=(0 if ctx.active else 1) if args[0] == "pgrep" else 0)
    ctx.run = run
    def admin(arguments, apply=False):
        events.append(("admin", list(arguments), apply))
        if arguments[0] == "list":
            return {"public_base_url": "https://crocodile.example.test"}
        if arguments[0] == "service-ports":
            return {"protected_service_ports": list(protected_ports)}
        if arguments[0] == "service-list":
            return list(records)
        if arguments[0] == "service-add":
            if not apply:
                return {"action": "service-add", "name": arguments[1]}
            service = {"name": arguments[1], "client_id": "svc_" + arguments[1],
                       "url": arguments[arguments.index("--url") + 1]}
            records.append(service)
            return {**service, "client_secret": "synthetic-private-value"}
        if arguments[0] == "service-remove":
            if apply:
                records[:] = [s for s in records if s["name"] != arguments[1]]
            return {"action": "service-remove"}
        pytest.fail(arguments)
    def admin_child(command, *, stdin, **kwargs):
        if os.fspath(command[0]) != "/opt/boring-crocodile/current/bin/boring-crocodile-admin":
            raise FileNotFoundError(command[0])
        data = admin(command[1:], apply=stdin is None)
        return SimpleNamespace(stdout=json.dumps(data), returncode=0)
    monkeypatch.setattr(install, "root_child", admin_child)
    return SimpleNamespace(ctx=ctx, settings=settings, events=events, records=records, ssh=ssh,
                           protected_ports=protected_ports,
                           dropin=dropins / "92-boring-tunnel-box.conf")


def run_install(host, monkeypatch):
    install.ready(host.ctx, host.settings)
    monkeypatch.setattr("builtins.input", lambda prompt: "APPLY")
    install.install(host.ctx, host.settings)


def test_plan_is_read_only_and_decline_changes_nothing(host, monkeypatch):
    original = host.ssh.read_bytes()
    install.ready(host.ctx, host.settings)
    plan = "\n".join(install.plan(host.ctx, host.settings))
    assert "service-add" in plan
    assert str(host.dropin) in plan
    assert not host.dropin.exists()
    assert not host.ctx.home.exists() and not host.records
    host.events.clear()
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    with pytest.raises(boring_deploy.Failure, match="declined"):
        install.install(host.ctx, host.settings)
    assert host.events == [] and host.ssh.read_bytes() == original


def test_apply_creates_identity_registers_roles_then_admits_key_and_exports_private_files(host, monkeypatch, capsys):
    main_config = host.ssh.read_bytes()
    main_stat = host.ssh.stat()
    run_install(host, monkeypatch)
    assert host.ssh.read_bytes() == main_config
    assert host.ssh.stat().st_ino == main_stat.st_ino
    assert host.dropin.read_text() == model.ssh_policy(host.ctx.tunnel_plan)
    assert install.look(host.ctx).installed
    site = install._site(host.ctx)
    config = json.loads((site / "config.json").read_text())
    assert config["identity"] == {"uid": 901, "gid": 902}
    assert "callers" not in config
    assert {r["name"] for r in host.records} == {"box", "box-d365"}
    added = [e for e in host.events if e[0] == "admin" and e[1][0] == "service-add" and e[2]]
    assert len(added) == 2
    for _, args, _ in added:
        assert args[args.index("--callers") + 1] == "pond=writer"
        assert args[args.index("--roles") + 1] == "reader,writer"
        assert args[args.index("--default-role") + 1] == "reader"
    windows = json.loads((site / "windows/settings.json").read_text())
    assert windows["crocodile_url"] == "https://crocodile.example.test"
    assert windows["ssh_host"] == "tunnel-box@ssh.example.test"
    assert (site / "windows/credentials/core-secret").stat().st_mode & 0o777 == 0o600
    assert (site / "ssh/.ssh/authorized_keys").read_text().startswith("restrict,port-forwarding,")
    assert host.ssh.stat().st_mode & 0o777 == 0o640
    assert "synthetic-private-value" not in capsys.readouterr().out


def test_uninstall_removes_grants_locks_identity_and_keeps_files(host, monkeypatch):
    run_install(host, monkeypatch)
    site = install._site(host.ctx)
    paths = {p for p in site.rglob("*")}
    dropin = host.dropin.read_bytes() if host.dropin.exists() else None
    host.events.clear()
    assert install.uninstall(host.ctx, False) == [str(site)]
    assert not host.records
    assert paths == {p for p in site.rglob("*")}
    assert (site / "ssh/.ssh/authorized_keys").read_text() == ""
    assert [e for e in host.events if e[0] == "lock"]
    assert not install.look(host.ctx).installed and install.look(host.ctx).retained
    removals = [e for e in host.events if e[0] == "admin" and e[1][0] == "service-remove" and e[2]]
    assert len(removals) == 2 and all("--expected-definition" in e[1] for e in removals)
    assert dropin is not None and host.dropin.read_bytes() == dropin
    install.uninstall(host.ctx, False)
    assert paths == {p for p in site.rglob("*")}
    assert host.dropin.read_bytes() == dropin


def test_active_tunnel_stays_revoked_and_no_grants_removed(host, monkeypatch):
    run_install(host, monkeypatch)
    host.ctx.active = True
    with pytest.raises(boring_deploy.Failure, match="still has processes"):
        install.uninstall(host.ctx, False)
    assert len(host.records) == 2
    assert (install._site(host.ctx) / "ssh/.ssh/authorized_keys").read_text() == ""


def test_uninstall_decline_keeps_admission_and_grants(host, monkeypatch):
    run_install(host, monkeypatch)
    path = install._site(host.ctx) / "ssh/.ssh/authorized_keys"
    original = path.read_bytes()
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    with pytest.raises(boring_deploy.Failure, match="declined"):
        install.uninstall(host.ctx, False)
    assert path.read_bytes() == original and len(host.records) == 2


def test_incomplete_key_admission_cannot_look_installed(host, monkeypatch):
    run_install(host, monkeypatch)
    (install._site(host.ctx) / "ssh/.ssh/authorized_keys").unlink()
    observed = install.look(host.ctx)
    assert not observed.installed and observed.irregular


def test_customized_key_is_retained(host, monkeypatch):
    run_install(host, monkeypatch)
    path = install._site(host.ctx) / "ssh/.ssh/authorized_keys"
    path.write_text("operator customization\n")
    host.events.clear()
    with pytest.raises(boring_deploy.Failure, match="changed"):
        install.uninstall(host.ctx, False)
    assert host.events == [] and len(host.records) == 2


def test_interrupted_registration_resumes_saved_identity_and_credentials(host, monkeypatch):
    original = install._admin
    def interrupted(arguments, apply=False):
        if arguments[:2] == ["service-add", "box-d365"] and apply:
            raise boring_deploy.Failure("synthetic interruption")
        return original(arguments, apply)
    monkeypatch.setattr(install, "_admin", interrupted)
    with pytest.raises(boring_deploy.Failure, match="synthetic interruption"):
        run_install(host, monkeypatch)
    assert (install._site(host.ctx) / "config.json").exists()
    assert not install.look(host.ctx).installed and not install.look(host.ctx).irregular
    monkeypatch.setattr(install, "_admin", original)
    dropin_stat = host.dropin.stat()
    run_install(host, monkeypatch)
    assert host.dropin.stat().st_ino == dropin_stat.st_ino
    assert install.look(host.ctx).installed
    assert [r["name"] for r in host.records] == ["box", "box-d365"]
    assert len([e for e in host.events if e[0] == "create"]) == 1


def test_lost_credential_reply_is_named_and_never_registered_again(host, monkeypatch):
    original = install._admin
    def lost(arguments, apply=False):
        answer = original(arguments, apply)
        if arguments[:2] == ["service-add", "box"] and apply:
            raise boring_deploy.Failure("synthetic lost reply")
        return answer
    monkeypatch.setattr(install, "_admin", lost)
    with pytest.raises(boring_deploy.Failure, match="lost reply"):
        run_install(host, monkeypatch)
    monkeypatch.setattr(install, "_admin", original)
    with pytest.raises(boring_deploy.Failure, match="one-time credential"):
        install.ready(host.ctx, host.settings)
    assert len(host.records) == 1
    assert len([e for e in host.events if e[0] == "create"]) == 1


def test_invalid_candidate_keeps_daemon_config_and_no_key_is_admitted(host, monkeypatch):
    original = host.ctx.run
    before = host.ssh.read_bytes()
    def invalid(*args, **kwargs):
        if args[:3] == ("sshd", "-t", "-f"):
            raise boring_deploy.Failure("synthetic invalid candidate")
        return original(*args, **kwargs)
    host.ctx.run = invalid
    with pytest.raises(boring_deploy.Failure, match="invalid candidate"):
        run_install(host, monkeypatch)
    assert host.ssh.read_bytes() == before and not host.records and not host.dropin.exists()
    assert not (install._site(host.ctx) / "ssh/.ssh/authorized_keys").exists()


@pytest.mark.parametrize("url", ["http://127.0.0.1:18530/mcp", "http://localhost:18530/other"])
def test_port_registered_to_another_service_refuses_before_effects(host, url):
    host.records.append({"name": "other", "client_id": "svc_other", "url": url})
    with pytest.raises(boring_deploy.Failure, match="18530.*other"):
        install.ready(host.ctx, host.settings)
    assert not host.ctx.home.exists() and len(host.records) == 1


@pytest.mark.parametrize("include", ["# Include {directory}/*.conf", "Include {directory}/91-*.conf"])
def test_install_refuses_without_include_for_its_dropin(host, include):
    host.ssh.write_text(include.format(directory=host.dropin.parent) + "\n")
    with pytest.raises(boring_deploy.Failure, match=r"Include.*sshd_config\.d"):
        install.ready(host.ctx, host.settings)
    assert not host.ctx.home.exists() and not host.dropin.exists() and not host.records


@pytest.mark.parametrize("file_kind", ["foreign", "same-bytes-foreign", "changed", "symlink"])
def test_install_refuses_customized_or_foreign_dropin(host, file_kind):
    if file_kind == "foreign":
        host.dropin.write_text("# operator-owned fixture\n")
    elif file_kind in {"changed", "same-bytes-foreign"}:
        value = model.declaration(name="box", box=host.settings["box"],
                                  services=host.settings["services"], callers=host.settings["callers"])
        policy = model.ssh_policy(value)
        host.dropin.write_text(policy.replace("MaxSessions 0", "MaxSessions 1") if file_kind == "changed" else policy)
    else:
        target = host.dropin.parent.parent / "outside.conf"
        target.write_text("# linked fixture\n")
        host.dropin.symlink_to(target)
    original = host.dropin.read_bytes()
    with pytest.raises(boring_deploy.Failure, match="customized"):
        install.ready(host.ctx, host.settings)
    assert host.dropin.read_bytes() == original
    assert not host.ctx.home.exists() and not host.records


@pytest.mark.parametrize("earlier_match", [False, True])
def test_staged_dropin_follows_effective_openssh_order(host, tmp_path, monkeypatch, earlier_match):
    import shutil
    import subprocess

    sshd, keygen = shutil.which("sshd"), shutil.which("ssh-keygen")
    if not sshd or not keygen:
        pytest.skip("OpenSSH tools unavailable")
    host_key = tmp_path / "fixture-host-key"
    subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)],
                   check=True, capture_output=True)
    with host.ssh.open("a") as config:
        config.write(f"HostKey {host_key}\nUsePAM yes\n")
    if earlier_match:
        (host.dropin.parent / "50-operator.conf").write_text(
            "Match User tunnel-box\n    AllowTcpForwarding yes\n")
    original_run = host.ctx.run

    def run(*args, **kwargs):
        original_run(*args, **kwargs)
        if args[0] == "sshd":
            config = () if "-f" in args else ("-f", str(host.ssh))
            reply = subprocess.run([sshd, *args[1:], *config], text=True, capture_output=True)
            assert reply.returncode == 0, reply.stderr
            return reply
        return SimpleNamespace(stdout="", returncode=0)

    host.ctx.run = run
    before = host.ssh.read_bytes()
    if earlier_match:
        with pytest.raises(boring_deploy.Failure, match="effective SSH policy differs for allowtcpforwarding"):
            run_install(host, monkeypatch)
        assert not host.dropin.exists()
        assert not any(event[:2] == ("run", ("systemctl", "reload", "ssh.service"))
                       for event in host.events)
        assert not (install._site(host.ctx) / "ssh/.ssh/authorized_keys").exists()
        assert not host.records
    else:
        run_install(host, monkeypatch)
        assert host.dropin.is_file()
        assert install.look(host.ctx).installed
    assert host.ssh.read_bytes() == before


def test_unprotected_tunnel_port_refuses_before_account_or_registration(host):
    host.protected_ports.remove(18532)
    with pytest.raises(boring_deploy.Failure, match="box-d365: unprotected loopback port 18532"):
        install.ready(host.ctx, host.settings)
    assert not any(row[0] == "create" or row[0] == "admin" and row[1][0] == "service-add"
                   for row in host.events)


def test_unknown_windows_server_refuses_before_account_or_registration(host):
    with pytest.raises(boring_deploy.Failure, match="unsupported Windows server 'd365-web'"):
        install.ready(host.ctx, {**host.settings, "services": "core=18530,d365-web=18532"})
    assert not host.events and not host.records


def test_first_install_refuses_when_protected_port_is_busy(host, monkeypatch):
    from contextlib import closing

    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as existing:
        existing.bind(("127.0.0.1", 0))
        port = existing.getsockname()[1]
        host.protected_ports.append(port)
        monkeypatch.setattr(install, "require_free_ports",
                            lambda config: check_free_ports(config | {"services": config["services"][:1]}))
        settings = host.settings | {"services": f"core={port},d365=18532"}
        with pytest.raises(boring_deploy.Failure, match=f"cannot bind loopback port {port}"):
            install.ready(host.ctx, settings)
    assert not host.records
