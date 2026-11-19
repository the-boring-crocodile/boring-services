"""deploy/install.py as an operator meets it, in a release assembled with the
real engine: first install of Canvas and Knowledge, settings in compose.yaml,
irregular hosts, leftovers reported, uninstall keeping state and config.
docker and useradd are fakes on PATH; accounts are the test user's."""
import importlib
from functools import partial
import io
import json
import os
import pwd
import sys
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

import boring_deploy  # noqa: E402  (the together environment has it: scripts/check.py)
from boring_deploy import artifact  # noqa: E402
from boring_deploy import install as boring_install  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux hosts")

FAKE = """#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_LOG"
case "$(basename "$0") $1" in
  "docker ps") cat "$FAKE_CONTAINERS" 2>/dev/null ;;
  "docker compose") case "$*" in
      *" config "*) cat "$FAKE_CONFIG" 2>/dev/null || echo '{}' ;;
      *" ps "*) cat "$FAKE_STACK" 2>/dev/null || echo '{"Service":"svc","State":"running","Health":""}' ;;
      *" up "*) rm -f "$FAKE_STACK" ;;
    esac ;;
  "useradd "*) echo "$*" > "$FAKE_ACCOUNT" ;;
  "docker network") [ -e "$FAKE_ACCOUNT.no-network" ] && exit 1 ;;
esac
exit 0
"""


class Steps:  # the deploy's own steps are the testbed's; here only install matters
    pass


CHOWNED = {}  # folder -> (uid, gid) that uninstall gave to root (the test cannot: recorded instead)


@pytest.fixture
def host(tmp_path, monkeypatch):
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("docker", "useradd", "userdel"):
        (tools / name).write_text(FAKE)
        (tools / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setattr(boring_deploy, "SYSTEM_TOOL_PATH", f"{tools}:/usr/sbin:/usr/bin:/sbin:/bin")
    for key, name in (("FAKE_LOG", "calls.log"), ("FAKE_CONTAINERS", "containers"),
                      ("FAKE_CONFIG", "config.json"), ("FAKE_ACCOUNT", "account"), ("FAKE_STACK", "stack")):
        monkeypatch.setenv(key, str(tmp_path / name))
    monkeypatch.setenv("BORING_DEPLOY_TEST", "1")
    monkeypatch.setattr(boring_deploy, "HOMES", tmp_path / "homes.json")
    monkeypatch.setattr(boring_deploy, "LOCKS", tmp_path / "locks")
    monkeypatch.setattr(boring_deploy, "WAIT_SECONDS", 1)
    CHOWNED.clear()
    fchown = os.fchown

    def to_root_recorded(fd, uid, gid):
        if (uid, gid) == (0, 0):
            CHOWNED[os.readlink(f"/proc/self/fd/{fd}")] = (uid, gid)
        else:
            fchown(fd, uid, gid)
    monkeypatch.setattr(os, "fchown", to_root_recorded)
    config = tmp_path / "site-config"
    config.mkdir()
    (config / "config.json").write_text('{"url": "https://canvas.example/mcp"}')
    (config / "credentials").mkdir()
    (config / "credentials" / "mcp-client-secret").write_text("synthetic")
    return tmp_path


def release(tmp, service, version="1.0.0"):
    out = tmp / f"out-{service}-{version}"
    out.mkdir()
    image = out / "Dockerfile"
    image.write_text("FROM scratch\n")
    files = {"image/Dockerfile": image, "install/compose.yaml": ROOT / service / "deploy/compose.yaml",
             "LICENSE": ROOT / "LICENSE", "COPYING.md": ROOT / "COPYING.md"}  # a release carries its licence
    if service == "canvas":
        files["install/canvas-admin"] = ROOT / "canvas/deploy/canvas-admin"
    if service == "mcp-ferry":
        files["deploy.d/ferry_config.py"] = ROOT / "mcp-ferry/src/boring_mcp_ferry/config.py"
    archive = artifact.assemble(out, {"component": f"boring-{service}", "version": version, "runtime": "docker",
                                      "default_home": "/nonexistent", "image": f"boring-{service}",
                                      "context": "image", "module": "boring_" + service.split("-")[0]},
                                ROOT / "deploy/steps.py", files, install=ROOT / "deploy/install.py")
    return archive.parent / f"boring-{service}-{version}"


def load(directory, tmp, monkeypatch, account=True, metadata=True, exposure=True):
    """The release's own install steps, with the test user standing in for the account."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)  # the release stays as its manifest lists it
    monkeypatch.syspath_prepend(str(directory / "deploy.d"))
    for name in ("install_steps", "boring_install", "steps"):
        sys.modules.pop(name, None)
    steps = importlib.import_module("install_steps")
    if exposure and hasattr(steps, "_proposed_exposure"):
        monkeypatch.setattr(steps, "_proposed_exposure", lambda ctx, settings: "fixture exposure: loopback")
        monkeypatch.setattr(steps, "_ferry_listeners", lambda ctx, settings, text: "{}")
    if metadata and hasattr(steps, "_installed_frontends"):
        # These host-layout tests fake the container; auth/parser journeys have
        # their own tests. The fake container reports a frontend with no Crocodile.
        monkeypatch.setattr(steps, "_installed_frontends", lambda ctx: {
            "": {"url": "https://service.example/mcp", "client_id": None, "authority_url": None}})
    # Use the real admission rules under the ordinary fixture owner.
    checked = partial(boring_deploy.root_input, owner=os.getuid())
    monkeypatch.setattr(steps, "root_input", checked, raising=False)
    monkeypatch.setitem(steps.Setting.check.__globals__, "root_input", checked)
    me = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(steps, "_account", lambda ctx: me if account or (tmp / "account").exists() else None)
    monkeypatch.setattr(steps.os, "chown", lambda *a: None)
    monkeypatch.setattr(steps, "ADMIN_DIR", tmp)
    return steps


def deploy(directory, steps, *argv):
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        code = boring_deploy.main(directory, Steps(), list(argv), install=steps)
    return code, out.getvalue()


def uninstall(directory, steps, *argv):
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        code = boring_install.uninstall_main(directory, steps, list(argv))
    return code, out.getvalue()


def rendered(home, tmp):
    """What `docker compose config` would answer for HOME's compose.yaml."""
    (tmp / "config.json").write_text(json.dumps({"services": {"canvas": {"volumes": [
        {"type": "bind", "source": str(home / "state")}, {"type": "bind", "source": str(home / "config")}]}}}))


@pytest.mark.parametrize("entrypoint", ["ready", "plan", "install"])
@pytest.mark.parametrize("unsafe", ["directory-link", "secret-link", "another-writer", "index-link",
                                  "ferry-url", "ferry-headers", "ferry-guide", "ferry-frontend"])
def test_proposed_inputs_refused_before_host_changes(host, monkeypatch, entrypoint, unsafe):
    from deploy_context import deploy_context
    from types import SimpleNamespace
    service = ("sourcecache-d365" if unsafe == "index-link" else
               "mcp-ferry" if unsafe.startswith("ferry-") else "knowledge")
    directory = release(host, service)
    install = load(directory, host, monkeypatch, account=False)
    source = host / "site-config"
    inputs = {"config_dir": str(source)}
    if unsafe.startswith("ferry-"):
        external = host / "external"
        external.mkdir()
        contents = {"url": "http://backend.example/mcp", "headers": "{}",
                    "guide": "A guide", "frontend": '{"url": "https://front.example/mcp"}'}
        for name, content in contents.items():
            (external / name).write_text(content)
        selected = external / unsafe.removeprefix("ferry-")
        selected.rename(host / "protected-target")
        selected.symlink_to(host / "protected-target")
        (source / "config.json").write_text(json.dumps({"backends": {"a": {
            "service": "../external/frontend", "guide_file": "../external/guide",
            "upstream": {"url_file": "../external/url", "headers_file": "../external/headers"}}}}))
    elif unsafe == "directory-link":
        alias = host / "input-alias"
        alias.symlink_to(source, target_is_directory=True)
        inputs["config_dir"] = str(alias)
    elif unsafe == "secret-link":
        secret = source / "credentials/mcp-client-secret"
        secret.unlink()
        secret.symlink_to(source / "config.json")
    elif unsafe == "another-writer":
        (source / "credentials/mcp-client-secret").chmod(0o666)
    else:
        index = host / "index.db"
        index.symlink_to(source / "config.json")
        inputs["index"] = str(index)
    home = host / "new-home"
    probe = deploy_context(home=home, release=SimpleNamespace(component="boring-" + service))
    if entrypoint == "install":
        class NoChanges:
            def mkdir(self, *args, **kwargs):
                pytest.fail("host changed before input refusal")
            def __truediv__(self, name):
                return home / name
        probe.home = NoChanges()
    with pytest.raises(boring_deploy.Failure, match="operator input"):
        getattr(install, entrypoint)(probe, inputs)
    assert not home.exists()
    assert not probe.run.called


def test_input_plan_names_owner_mode_without_content(host, monkeypatch):
    from deploy_context import deploy_context
    from types import SimpleNamespace
    directory = release(host, "knowledge")
    install = load(directory, host, monkeypatch)
    probe = deploy_context(home=host / "new-home", release=SimpleNamespace(component="boring-knowledge"))
    output = "\n".join(install.plan(probe, {"config_dir": str(host / "site-config")}))
    assert f"uid {os.getuid()}" in output and "mode " in output
    assert str(host / "site-config/credentials/mcp-client-secret") in output
    assert "synthetic" not in output


def test_ferry_plan_checks_and_names_external_parent_references(host, monkeypatch):
    from deploy_context import deploy_context
    from types import SimpleNamespace
    directory = release(host, "mcp-ferry")
    install = load(directory, host, monkeypatch)
    source = host / "site-config"
    external = host / "external"
    external.mkdir()
    data = {"url": "http://backend.example/mcp", "headers": '{"X-Key": "fixture-private-only"}',
            "guide": "fixture-private-only", "frontend": '{"url": "https://front.example/mcp"}'}
    for name, text in data.items():
        (external / name).write_text(text)
    (source / "config.json").write_text(json.dumps({"backends": {"a": {
        "service": "../external/frontend", "guide_file": "../external/guide",
        "upstream": {"url_file": "../external/url", "headers_file": "../external/headers"}}}}))
    probe = deploy_context(home=host / "new-home", release=SimpleNamespace(component="boring-mcp-ferry"))
    output = "\n".join(install.plan(probe, {"config_dir": str(source)}))
    assert all(str(source / "../external" / name) in output for name in data)
    assert "fixture-private-only" not in output
    assert not probe.home.exists() and not probe.run.called


@pytest.mark.parametrize("fails", [False, True])
def test_config_copy_is_private_throughout_and_restores_umask(host, monkeypatch, fails):
    directory = release(host, "canvas")
    steps = load(directory, host, monkeypatch)
    home = host / "private-copy"
    home.mkdir()
    from deploy_context import deploy_context
    ctx = deploy_context(home=home)
    # Even a broadly readable source must not reopen the staged secret.
    (host / "site-config").chmod(0o755)
    (host / "site-config/credentials").chmod(0o755)
    (host / "site-config/credentials/mcp-client-secret").chmod(0o644)
    real_copy = steps.shutil.copytree
    observed = []

    def checked_copy(*args, **kwargs):
        mask = os.umask(0o077)
        observed.append(mask)
        if fails:
            raise OSError("synthetic copy failure")
        result = real_copy(*args, **kwargs)
        assert all(path.stat().st_mode & 0o077 == 0 for path in Path(args[1]).rglob("*") if path.is_file())
        return result

    monkeypatch.setattr(steps.shutil, "copytree", checked_copy)
    previous = os.umask(0o022)
    try:
        if fails:
            with pytest.raises(OSError, match="synthetic copy failure"):
                steps._read_only_copy(ctx, host / "site-config", "config", pwd.getpwuid(os.getuid()))
        else:
            steps._read_only_copy(ctx, host / "site-config", "config", pwd.getpwuid(os.getuid()))
            assert (home / "config/credentials/mcp-client-secret").stat().st_mode & 0o777 == 0o400
        assert observed and set(observed) == {0o077}
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(previous)


def test_canvas_first_install_writes_the_sites_compose_and_admin(host, monkeypatch):
    directory = release(host, "canvas")
    steps = load(directory, host, monkeypatch, account=False)
    home = host / "boring-canvas"
    code, out = deploy(directory, steps, str(home), "--install", "--config-dir", str(host / "site-config"), "--unsafe-url-auth")
    assert code == 0, out
    assert "create the system account boring-canvas" in out and "7 record  boring-canvas 1.0.0 is live" in out
    compose = (home / "compose.yaml").read_text()
    me = pwd.getpwuid(os.getuid())
    assert f'user: "{me.pw_uid}:{me.pw_gid}"' in compose and "image: ${BORING_IMAGE}" in compose
    assert '"127.0.0.1:18511:18511"' in compose and "18510" not in compose
    assert "--public-base-url" not in compose and "--host=" not in compose
    assert '- "--unsafe-url-auth"\n' in compose and "@" not in compose.split("services:")[1]
    assert (home / "credentials" / "mcp-client-secret").read_text() == "synthetic"
    assert not (home / "config" / "credentials").exists() and (home / "config" / "config.json").exists()
    assert "- ./credentials:/run/credentials:ro" in compose and "--config=/run/config/config.json" in compose
    assert (home / "state" / "oauth").is_dir()
    admin = host / "boring-canvas-admin"
    subprocess.run([str(admin), "list"], check=True)
    assert (host / "calls.log").read_text().splitlines()[-1] == (
        f"docker compose -f {home}/compose.yaml --env-file {home}/boring-image.env "
        "exec -T canvas python3 -m boring_canvas.admin list")
    (home / ".env").write_text("OPERATOR=synthetic-private\n")
    (home / ".env").chmod(0o600)
    subprocess.run([str(admin), "limits"], check=True)
    assert (host / "calls.log").read_text().splitlines()[-1] == (
        f"docker compose -f {home}/compose.yaml --env-file {home}/.env "
        f"--env-file {home}/boring-image.env exec -T canvas python3 -m boring_canvas.admin limits")
    assert (home / ".env").read_text() == "OPERATOR=synthetic-private\n"
    assert "--user-group" in (host / "account").read_text()
    assert json.loads(boring_deploy.HOMES.read_text()) == {"boring-canvas": str(home)}


def ups(host, seen=0):
    return [c for c in (host / "calls.log").read_text().splitlines()[seen:] if c.endswith(" up -d")]


@pytest.mark.parametrize("stack", ['{"Service":"canvas","State":"exited","Health":""}\n', ""])
def test_an_update_leaves_a_stopped_stack_stopped_and_a_running_one_is_updated(host, monkeypatch, stack):
    """The first install starts the stack even though nothing ran; an update of a
    stack the site stopped (`compose stop` or `down`) writes the new image into boring-image.env
    and brings nothing up; an update of a running stack brings it up."""
    first = release(host, "knowledge")
    steps = load(first, host, monkeypatch)
    home = host / "boring-knowledge"
    (host / "stack").write_text("")  # nothing runs yet
    code, out = deploy(first, steps, str(home), "--install", "--config-dir", str(host / "site-config"))
    assert code == 0 and "1.0.0 is live" in out, out
    assert len(ups(host)) == 1
    (host / "stack").write_text(stack)  # the site stopped it
    seen = len((host / "calls.log").read_text().splitlines())
    second = release(host, "knowledge", "1.1.0")
    code, out = deploy(second, load(second, host, monkeypatch), str(home))
    assert code == 0, out
    assert "site is stopped: not started" in out and "smoke  skipped" in out
    assert "boring-knowledge 1.1.0 is installed, not started" in out
    assert ups(host, seen) == [] and "BORING_IMAGE=boring-knowledge:1.1.0\n" in (home / "boring-image.env").read_text()
    (host / "stack").unlink()  # the site started it again: it runs
    seen = len((host / "calls.log").read_text().splitlines())
    third = release(host, "knowledge", "1.2.0")
    code, out = deploy(third, load(third, host, monkeypatch), str(home))
    assert code == 0 and "boring-knowledge 1.2.0 is live" in out, out
    assert len(ups(host, seen)) == 1 and "BORING_IMAGE=boring-knowledge:1.2.0\n" in (home / "boring-image.env").read_text()


def test_knowledge_without_settings_but_its_config_and_no_unsafe_line(host, monkeypatch):
    directory = release(host, "knowledge")
    steps = load(directory, host, monkeypatch)
    home = host / "boring-knowledge"
    code, out = deploy(directory, steps, str(home), "--install")
    assert code == 1 and "--config-dir" in out and not home.exists()
    code, out = deploy(directory, steps, str(home), "--install", "--config-dir", str(host / "site-config"))
    assert code == 0, out
    assert "account boring-knowledge exists" in out and "create the system account" not in out
    assert not (host / "account").exists() and "useradd" not in (host / "calls.log").read_text()
    compose = (home / "compose.yaml").read_text()
    assert "unsafe" not in compose and "127.0.0.1:18512:18512" in compose


def test_settings_once_installed_are_refused_and_leftovers_reported(host, monkeypatch):
    directory = release(host, "canvas")
    steps = load(directory, host, monkeypatch)
    home = host / "boring-canvas"
    assert deploy(directory, steps, str(home), "--install", "--config-dir", str(host / "site-config"))[0] == 0
    (home / "build-0.2.5").mkdir()
    (home / "Dockerfile").write_text("old")
    rendered(home, host)
    (host / "containers").write_text(f"boring-canvas-canvas-1\tboring-canvas:1.0.0\tboring-canvas\t{home}\n")
    code, out = deploy(directory, steps, str(home), "--check")
    assert code == 0 and "installed" in out
    assert "used by neither compose.yaml nor the deploy (can go once checked): Dockerfile, build-0.2.5" in out
    compose = (home / "compose.yaml").read_text()
    code, out = deploy(directory, steps, str(home), "--unsafe-url-auth")
    assert code == 1 and "settings are for a first install only" in out
    assert (home / "compose.yaml").read_text() == compose


def test_irregular_hosts_stop_before_anything(host, monkeypatch):
    directory = release(host, "canvas")
    steps = load(directory, host, monkeypatch)
    home = host / "boring-canvas"
    settings = ("--install", "--config-dir", str(host / "site-config"))
    (host / "containers").write_text("boring-canvas-canvas-1\tboring-canvas:0.2.6\tboring-canvas\t/opt/docker/x\n")
    code, out = deploy(directory, steps, str(home), *settings)
    assert code == 1 and "irregular" in out and "without" in out and "runs from another place" in out
    assert not home.exists()
    (host / "containers").write_text("")
    home.mkdir()
    (home / "compose.yaml").write_text("services:\n  canvas:\n    build: .\n")
    rendered(home, host)
    code, out = deploy(directory, steps, str(home))
    assert code == 1 and "does not name its image" in out
    assert (home / "compose.yaml").read_text() == "services:\n  canvas:\n    build: .\n"


def _footprint(path):
    info = path.lstat()
    return info.st_uid, info.st_gid, info.st_mode, path.read_bytes() if path.is_file() else None


def test_symlinks_in_state_stop_the_install_and_leave_their_targets_alone(host, monkeypatch):
    directory = release(host, "canvas")
    steps = load(directory, host, monkeypatch)
    home = host / "boring-canvas"
    settings = ("--install", "--config-dir", str(host / "site-config"))
    outside = host / "outside-secret"
    outside.write_bytes(b"root's file")
    outside.chmod(0o640)
    before = _footprint(outside)
    home.mkdir()
    (home / "state").symlink_to(outside)
    code, out = deploy(directory, steps, str(home), *settings)
    assert code == 1 and "irregular" in out and f"{home / 'state'} is a symlink" in out
    assert _footprint(outside) == before and not (home / "compose.yaml").exists()

    (home / "state").unlink()
    (home / "state").mkdir()
    elsewhere = host / "outside-dir"
    elsewhere.mkdir(mode=0o755)
    before = _footprint(elsewhere)
    (home / "state" / "oauth").symlink_to(elsewhere)
    code, out = deploy(directory, steps, str(home), *settings)
    assert code == 1 and f"{home / 'state' / 'oauth'} is a symlink" in out
    assert _footprint(elsewhere) == before and not (home / "compose.yaml").exists()

    # a swap after look(): install itself does not follow it either
    me = pwd.getpwuid(os.getuid())
    fd = os.open(home / "state", os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(steps.Failure, match="not a real directory"):
            steps._service_directory(fd, home / "state" / "oauth", me, 0o700)
    finally:
        os.close(fd)
    assert _footprint(elsewhere) == before


def test_uninstall_keeps_state_and_config_and_a_reinstall_reuses_them(host, monkeypatch):
    directory = release(host, "canvas")
    steps = load(directory, host, monkeypatch)
    home = host / "boring-canvas"
    settings = ("--config-dir", str(host / "site-config"))
    assert deploy(directory, steps, str(home), "--install", *settings)[0] == 0
    (home / "state" / "canvas.db").write_text("real data")
    operator_env = home / ".env"
    operator_env.write_bytes(b"SECRET=synthetic-private\r\nBORING_IMAGE=legacy:1\r\nOTHER=x")
    operator_env.chmod(0o600)
    rendered(home, host)
    compose = (home / "compose.yaml").read_text()
    code, out = uninstall(directory, steps, str(home), "--yes", "--delete-data")
    assert code == 0, out
    assert not (home / "compose.yaml").exists() and (home / "compose.yaml.uninstalled").exists()
    assert (home / "compose.yaml.uninstalled").read_text() == compose
    assert (home / "credentials" / "mcp-client-secret").read_text() == "synthetic"
    assert (home / "config" / "config.json").read_text() == '{"url": "https://canvas.example/mcp"}'
    assert not (home / "release").exists() and not (home / "boring-image.env").exists()
    assert operator_env.read_bytes() == b"SECRET=synthetic-private\r\nOTHER=x"
    assert operator_env.stat().st_mode & 0o777 == 0o600
    assert not (host / "boring-canvas-admin").exists()
    assert (home / "state" / "canvas.db").read_text() == "real data"
    assert "no longer live" in out and str(home / "state") in out and str(home / "config") in out
    calls = (host / "calls.log").read_text()
    assert "compose -f" in calls and " down" in calls and "userdel boring-canvas" in calls
    assert calls.index(" down") < calls.index("userdel boring-canvas")
    # sealed before userdel: root's (recorded), 0700, the content untouched
    assert CHOWNED == {str(home / "state"): (0, 0), str(home / "config"): (0, 0), str(home / "credentials"): (0, 0)}
    assert (home / "state").stat().st_mode & 0o777 == 0o700 and (home / "config").stat().st_mode & 0o777 == 0o700
    code, out = deploy(directory, steps, str(home), "--install", *settings)
    assert code == 0, out
    assert "state from an earlier install: reused" in out.replace(str(home) + "/", "")
    assert (home / "state" / "canvas.db").read_text() == "real data"


def test_a_seal_that_fails_keeps_the_account_and_a_rerun_finishes(host, monkeypatch):
    """config/ replaced by a symlink: not followed, not sealed, no userdel, the
    service still looks installed; once it is a folder again a rerun finishes."""
    directory = release(host, "canvas")
    steps = load(directory, host, monkeypatch)
    home = host / "boring-canvas"
    assert deploy(directory, steps, str(home), "--install", "--config-dir", str(host / "site-config"))[0] == 0
    rendered(home, host)
    outside = host / "outside"
    outside.mkdir(mode=0o755)
    os.chmod(outside, 0o755)
    os.chmod(home / "config", 0o700)  # read-only as install leaves it: a folder cannot move to another parent
    (home / "config").rename(host / "config-aside")
    (home / "config").symlink_to(outside)
    code, out = uninstall(directory, steps, str(home), "--yes", "--delete-data")
    assert code == 1 and "is a symlink: not sealed, the account is kept" in out
    assert "userdel" not in (host / "calls.log").read_text() and CHOWNED == {}
    assert outside.stat().st_mode & 0o777 == 0o755 and (home / "compose.yaml").exists()
    (home / "config").unlink()
    (host / "config-aside").rename(home / "config")
    code, out = uninstall(directory, steps, str(home), "--yes", "--delete-data")
    assert code == 0, out
    assert "userdel boring-canvas" in (host / "calls.log").read_text()
    assert set(CHOWNED) == {str(home / "state"), str(home / "config"), str(home / "credentials")}
    assert not (home / "compose.yaml").exists() and (home / "compose.yaml.uninstalled").exists()


def test_fishmarket_needs_its_config_json_and_the_edge_network(host, monkeypatch):
    directory = release(host, "fishmarket")
    steps = load(directory, host, monkeypatch)
    home = host / "boring-fishmarket"
    config = host / "fish-config"
    config.mkdir()
    settings = ("--install", "--config-dir", str(config), "--edge-network", "edge_default")
    code, out = deploy(directory, steps, str(home), *settings)
    assert code == 1 and "operator input" in out and str(config / "config.json") in out
    (config / "config.json").write_text('{"url": "https://pond.example/fishmarket/mcp"}')
    (host / "account.no-network").write_text("")
    code, out = deploy(directory, steps, str(home), *settings)
    assert code == 1 and "no docker network edge_default" in out and "nothing changed" in out
    assert not home.exists()
    (host / "account.no-network").unlink()
    code, out = deploy(directory, steps, str(home), *settings)
    assert code == 0, out
    compose = (home / "compose.yaml").read_text()
    assert "name: edge_default" in compose and "\"--unsafe-url-auth\"" not in compose
    assert "ports:" not in compose  # the edge reaches it on its network; no host port by default
    assert (home / "state").stat().st_mode & 0o777 == 0o700


def test_fishmarket_loopback_port_for_a_proxy_on_the_host(host, monkeypatch):
    """--loopback-port: the first deploy behind a proxy on the host passes without
    editing compose.yaml (real-domain fresh install, 2026-11-01)."""
    directory = release(host, "fishmarket")
    steps = load(directory, host, monkeypatch)
    home = host / "boring-fishmarket"
    config = host / "fish-config"
    config.mkdir()
    (config / "config.json").write_text('{"url": "https://pond.example/fishmarket/mcp"}')
    code, out = deploy(directory, steps, str(home), "--install", "--config-dir", str(config),
                       "--edge-network", "fishmarket-net", "--loopback-port")
    assert code == 0, out
    assert "published on the host's loopback: 127.0.0.1:18518" in out
    compose = (home / "compose.yaml").read_text()
    assert '    ports: ["127.0.0.1:18518:8000"]\n' in compose and "@" not in compose.split("services:")[1]


def test_sourcecache_serves_its_index_from_snapshots_and_uninstall_keeps_it(host, monkeypatch):
    directory = release(host, "sourcecache-d365")
    steps = load(directory, host, monkeypatch)
    home = host / "boring-sourcecache-d365"
    index = host / "d365-standard-10.0.48-v1.db"
    index.write_text("an index")
    original_copy = steps.shutil.copyfile
    def no_unchecked_copy(source, target, *args, **kwargs):
        path = Path(source)
        if path == index or host / "site-config" in path.parents:
            pytest.fail("operator source copied by pathname instead of its checked descriptor")
        return original_copy(source, target, *args, **kwargs)
    monkeypatch.setattr(steps.shutil, "copyfile", no_unchecked_copy)
    settings = ("--install", "--config-dir", str(host / "site-config"), "--index", str(index))
    code, out = deploy(directory, steps, str(home), *settings)
    assert code == 0, out
    assert "--db=/data/snapshots/d365-standard-10.0.48-v1.db" in (home / "compose.yaml").read_text()
    snapshot = home / "snapshots" / index.name
    assert snapshot.read_text() == "an index"

    def snapshot_kept():
        assert snapshot.read_bytes() == b"an index" and snapshot.stat().st_mode & 0o777 == 0o444

    snapshot_kept()
    rendered(home, host)
    compose = (home / "compose.yaml").read_text()
    code, out = uninstall(directory, steps, str(home), "--yes")
    assert code == 0 and str(home / "snapshots") in out
    assert not (home / "compose.yaml").exists() and (home / "compose.yaml.uninstalled").read_text() == compose
    snapshot_kept()
    code, out = deploy(directory, steps, str(home), *settings)  # the same source again: same content
    assert code == 0 and "snapshots from an earlier install: reused" in out.replace(str(home) + "/", "")
    snapshot_kept()
    assert uninstall(directory, steps, str(home), "--yes")[0] == 0
    assert not (home / "compose.yaml").exists() and (home / "compose.yaml.uninstalled").read_text() == compose
    assert len(list(home.glob("compose.yaml.uninstalled-*"))) == 1
    snapshot_kept()
    index.write_text("another index")
    code, out = deploy(directory, steps, str(home), *settings)
    assert code == 1 and "exists with other content" in out and "nothing changed" in out
    snapshot_kept()
    assert not (home / "compose.yaml").exists()


# -- a home laid out its own way: the deploy renames nothing --

OLD_COMPOSE = """services:
  knowledge:
    image: ${{BORING_IMAGE}}
    volumes:
      - {home}/state:/data/state
      - {home}/service.json:/run/config/service.json:ro
    secrets:
      - mcp_client_secret
    command:
      - --config=/run/config/service.json
secrets:
  mcp_client_secret:
    file: ./mcp_client_secret
"""


def own_layout_home(host, monkeypatch):
    """Knowledge installed, then laid out the operator's own way: service.json and
    mcp_client_secret at the top of HOME, named by the operator's compose.yaml."""
    directory = release(host, "knowledge")
    steps = load(directory, host, monkeypatch)
    home = host / "boring-knowledge"
    assert deploy(directory, steps, str(home), "--install", "--config-dir", str(host / "site-config"))[0] == 0
    for folder in ("config", "credentials"):
        steps._remove_tree(home / folder)
    (home / "service.json").write_text('{"url": "https://knowledge.example/mcp"}')
    (home / "mcp_client_secret").write_text("synthetic")
    (home / "mcp_client_secret").chmod(0o400)
    (home / "compose.yaml").write_text(OLD_COMPOSE.format(home=home))
    return directory, steps, home


def files(home):
    """The site's own files: configuration, secrets, compose.yaml (not the releases)."""
    site = {"config", "credentials", "compose.yaml", "service.json", "mcp_client_secret"}
    return {str(p.relative_to(home)): p.read_bytes() for p in sorted(home.rglob("*"))
            if p.is_file() and p.relative_to(home).parts[0] in site}


def test_an_own_layout_deploys_as_it_is(host, monkeypatch):
    # The deploy runs the site's compose.yaml as it stands and renames nothing.
    directory, steps, home = own_layout_home(host, monkeypatch)
    before = files(home)
    code, out = deploy(directory, steps, str(home))
    assert code == 0, out
    assert "moved" not in out and files(home) == before
