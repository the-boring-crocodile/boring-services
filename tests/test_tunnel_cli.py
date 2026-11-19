"""The real deploy/uninstall parsers and engine on an isolated account/SSH fixture."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest
import boring_deploy
from boring_deploy import artifact
from test_tunnel_install import host, install, engine_install  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("build_tunnel_fixture", ROOT / "scripts/build_tunnel.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.fixture
def cli(host, tmp_path, monkeypatch):
    monkeypatch.setenv("BORING_DEPLOY_TEST", "1")
    monkeypatch.setattr(boring_deploy, "LOCKS", tmp_path / "locks")
    homes = tmp_path / "homes.json"
    crocodile = tmp_path / "crocodile"
    (crocodile / "current").mkdir(parents=True)
    (crocodile / "current/VERSION").write_text("0.5.0\n")
    homes.write_text(json.dumps({"boring-crocodile": str(crocodile)}))
    monkeypatch.setattr(boring_deploy, "HOMES", homes)
    monkeypatch.setattr(boring_deploy.Context, "run", lambda self, *a, **kw: host.ctx.run(*a, **kw))
    monkeypatch.setitem(install.Setting.check.__globals__, "root_input", install.root_input)
    monkeypatch.setitem(sys.modules, "install_steps", install)
    step_spec = importlib.util.spec_from_file_location("tunnel_cli_steps", ROOT / "tunnel/deploy/steps.py")
    steps = importlib.util.module_from_spec(step_spec)
    step_spec.loader.exec_module(steps)
    archive = builder.release_artifact(artifact, tmp_path / "fixture-artifact")
    release = archive.parent / ("boring-tunnel-" + (ROOT / "tunnel/VERSION").read_text().strip())
    return host, release, steps


def arguments(host):
    return [str(host.ctx.home), "--name", "box", "--install",
            *[arg for key, value in host.settings.items()
              for arg in ("--" + key.replace("_", "-"), value)]]


def test_release_contains_only_deploy_and_its_declaration_owner(cli):
    host, release, _ = cli
    metadata = json.loads((release / "RELEASE.json").read_text())
    assert metadata["runtime"] == "files"
    assert metadata["version"] == (ROOT / "tunnel/VERSION").read_text().strip()
    assert (release / "deploy.d/tunnel_model.py").read_bytes() == (ROOT / "tunnel/deploy/model.py").read_bytes()
    assert not any(release.glob("*.whl"))
    assert not (release / "python.tar.gz").exists() and not (release / "image").exists()


def test_real_cli_plan_apply_and_uninstall_keep_account_files(cli, monkeypatch, capsys):
    host, release, steps = cli
    args = arguments(host)
    assert boring_deploy.main(release, steps, args + ["--check"], install=install) == 0
    assert not host.ctx.home.exists() and not host.records
    monkeypatch.setattr("builtins.input", lambda prompt: "APPLY")
    assert boring_deploy.main(release, steps, args, install=install) == 0
    assert install.look(host.ctx).installed
    assert not (host.ctx.home / "current/app").exists()
    kept = {path for path in host.ctx.home.rglob("*")}
    assert engine_install.uninstall_main(release, install,
        [str(host.ctx.home), "--name", "box", "--yes", "--delete-data"]) == 0
    assert not host.records
    assert kept <= {path for path in host.ctx.home.rglob("*")}
    assert (host.ctx.home / "instances/box/ssh/.ssh/authorized_keys").read_text() == ""
    assert "synthetic-private-value" not in capsys.readouterr().out


def test_real_cli_decline_never_creates_account_or_home(cli, monkeypatch):
    host, release, steps = cli
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert boring_deploy.main(release, steps, arguments(host), install=install) == 1
    assert not host.ctx.home.exists() and not host.records
    assert not [event for event in host.events if event[0] == "create"]


def test_partial_removal_retries_through_the_cli(cli, monkeypatch, capsys):
    host, release, steps = cli
    monkeypatch.setattr("builtins.input", lambda prompt: "APPLY")
    assert boring_deploy.main(release, steps, arguments(host), install=install) == 0
    args = [str(host.ctx.home), "--name", "box", "--yes", "--delete-data"]
    host.ctx.active = True
    assert engine_install.uninstall_main(release, install, args) == 1
    assert len(host.records) == 2
    assert "still has processes" in capsys.readouterr().err
    host.ctx.active = False
    assert engine_install.uninstall_main(release, install, args) == 0
    assert host.records == []
    assert (host.ctx.home / "instances/box/windows/settings.json").is_file()


def test_update_refuses_a_customized_installed_dropin(cli, monkeypatch, capsys):
    host, release, steps = cli
    monkeypatch.setattr("builtins.input", lambda prompt: "APPLY")
    assert boring_deploy.main(release, steps, arguments(host), install=install) == 0
    host.dropin.write_text(host.dropin.read_text() + "# operator comment\n")
    args = [str(host.ctx.home), "--name", "box"]
    assert boring_deploy.main(release, steps, args, install=install) == 1
    assert "customized" in capsys.readouterr().err
    assert len(host.records) == 2


def test_first_install_ignores_other_component_home(cli, tmp_path, monkeypatch, capsys):
    host, release, steps = cli
    opt = tmp_path / "opt"
    canvas = opt / "boring-canvas"
    (canvas / "config").mkdir(parents=True)
    (canvas / "release").mkdir()
    (canvas / "release/VERSION").write_text("0.3.0rc11\n")
    (canvas / "COMPONENT").write_text("boring-canvas\n")
    (canvas / "DEPLOYED").write_text("2026-11-18T08:30:00+01:00 root deploy 0.3.0rc11\n")
    (canvas / "boring-image.env").write_text("BORING_IMAGE=boring-canvas:0.3.0rc11\n")
    (canvas / "compose.yaml").write_text("services: {canvas: {image: '${BORING_IMAGE}'}}\n")
    homes = json.loads(boring_deploy.HOMES.read_text())
    homes["boring-canvas"] = str(canvas)
    boring_deploy.HOMES.write_text(json.dumps(homes))
    host.ctx.home = opt / "boring-tunnel"

    inspected = []
    input_info = boring_deploy._input_info

    def with_service_owner(fd, path, owner, **kwargs):
        if path == canvas or canvas in path.parents:
            inspected.append(path)
        if path == canvas / "config":
            # Model its distinct service UID without chown or root.
            raise boring_deploy.Failure(f"unsafe operator input {path}: owner uid 980, expected {owner}")
        return input_info(fd, path, owner, **kwargs)

    monkeypatch.setattr(boring_deploy, "_input_info", with_service_owner)
    monkeypatch.setattr("builtins.input", lambda prompt: "APPLY")
    assert boring_deploy.main(release, steps, arguments(host), install=install) == 0, capsys.readouterr().err
    assert install.look(host.ctx).installed and not inspected


def test_home_check_failure_can_retry_the_same_install(cli, monkeypatch, capsys):
    host, release, steps = cli
    args = arguments(host)
    original = install._directory
    fail = True
    created = False

    def create(*args):
        nonlocal created
        if created:
            raise boring_deploy.Failure("account already exists; not adopted")
        created = True
        return original_create(*args)

    def directory(path, mode):
        nonlocal fail
        if fail and path == host.ctx.home / "instances/box":
            fail = False
            raise boring_deploy.Failure("fixture HOME unavailable")
        return original(path, mode)

    original_create = install.create_system_account
    monkeypatch.setattr(install, "create_system_account", create)
    monkeypatch.setattr(install, "_directory", directory)
    monkeypatch.setattr("builtins.input", lambda prompt: "APPLY")
    assert boring_deploy.main(release, steps, args, install=install) == 1
    assert "fixture HOME unavailable" in capsys.readouterr().err
    assert not created
    assert boring_deploy.main(release, steps, args, install=install) == 0, capsys.readouterr().err
    assert install.look(host.ctx).installed
    assert len([event for event in host.events if event[0] == "create"]) == 1


def test_install_retries_after_ssh_validation_fails_with_account_created(cli, monkeypatch, capsys):
    host, release, steps = cli
    args = arguments(host)
    original = host.ctx.run

    def fail_with_account(*args, **kwargs):
        if args[:3] == ("sshd", "-t", "-f"):
            assert any(event[0] == "create" for event in host.events)
            raise boring_deploy.Failure("fixture SSH validation stopped")
        return original(*args, **kwargs)

    host.ctx.run = fail_with_account
    monkeypatch.setattr("builtins.input", lambda prompt: "APPLY")
    assert boring_deploy.main(release, steps, args, install=install) == 1
    assert "fixture SSH validation stopped" in capsys.readouterr().err
    assert install._load(host.ctx)["identity"] == {"uid": 901, "gid": 902}
    assert not host.records and not host.dropin.exists()
    host.ctx.run = original
    assert boring_deploy.main(release, steps, args, install=install) == 0
    assert install.look(host.ctx).installed
    assert len([event for event in host.events if event[0] == "create"]) == 1


@pytest.mark.parametrize("kind", ["symlink", "writable"])
def test_first_install_checks_home_ancestor_before_creating_account(cli, tmp_path, monkeypatch, capsys, kind):
    host, release, steps = cli
    parent = tmp_path / "opt"
    if kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        parent.symlink_to(target, target_is_directory=True)
        diagnostic = "unsafe component HOME"
    else:
        parent.mkdir()
        parent.chmod(0o777)
        diagnostic = "permits another writer"
    host.ctx.home = parent / "boring-tunnel"
    monkeypatch.setattr("builtins.input", lambda prompt: "APPLY")
    assert boring_deploy.main(release, steps, arguments(host), install=install) == 1
    assert diagnostic in capsys.readouterr().err
    assert not host.ctx.home.exists() and not host.records and not host.dropin.exists()
    assert not any(event[0] == "create" for event in host.events)


def test_deploy_check_names_retained_port_missing_from_crocodile_policy(cli, monkeypatch, capsys):
    host, release, steps = cli
    monkeypatch.setattr("builtins.input", lambda prompt: "APPLY")
    assert boring_deploy.main(release, steps, arguments(host), install=install) == 0
    host.protected_ports.remove(18532)
    assert boring_deploy.main(release, steps, [str(host.ctx.home), "--name", "box", "--check"],
                              install=install) == 1
    assert "box-d365: unprotected loopback port 18532" in capsys.readouterr().out
