"""Tunnel declaration and SSH policy; no accounts, sockets or live configuration."""
import importlib.util
from pathlib import Path
import sys
import pytest
from boring_deploy import Failure

SOURCE = Path(__file__).resolve().parents[1] / "tunnel/deploy/model.py"
spec = importlib.util.spec_from_file_location("tunnel_model", SOURCE)
model = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = model
spec.loader.exec_module(model)


def declaration(**changes):
    return model.declaration(**({"name": "box", "box": "203.0.113.7",
        "services": "core=18530,d365=18532", "callers": "pond=writer,pirate/parrot=reader"} | changes))


def test_explicit_ports_own_names_urls_and_reverse_permissions():
    value = declaration()
    assert value["account"] == "tunnel-box"
    assert value["services"] == [
        {"short": "core", "name": "box", "port": 18530, "url": "http://127.0.0.1:18530/mcp"},
        {"short": "d365", "name": "box-d365", "port": 18532, "url": "http://127.0.0.1:18532/mcp"}]
    assert value["callers"] == "pond=writer,pirate/parrot=reader"
    assert model.declaration(name="other", box="2001:db8::7", services="d365=19022",
                              callers="")["box"] == "2001:db8::7"


@pytest.mark.parametrize("change", [
    {"name": "../box"}, {"name": "x" * 25}, {"box": "example.test"}, {"box": "203.0.113.7/24"}, {"box": 'fe80::1%bad"scope'},
    {"services": ""}, {"services": "core"}, {"services": "core=22"},
    {"services": "core=18530,d365=18530"}, {"services": "core=18530,core=18532"},
    {"services": "core=18530,../../other=18532"}, {"services": "core=18530,d365=65536"},
])
def test_invalid_declarations_refuse(change):
    with pytest.raises(Failure):
        declaration(**change)


def test_key_has_source_and_reverse_listeners_only():
    line = model.authorized_key(declaration(), "ssh-ed25519 c3ludGhldGlj fixture\n")
    assert line.startswith('restrict,port-forwarding,from="203.0.113.7",')
    assert 'permitlisten="127.0.0.1:18530"' in line
    assert 'permitlisten="127.0.0.1:18532"' in line
    assert "permitopen" not in line  # complete -L denial belongs to the daemon
    assert line.endswith(" ssh-ed25519 c3ludGhldGlj fixture\n")


@pytest.mark.parametrize("key", ["", 'command="anything" ssh-ed25519 c3ludGhldGlj',
                                "ssh-ed25519 c3ludGhldGlj\nssh-ed25519 c3ludGhldGlj",
                                "ssh-ed25519 invalid!!!"])
def test_caller_cannot_supply_ssh_options_or_multiple_keys(key):
    with pytest.raises(Failure):
        model.authorized_key(declaration(), key)


def test_account_policy_denies_local_forwarding_shells_passwords_and_unix_sockets():
    text = model.ssh_policy(declaration())
    assert text.startswith("\n# boring-tunnel box\nMatch User tunnel-box\n")
    for line in ("AllowTcpForwarding remote", "PermitOpen none", "MaxSessions 0",
                 "AllowStreamLocalForwarding no", "PasswordAuthentication no",
                 "KbdInteractiveAuthentication no", "PermitTunnel no", "GatewayPorts no"):
        assert "    " + line + "\n" in text
    assert "    PermitListen 127.0.0.1:18530 127.0.0.1:18532\n" in text


def test_openssh_effective_policy_on_an_isolated_fixture(tmp_path):
    import shutil
    import subprocess
    sshd, keygen = shutil.which("sshd"), shutil.which("ssh-keygen")
    if not sshd or not keygen:
        pytest.skip("OpenSSH tools unavailable")
    key = tmp_path / "host-key"
    subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True,
                   capture_output=True)
    config = tmp_path / "sshd_config"
    config.write_text("HostKey " + str(key) + "\nUsePAM yes\n" + model.ssh_policy(declaration()))
    def effective(user):
        reply = subprocess.run([sshd, "-T", "-f", str(config), "-C",
            "user=" + user + ",host=fixture,addr=203.0.113.7"], text=True, capture_output=True)
        assert reply.returncode == 0, reply.stderr
        return dict(line.split(None, 1) for line in reply.stdout.splitlines() if " " in line)
    owned = effective("tunnel-box")
    assert owned["allowtcpforwarding"] == "remote"
    assert owned["allowstreamlocalforwarding"] == "no"
    assert owned["permitopen"] == "none"
    assert owned["permitlisten"] == "127.0.0.1:18530 127.0.0.1:18532"
    assert owned["maxsessions"] == "0"
    assert owned["passwordauthentication"] == "no"
    assert owned["authorizedkeysfile"] == ".ssh/authorized_keys"
    assert owned["authorizedkeyscommand"] == "none"
    assert owned["trustedusercakeys"] == "none"
    assert owned["authenticationmethods"] == "publickey"
    other = effective("unrelated-fixture-account")
    assert other["maxsessions"] != "0"
    assert other["allowtcpforwarding"] != "remote"
