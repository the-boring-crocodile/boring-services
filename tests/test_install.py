"""deploy/install.py as an operator meets it, in a release assembled with the
real engine: first install of Canvas and Knowledge, settings in compose.yaml,
irregular hosts, leftovers reported, uninstall keeping state and config.
docker and useradd are fakes on PATH; accounts are the test user's."""
import importlib
import io
import json
import os
import pwd
import sys
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
    archive = artifact.assemble(out, {"component": f"boring-{service}", "version": version, "runtime": "docker",
                                      "default_home": "/nonexistent", "image": f"boring-{service}",
                                      "context": "image", "module": "boring_" + service.split("-")[0]},
                                ROOT / "deploy/steps.py", files, install=ROOT / "deploy/install.py")
    return archive.parent / f"boring-{service}-{version}"


def load(directory, tmp, monkeypatch, account=True):
    """The release's own install steps, with the test user standing in for the account."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)  # the release stays as its manifest lists it
    monkeypatch.syspath_prepend(str(directory / "deploy.d"))
    for name in ("install_steps", "boring_install"):
        sys.modules.pop(name, None)
    steps = importlib.import_module("install_steps")
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


def test_canvas_first_install_writes_the_sites_compose_and_admin(host, monkeypatch):
    directory = release(host, "canvas")
    steps = load(directory, host, monkeypatch, account=False)
    home = host / "boring-canvas"
    code, out = deploy(directory, steps, str(home), "--install", "--config-dir", str(host / "site-config"),
                       "--public-url", "https://canvas.example", "--web-bind", "fd00::1", "--unsafe-url-auth")
    assert code == 0, out
    assert "create the system account boring-canvas" in out and "7 record  boring-canvas 1.0.0 is live" in out
    compose = (home / "compose.yaml").read_text()
    me = pwd.getpwuid(os.getuid())
    assert f'user: "{me.pw_uid}:{me.pw_gid}"' in compose and "image: ${BORING_IMAGE}" in compose
    assert '"[fd00::1]:18510:18510"' in compose and "--public-base-url=https://canvas.example" in compose
    assert '- "--unsafe-url-auth"\n' in compose and "@" not in compose.split("services:")[1]
    assert (home / "credentials" / "mcp-client-secret").read_text() == "synthetic"
    assert not (home / "config" / "credentials").exists() and (home / "config" / "config.json").exists()
    assert "- ./credentials:/run/credentials:ro" in compose and "--config=/run/config/config.json" in compose
    assert (home / "state" / "oauth").is_dir()
    assert f"-f {home}/compose.yaml exec" in (host / "boring-canvas-admin").read_text()
    assert "--user-group" in (host / "account").read_text()
    assert json.loads(boring_deploy.HOMES.read_text()) == {"boring-canvas": str(home)}


def ups(host, seen=0):
    return [c for c in (host / "calls.log").read_text().splitlines()[seen:] if c.endswith(" up -d")]


@pytest.mark.parametrize("stack", ['{"Service":"canvas","State":"exited","Health":""}\n', ""])
def test_an_update_leaves_a_stopped_stack_stopped_and_a_running_one_is_updated(host, monkeypatch, stack):
    """The first install starts the stack even though nothing ran; an update of a
    stack the site stopped (`compose stop` or `down`) writes the new image into .env
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
    assert ups(host, seen) == [] and "BORING_IMAGE=boring-knowledge:1.1.0\n" in (home / ".env").read_text()
    (host / "stack").unlink()  # the site started it again: it runs
    seen = len((host / "calls.log").read_text().splitlines())
    third = release(host, "knowledge", "1.2.0")
    code, out = deploy(third, load(third, host, monkeypatch), str(home))
    assert code == 0 and "boring-knowledge 1.2.0 is live" in out, out
    assert len(ups(host, seen)) == 1 and "BORING_IMAGE=boring-knowledge:1.2.0\n" in (home / ".env").read_text()


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
    assert deploy(directory, steps, str(home), "--install", "--config-dir", str(host / "site-config"),
                  "--public-url", "https://canvas.example")[0] == 0
    (home / "build-0.2.5").mkdir()
    (home / "Dockerfile").write_text("old")
    rendered(home, host)
    (host / "containers").write_text(f"boring-canvas-canvas-1\tboring-canvas:1.0.0\tboring-canvas\t{home}\n")
    code, out = deploy(directory, steps, str(home), "--check")
    assert code == 0 and "installed" in out
    assert "used by neither compose.yaml nor the deploy (can go once checked): Dockerfile, build-0.2.5" in out
    compose = (home / "compose.yaml").read_text()
    code, out = deploy(directory, steps, str(home), "--public-url", "https://other.example")
    assert code == 1 and "settings are for a first install only" in out
    assert (home / "compose.yaml").read_text() == compose


def test_irregular_hosts_stop_before_anything(host, monkeypatch):
    directory = release(host, "canvas")
    steps = load(directory, host, monkeypatch)
    home = host / "boring-canvas"
    settings = ("--install", "--config-dir", str(host / "site-config"), "--public-url", "https://canvas.example")
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
    settings = ("--install", "--config-dir", str(host / "site-config"), "--public-url", "https://canvas.example")
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
    settings = ("--config-dir", str(host / "site-config"), "--public-url", "https://canvas.example")
    assert deploy(directory, steps, str(home), "--install", *settings)[0] == 0
    (home / "state" / "canvas.db").write_text("real data")
    rendered(home, host)
    compose = (home / "compose.yaml").read_text()
    code, out = uninstall(directory, steps, str(home), "--yes", "--delete-data")
    assert code == 0, out
    assert not (home / "compose.yaml").exists() and (home / "compose.yaml.uninstalled").exists()
    assert (home / "compose.yaml.uninstalled").read_text() == compose
    assert (home / "credentials" / "mcp-client-secret").read_text() == "synthetic"
    assert (home / "config" / "config.json").read_text() == '{"url": "https://canvas.example/mcp"}'
    assert not (home / "release").exists() and not (home / ".env").exists()
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
    assert deploy(directory, steps, str(home), "--install", "--config-dir", str(host / "site-config"),
                  "--public-url", "https://canvas.example")[0] == 0
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
    assert code == 1 and "a config.json (a JSON object) is needed" in out
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
    assert (home / "state").stat().st_mode & 0o777 == 0o700


def test_sourcecache_serves_its_index_from_snapshots_and_uninstall_keeps_it(host, monkeypatch):
    directory = release(host, "sourcecache-d365")
    steps = load(directory, host, monkeypatch)
    home = host / "boring-sourcecache-d365"
    index = host / "d365-standard-10.0.48-v1.db"
    index.write_text("an index")
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
