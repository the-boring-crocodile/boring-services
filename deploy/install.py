"""Install steps for a service (runtime docker; see boring_deploy.install).

What a service is on a host, as install makes it and look finds it:

    account       boring-<service>: system account with its own group, no home, nologin
    HOME          root's, 0755
      state/        the account's, 0750 (Fishmarket 0700; state/oauth 0700): never removed;
                    both real directories (a symlink there is irregular)
      config/       config.json and the files it names (--config-dir), account:root,
                    read-only
      credentials/  the secrets (mcp-client-secret): --config-dir's credentials/,
                    account:root, read-only
      snapshots/    Sourcecache: its index (--index), root's, read-only; never removed
      compose.yaml  from the release's install/compose.yaml: image ${BORING_IMAGE}, the
                    account's uid:gid, the site's settings. Written last; the operator's
                    from then on (install and deploy never rewrite it)
    container     the compose project's (the deploy builds the image and starts it);
                  Fishmarket joins the edge's network (--edge-network), which must exist
    extras        Canvas: /usr/local/sbin/boring-canvas-admin

Installed means HOME/compose.yaml exists. Then irregular: it does not name
${BORING_IMAGE}, or the account is missing. Not installed: a container of the
project, or one of this image running from another directory, is irregular;
an account, state/ or config/ from an earlier install are reused and said so.
Files in HOME that neither compose.yaml nor the deploy uses are reported.

Uninstall: compose down, the service's images, the release directories, .env
and the extras go; with --delete-data state/, config/ and credentials/ are sealed (root's,
0700: a later account given the same uid cannot reach them) and then the
account goes (a seal that fails keeps the account); last compose.yaml becomes
compose.yaml.uninstalled, so a rerun after a failure on the way finishes.
State, configuration, snapshots and anything unrecognised stay and are listed.

Standard library only: it runs on the host's python3 from deploy.d/.
"""
import datetime
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

from boring_install import Failure, Look, Setting, seal

ENGINE_FILES = {"compose.yaml", ".env", "DEPLOYED", "COMPONENT", "release", "release.previous",
                "release.new", "backups"}
ADMIN_DIR = Path("/usr/local/sbin")

CONFIG_NAME = "config.json"
CREDENTIALS = "credentials"
CONFIG = Setting("config-dir", f"a directory with {CONFIG_NAME}, the files it names and {CREDENTIALS}/ "
                 "(the secrets: mcp-client-secret)", directory=True)
PUBLIC_URL = Setting("public-url", "the https://host[:port] your reverse proxy serves it at",
                     pattern=r"https://[A-Za-z0-9.-]+(:[0-9]+)?")
WEB_BIND = Setting("web-bind", "the IP address its web port listens on", default="127.0.0.1",
                   pattern=r"[0-9a-fA-F.:]+")
UNSAFE_URL_AUTH = Setting("unsafe-url-auth", "put the token into the links it gives out", flag=True)
EDGE_NETWORK = Setting("edge-network", "the docker network the edge (Caddy) reaches it on",
                       pattern=r"[A-Za-z0-9][A-Za-z0-9_.-]*")
INDEX = Setting("index", "the index file to serve (sourcecache build --db)", file=True)


@dataclass
class Spec:
    settings: list = field(default_factory=list)
    admin: str | None = None          # install/<admin> -> /usr/local/sbin/boring-<admin>
    state_mode: int = 0o750


SPECS = {
    "boring-canvas": Spec([CONFIG, PUBLIC_URL, WEB_BIND, UNSAFE_URL_AUTH], admin="canvas-admin"),
    "boring-knowledge": Spec([CONFIG]),
    "boring-fishmarket": Spec([CONFIG, EDGE_NETWORK, UNSAFE_URL_AUTH], state_mode=0o700),
    "boring-sourcecache-d365": Spec([CONFIG, INDEX]),
}


def SETTINGS(release):  # noqa: N802  (the name the engine asks for)
    return SPECS[release.component].settings


# -- reality -----------------------------------------------------------------

def _account(ctx):
    try:
        return pwd.getpwnam(ctx.release.component)
    except KeyError:
        return None


def _project(home):
    """docker compose's default project name: the directory's, normalised."""
    return re.sub(r"[^a-z0-9_-]", "", home.name.lower())


def _containers(ctx):
    """(name, image, working_dir) of every container on the host."""
    rows = ctx.run("docker", "ps", "--all", "--no-trunc", "--format",
                   '{{.Names}}\t{{.Image}}\t{{.Label "com.docker.compose.project"}}\t'
                   '{{.Label "com.docker.compose.project.working_dir"}}').stdout.splitlines()
    return [row.split("\t") for row in rows if row.strip()]


def _used_by_compose(ctx):
    """Top-level names in HOME that compose.yaml mounts or reads."""
    rendered = ctx.run("docker", "compose", "-f", str(ctx.home / "compose.yaml"), "config", "--format", "json",
                       env={"BORING_IMAGE": "look"}, check=False)
    try:
        config = json.loads(rendered.stdout) if rendered.returncode == 0 else None
    except ValueError:
        config = None
    if not isinstance(config, dict):
        return None
    paths = []
    for service in config.get("services", {}).values():
        paths += [v.get("source", "") for v in service.get("volumes", []) if v.get("type") == "bind"]
        paths += [e.get("path", "") if isinstance(e, dict) else e for e in service.get("env_file", []) or []]
        if isinstance(service.get("build"), dict):
            paths.append(service["build"].get("context", ""))
    paths += [s.get("file", "") for s in (config.get("secrets") or {}).values()]
    home = ctx.home.resolve()
    used = set()
    for path in filter(None, paths):
        path = Path(path)
        if path.is_absolute() and (path == home or home in path.parents):
            used.add(path.relative_to(home).parts[0] if path != home else ".")
    return used


# state/ and state/oauth belong to the service account, which can replace what
# is inside state/ (and state/ itself, through an earlier container or a
# restore). Install runs as root and chowns/chmods them: through a symlink that
# would hand any file on the host to the account. So both must be real
# directories; look() reports anything else before anything changes, and
# _service_directory() opens them without following symlinks (O_NOFOLLOW) and
# changes only the directory it opened (fchown/fchmod), so a swap after look()
# fails instead of being followed.
SERVICE_DIRECTORIES = ("state", "state/oauth")


def _not_directories(home):
    found = []
    for name in SERVICE_DIRECTORIES:
        path = home / name
        try:
            mode = path.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            continue
        if not stat.S_ISDIR(mode):
            kind = "a symlink" if stat.S_ISLNK(mode) else "not a directory"
            found.append(f"{path} is {kind}: install changes owner and mode of state/ and state/oauth "
                         "as root and only on real directories; move it away and check what it names")
            break  # what lies below it is not ours to look at
    return found


def _service_directory(parent_fd, path, account, mode):
    """PATH (named in the directory PARENT_FD) a real directory the account owns
    with MODE; returns its open fd (the caller closes it). Never follows a symlink."""
    name = path.name
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    except OSError as error:  # ELOOP: a symlink; ENOTDIR: something else
        raise Failure(f"{path} is not a real directory ({error.strerror}): nothing was changed on it",
                      "move it away and check what it names") from None
    try:
        os.fchown(fd, account.pw_uid, account.pw_gid)
        os.fchmod(fd, mode)
    except BaseException:
        os.close(fd)
        raise
    return fd


def look(ctx):
    home, component = ctx.home, ctx.release.component
    compose = home / "compose.yaml"
    account = _account(ctx)
    image = ctx.release.details["image"]
    irregular, notes = [], []
    irregular += _not_directories(home)
    rows = _containers(ctx)
    ours = [name for name, _, project, _ in rows if project == _project(home)]
    elsewhere = [f"{name} ({where or 'no compose project'})" for name, used, _, where in rows
                 if used.split(":")[0] == image and Path(where or "/").resolve() != home.resolve()]
    if elsewhere:
        irregular.append(f"{image} runs from another place: {', '.join(elsewhere)}")
    if compose.is_file():
        if "${BORING_IMAGE}" not in compose.read_text():
            irregular.append(f"{compose} does not name its image as ${{BORING_IMAGE}} (an install from before "
                             "the deploy engine: set `image: ${BORING_IMAGE}` once)")
        if account is None:
            irregular.append(f"{compose} exists but the account {component} does not")
        if not ours:
            notes.append("no container: the deploy starts it")
        used = _used_by_compose(ctx)
        if used is None:
            irregular.append(f"docker compose cannot read {compose}")
        else:
            unused = sorted(p.name for p in home.iterdir() if p.name not in ENGINE_FILES | used)
            if unused:
                notes.append("in HOME but used by neither compose.yaml nor the deploy (can go once checked): "
                             + ", ".join(unused))
        return Look(True, irregular, notes)
    if ours:
        irregular.append(f"containers of project {_project(home)} exist without {compose}: {', '.join(ours)}")
    if account is not None:
        notes.append(f"account {component} exists (uid {account.pw_uid}): reused")
    for kept, then in (("state", "reused"), ("snapshots", "reused"), ("config", "replaced by --config-dir"),
                       (CREDENTIALS, "replaced by --config-dir's credentials/"),
                       ("compose.yaml.uninstalled", "left as it is")):
        if (home / kept).exists():
            notes.append(f"{home / kept} from an earlier install: {then}")
    return Look(False, irregular, notes)


# -- install -----------------------------------------------------------------

def plan(ctx, settings):
    spec = SPECS[ctx.release.component]
    lines = [] if _account(ctx) else [f"create the system account {ctx.release.component} (its own group, nologin)"]
    lines += [f"{ctx.home}: state/ (the account's), config/ and {CREDENTIALS}/ from {settings['config_dir']}"]
    if "index" in settings:
        lines.append(f"{ctx.home / 'snapshots'}: {Path(settings['index']).name} from {settings['index']}")
    if "edge_network" in settings:
        lines.append(f"the container joins the docker network {settings['edge_network']} (must exist)")
    if spec.admin:
        lines.append(f"{ADMIN_DIR / ('boring-' + spec.admin)} (root only)")
    shown = {k: v for k, v in settings.items() if k not in ("config_dir", "index")}
    lines.append(f"compose.yaml from the release's template ({', '.join(f'{k}={v}' for k, v in shown.items()) or 'no settings'})")
    lines.append("then the first deploy: build the image, start the container, smoke")
    return lines


def _check_config(directory, name):
    service = Path(directory) / name
    try:
        if not isinstance(json.loads(service.read_text()), dict):
            raise ValueError
    except (OSError, ValueError):
        raise Failure(f"{service}: a {name} (a JSON object) is needed; see the examples") from None


def _same_file_content(a, b):
    if not b.is_file() or a.stat().st_size != b.stat().st_size:
        return False
    digests = []
    for path in (a, b):
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        digests.append(digest.digest())
    return digests[0] == digests[1]


def _index_copy(ctx, source):
    """Whether the index must be copied into HOME/snapshots/: not when it, or the
    same content under its name, is there already (a reinstall names its source
    again). Another file under that name stops the install before it changes anything."""
    source, target = Path(source), ctx.home / "snapshots" / Path(source).name
    if source.resolve() == target.resolve() or _same_file_content(source, target):
        return False
    if target.exists():
        raise Failure(f"{target} exists with other content than {source}",
                      "name the one in snapshots/, or move it away first")
    return True


def _index(ctx, source, copy):
    """The index in HOME/snapshots/, read-only; returns its name."""
    snapshots = ctx.home / "snapshots"
    snapshots.mkdir(mode=0o755, exist_ok=True)
    target = snapshots / Path(source).name
    if copy:
        partial = target.with_name(target.name + ".partial")
        shutil.copyfile(source, partial)
        partial.rename(target)
    target.chmod(0o444)
    return target.name


def _remove_tree(path):
    """rmtree, also of the read-only trees install makes (config/)."""
    if not path.exists():
        return
    for directory in [path, *(p for p in path.rglob("*") if p.is_dir() and not p.is_symlink())]:
        directory.chmod(0o700)
    shutil.rmtree(path)


def _config(ctx, source, account):
    """--config-dir into config/, its credentials/ (if any) into credentials/: both
    the account's (root's group), read-only, replaced whole."""
    secrets = Path(source) / CREDENTIALS
    _read_only_copy(ctx, source, "config", account, ignore=lambda folder, names:
                    [CREDENTIALS] if Path(folder) == Path(source) else [])
    if secrets.is_dir():
        _read_only_copy(ctx, secrets, CREDENTIALS, account)
    else:
        (ctx.home / CREDENTIALS).mkdir(mode=0o500, exist_ok=True)
        os.chown(ctx.home / CREDENTIALS, account.pw_uid, 0)


def _read_only_copy(ctx, source, name, account, ignore=None):
    target, new = ctx.home / name, ctx.home / f"{name}.new"
    _remove_tree(new)
    shutil.copytree(source, new, symlinks=False, ignore=ignore)
    for path in [new, *new.rglob("*")]:
        os.chown(path, account.pw_uid, 0)
        path.chmod(0o500 if path.is_dir() else 0o400)
    old = ctx.home / f"{name}.old"
    _remove_tree(old)
    if target.exists():
        target.rename(old)
    new.rename(target)
    _remove_tree(old)


def _compose(ctx, settings, account):
    text = (ctx.release.directory / "install" / "compose.yaml").read_text()
    web_bind = settings.get("web_bind", "")
    values = {"@UID@": str(account.pw_uid), "@GID@": str(account.pw_gid),
              "@PUBLIC_URL@": settings.get("public_url", ""),
              "@WEB_BIND@": f"[{web_bind}]" if ":" in web_bind else web_bind,
              "@EDGE_NETWORK@": settings.get("edge_network", ""),
              "@INDEX@": Path(settings.get("index", "")).name}
    for key, value in values.items():
        text = text.replace(key, value)
    lines = []
    for line in text.splitlines():
        if "@UNSAFE_URL_AUTH@" in line:
            if not settings.get("unsafe_url_auth"):
                continue
            line = line.split("#", 1)[0].rstrip()
        lines.append(line)
    temporary = ctx.home / "compose.yaml.new"
    temporary.write_text("\n".join(lines) + "\n")
    temporary.chmod(0o644)
    os.replace(temporary, ctx.home / "compose.yaml")


def ready(ctx, settings):
    """What the settings name must be there before anything changes."""
    _check_config(settings["config_dir"], CONFIG_NAME)
    if "edge_network" in settings and ctx.run("docker", "network", "inspect", settings["edge_network"],
                                               check=False).returncode != 0:
        raise Failure(f"no docker network {settings['edge_network']}", "the edge's network must exist first")
    if "index" in settings:
        _index_copy(ctx, settings["index"])


def install(ctx, settings):
    component, home = ctx.release.component, ctx.home
    spec = SPECS[component]
    copy_index = _index_copy(ctx, settings["index"]) if "index" in settings else False
    if _account(ctx) is None:
        ctx.run("useradd", "--system", "--user-group", "--no-create-home", "--home-dir", "/nonexistent",
                "--shell", "/usr/sbin/nologin", "--comment", f"{component} service account", component)
        ctx.log(f"created the account {component}")
    account = _account(ctx)
    home.mkdir(mode=0o755, parents=True, exist_ok=True)
    home_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)  # root's: it may be a symlink
    try:
        state_fd = _service_directory(home_fd, home / "state", account, spec.state_mode)
        try:
            os.close(_service_directory(state_fd, home / "state" / "oauth", account, 0o700))
        finally:
            os.close(state_fd)
    finally:
        os.close(home_fd)
    _config(ctx, settings["config_dir"], account)
    if "index" in settings:
        settings = {**settings, "index": _index(ctx, settings["index"], copy_index)}
    ctx.log(f"{home}: state/, config/ and {CREDENTIALS}/ ready" + (", the index in snapshots/" if "index" in settings else ""))
    if spec.admin:
        target = ADMIN_DIR / f"boring-{spec.admin}"
        text = (ctx.release.directory / "install" / spec.admin).read_text().replace("@HOME@", str(home))
        temporary = target.with_name(target.name + ".new")
        temporary.write_text(text)
        temporary.chmod(0o750)
        os.replace(temporary, target)
        ctx.log(f"installed {target}")
    _compose(ctx, settings, account)
    ctx.log(f"wrote {home / 'compose.yaml'} (yours from now on)")


# -- uninstall ---------------------------------------------------------------

def uninstall(ctx, delete_data):
    component, home = ctx.release.component, ctx.home
    spec = SPECS[component]
    compose = home / "compose.yaml"
    ctx.run("docker", "compose", "-f", str(compose), "down", env={"BORING_IMAGE": "uninstall"})
    ctx.log("container and network removed")
    image = ctx.release.details["image"]
    for tag in ctx.run("docker", "image", "ls", image, "--format", "{{.Tag}}", check=False).stdout.split():
        if tag != "<none>":
            ctx.run("docker", "image", "rm", f"{image}:{tag}", check=False)
    for name in ("release", "release.previous", "release.new"):
        shutil.rmtree(home / name, ignore_errors=True)
    (home / ".env").unlink(missing_ok=True)
    ctx.log("images, release directories and .env removed")
    if spec.admin:
        (ADMIN_DIR / f"boring-{spec.admin}").unlink(missing_ok=True)
    account = _account(ctx)
    if delete_data and account is not None:
        kept = [home / "state", home / "config", home / CREDENTIALS] + [
            home / f"{name}.{end}" for name in ("config", CREDENTIALS) for end in ("new", "old")
            if (home / f"{name}.{end}").exists()]
        seal(ctx, kept, account.pw_uid)  # raises: the account stays, for a rerun
        ctx.run("userdel", component)
        ctx.log(f"account {component} removed; what it owned is sealed (root's, 0700)")
    target = home / "compose.yaml.uninstalled"
    if target.exists():
        target = home / f"compose.yaml.uninstalled-{datetime.datetime.now():%Y%m%dT%H%M%S}"
    compose.rename(target)  # last: until here look says installed, and a rerun finishes
    return [str(p) for p in sorted(home.iterdir()) if p.name not in {"DEPLOYED", "COMPONENT"}]
