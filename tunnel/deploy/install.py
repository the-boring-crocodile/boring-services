"""Install the optional tunnel through the normal deploy plan, then typed APPLY.

Crocodile owns service policy/credentials. Common owns Unix identity checks.
This component owns endpoint declarations, its SSH policy and retained files.
"""
from contextlib import ExitStack, contextmanager
from fnmatch import fnmatchcase
from glob import glob
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit

from boring_deploy import (AccountIdentity, Failure, create_system_account,
                           lock_system_account, root_child, root_input, system_account)
from boring_install import Look, Setting
import tunnel_model as model

DELETE_DATA_HELP = "finish retained removal; lock the account and keep every file"

INSTANCE = Setting("name", "the Windows box name", pattern=r"[a-z][a-z0-9-]{0,23}")
SETTINGS = [Setting("box", "the Windows source IP"), Setting("key", "a staged plain SSH public key", file=True),
            Setting("services", "explicit NAME=PORT endpoints, for example core=18530,d365=18532"),
            Setting("callers", "Crocodile NAME=ROLE grants", default=""),
            Setting("ssh-host", "the Linux SSH host name Windows connects to")]
SSH_CONFIG = Path("/etc/ssh/sshd_config")
ADMIN = "/opt/boring-crocodile/current/bin/boring-crocodile-admin"


def _site(ctx):
    return ctx.home / "instances" / ctx.instance


def _read(path):
    with root_input(path) as source:
        return source.read_text()


def _ssh_dropin(name):
    # Keep tunnel policies next to the Pond's 91-boring-crocodile.conf convention.
    # An earlier matching directive can win. sshd -T checks the effective result.
    return SSH_CONFIG.parent / "sshd_config.d" / f"92-boring-tunnel-{name}.conf"


def _check_ssh_dropin(path, policy):
    if not os.path.lexists(path):
        return False
    try:
        matches = _read(path) == policy
    except (Failure, OSError, UnicodeError):
        matches = False
    if not matches:
        raise Failure(f"{path}: customized or foreign SSH drop-in; retained")
    return True


def _ssh_candidate(main, dropin, staged=None):
    """Require a main-file Include for our path and stage it in its glob order."""
    found = False
    lines = []
    for line in main.splitlines(keepends=True):
        directive = re.match(r"^\s*include(?:\s*=\s*|\s+)", line, re.IGNORECASE)
        if directive is None:
            lines.append(line)
            continue
        patterns = shlex.split(line[directive.end():], comments=True)
        includes, changed = [], False
        for pattern in patterns:
            absolute = SSH_CONFIG.parent / pattern
            if absolute.parent != dropin.parent or not fnmatchcase(dropin.name, absolute.name):
                includes.append(pattern)
                continue
            found = True
            if staged is None:
                continue
            # Expand just this operand as OpenSSH would, inserting the staged
            # file under its final sort key. Other includes retain their position.
            matches = set(glob(str(absolute))) | {str(dropin)}
            includes.extend(str(staged) if path == str(dropin) else path for path in sorted(matches))
            changed = True
        lines.append("Include " + " ".join(json.dumps(path, ensure_ascii=False) for path in includes)
                     + "\n" if changed else line)
    if not found:
        raise Failure(f"{SSH_CONFIG} needs an Include for {dropin.parent} matching {dropin.name}")
    return "".join(lines)


@contextmanager
def _directory_fd(path, *, create=None, owner=0):
    """Open only this directory and its ancestors, with no symlinks or other writers."""
    if not path.is_absolute() or ".." in path.parts:
        raise Failure(f"{path}: expected an absolute directory path without '..'")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as opened:
        fd = None
        current = Path(path.anchor)
        try:
            for index, part in enumerate(path.parts):
                current /= part
                last = index == len(path.parts) - 1
                try:
                    next_fd = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    if create is None:
                        raise
                    os.mkdir(part, create if last else 0o755, dir_fd=fd)
                    next_fd = os.open(part, flags, dir_fd=fd)
                opened.callback(os.close, next_fd)
                fd = next_fd
                info = os.fstat(fd)
                if info.st_uid not in ({owner} if last else {0, owner}):
                    raise Failure(f"{current}: unsafe directory owner uid {info.st_uid}")
                sticky_ancestor = not last and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
                if info.st_mode & 0o022 and not sticky_ancestor:
                    raise Failure(f"{current}: directory permits another writer")
        except OSError as error:
            raise Failure(f"{current}: cannot open or create safe directory: {error.strerror}") from error
        yield fd


def _directory(path, mode):
    with _directory_fd(path, create=mode):
        pass


def _write(path, text, mode=0o600, expected=None):
    """Publish only in checked root-controlled directories, preserving old metadata."""
    with _directory_fd(path.parent):
        pass
    metadata, attributes = None, {}
    if os.path.lexists(path):
        with root_input(path) as source:
            if expected is None or source.read_text() != expected:
                raise Failure(str(path) + ": changed or already exists; retained")
            metadata = os.fstat(source.fd)
            attributes = {name: os.getxattr(source.fd, name) for name in os.listxattr(source.fd)}
    elif expected is not None:
        raise Failure(str(path) + ": expected file disappeared; retained")
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fchmod(output.fileno(), stat.S_IMODE(metadata.st_mode) if metadata else mode)
            if metadata:
                os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
            for name, value in attributes.items():
                os.setxattr(output.fileno(), name, value)
            os.fsync(output.fileno())
        if expected is not None and _read(path) != expected:
            raise Failure(str(path) + ": changed before publication; retained")
        if expected is None:
            try:
                os.link(temporary, path)
                os.unlink(temporary)
            except FileExistsError:
                raise Failure(str(path) + ": appeared before publication; retained") from None
        else:
            os.replace(temporary, path)
        with _directory_fd(path.parent) as parent:
            os.fsync(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _admin(arguments, apply=False):
    # The Crocodile prompts on stderr/stdin; capture credentials only in memory.
    reply = root_child([ADMIN, *arguments], text=True, stdout=subprocess.PIPE,
                       stderr=None, stdin=None if apply else subprocess.DEVNULL)
    if reply.returncode:
        raise Failure("Crocodile " + arguments[0] + " failed; inspect service-list before retrying")
    try:
        return json.loads(reply.stdout)
    except (TypeError, ValueError):
        raise Failure("Crocodile reply is not JSON; inspect service-list before retrying") from None


def _add(service, callers):
    return ["service-add", service["name"], "--url", service["url"], "--callers", callers,
            "--roles", "reader,writer", "--default-role", "reader"]


def _load(ctx):
    return json.loads(_read(_site(ctx) / "config.json"))



def _credential(ctx, service):
    path = _site(ctx) / "credentials" / (service["short"] + ".json")
    if not os.path.lexists(path):
        return None
    try:
        data = json.loads(_read(path))
    except ValueError:
        raise Failure(str(path) + ": saved credential is invalid; retained") from None
    if (not isinstance(data, dict) or set(data) != {"client_id", "client_secret"}
            or not isinstance(data["client_id"], str) or not data["client_id"].startswith("svc_")
            or not isinstance(data["client_secret"], str) or not data["client_secret"]):
        raise Failure(str(path) + ": saved credential is invalid; retained")
    return data


def _records():
    records = _admin(["service-list"])
    if (not isinstance(records, list) or any(not isinstance(r, dict) or
            any(not isinstance(r.get(k), str) for k in ("name", "client_id", "url")) for r in records)):
        raise Failure("Crocodile service-list has invalid service identities")
    return records


def _loopback_port(url):
    parsed = urlsplit(url)
    host = parsed.hostname
    if host not in {"localhost", "127.0.0.1"}:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return None
        # An IPv6 wildcard can overlap IPv4. A v6-only loopback cannot.
        if not (address.is_unspecified or getattr(address, "ipv4_mapped", None)
                and address.ipv4_mapped == ipaddress.IPv4Address("127.0.0.1")):
            return None
    return parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)


def require_protected_ports(config, inventory):
    ports = inventory.get("protected_service_ports") if isinstance(inventory, dict) else None
    if not isinstance(ports, list) or any(type(port) is not int for port in ports):
        raise Failure("Crocodile inventory cannot report protected service ports; update Crocodile first")
    for service in config["services"]:
        if service["port"] not in ports:
            reason = inventory.get("reason") or "port not rendered"
            raise Failure(f"tunnel {config['name']} service {service['name']}: "
                          f"unprotected loopback port {service['port']} ({reason})",
                          "choose a protected port or add it to Crocodile's server.protected_ports "
                          "and apply a site render first")


def require_free_ports(config):
    for service in config["services"]:
        port = service["port"]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as error:
                raise Failure(f"tunnel {config['name']} service {service['name']}: "
                              f"cannot bind loopback port {port}: {error.strerror}",
                              "close a previous tunnel window or choose an unused protected port") from None


def _services(value):
    return [{k: service[k] for k in ("short", "name", "port", "url")} for service in value["services"]]


def _same(path, text, mode=0o600):
    if os.path.lexists(path):
        if _read(path) != text:
            raise Failure(str(path) + ": differs from the saved credential/configuration; retained")
    else:
        _write(path, text, mode)


def look(ctx):
    if ctx.instance is None:
        return Look(any((ctx.home / "instances").glob("*/config.json")))
    path = _site(ctx) / "config.json"
    if not os.path.lexists(path):
        return Look(False)
    try:
        config = _load(ctx)
        system_account(config["account"], _site(ctx) / "ssh", "Boring tunnel " + ctx.instance,
                       AccountIdentity(**config["identity"]))
        key = _site(ctx) / "ssh/.ssh/authorized_keys"
        actual = _read(key) if os.path.lexists(key) else None
        if actual not in (None, "", config["key"]):
            raise Failure("tunnel key admission differs from its configuration; retained")
        if config.get("uninstalled", False):
            return Look(False, notes=["removal requested; uninstall --delete-data can finish retained state"],
                        retained=True)
        manifest = _site(ctx) / "windows/settings.json"
        if not os.path.lexists(manifest):
            return Look(False, notes=["unfinished tunnel install; rerun the reviewed install command"],
                        retained=True)
        if actual != config["key"]:
            raise Failure("tunnel key admission differs from its configuration; retained")
    except (Failure, OSError, KeyError, ValueError, TypeError) as error:
        return Look(False, [str(error)])
    return Look(True)


def ready(ctx, settings):
    value = model.declaration(name=ctx.instance, box=settings["box"],
                              services=settings["services"], callers=settings["callers"])
    if not settings["ssh_host"] or any(char.isspace() for char in settings["ssh_host"]) or "@" in settings["ssh_host"]:
        raise Failure("--ssh-host needs a host name without user or whitespace")
    value["ssh_host"] = value["account"] + "@" + settings["ssh_host"]
    value["key"] = model.authorized_key(value, _read(Path(settings["key"])))
    saved = _site(ctx) / "config.json"
    if os.path.lexists(saved):
        config = _load(ctx)
        if config.get("uninstalled", False):
            raise Failure("this tunnel was removed; retained account and files need an explicit operator decision")
        expected = {k: value[k] for k in ("name", "box", "account", "key", "ssh_host")}
        if any(config.get(k) != v for k, v in expected.items()) or config.get("services") != _services(value):
            raise Failure("unfinished tunnel configuration differs; retain it and review before changing inputs")
        system_account(config["account"], _site(ctx) / "ssh", "Boring tunnel " + ctx.instance,
                       AccountIdentity(**config["identity"]))
    ctx.run("ssh-keygen", "-l", "-f", settings["key"])
    main = _read(SSH_CONFIG)
    dropin = _ssh_dropin(ctx.instance)
    _ssh_candidate(main, dropin)
    existing = _check_ssh_dropin(dropin, model.ssh_policy(value))
    if existing and not os.path.lexists(saved):
        raise Failure(f"{dropin}: customized or foreign SSH drop-in; retained")
    with _directory_fd(dropin.parent):
        pass
    value["ssh_existing"] = existing
    ctx.run("sshd", "-t")
    value["ssh_main"] = main
    inventory = _admin(["list", "--json"])
    authority = inventory.get("public_base_url") if isinstance(inventory, dict) else None
    if not isinstance(authority, str) or not authority.startswith("https://"):
        raise Failure("Crocodile inventory needs its public HTTPS base URL")
    value["crocodile_url"] = authority
    require_protected_ports(value, _admin(["service-ports"]))
    records = _records()
    for service in value["services"]:
        credential = _credential(ctx, service)
        found = [r for r in records if r["name"] == service["name"]]
        for record in records:
            if record["name"] != service["name"] and _loopback_port(record["url"]) == service["port"]:
                raise Failure(f"loopback port {service['port']} is registered to {record['name']}; choose another port")
        if credential is not None:
            if (len(found) != 1 or found[0]["client_id"] != credential["client_id"]
                    or found[0]["url"] != service["url"]):
                raise Failure(service["name"] + ": saved credential no longer matches Crocodile; retained")
            proposal = {"name": service["name"], "action": "keep existing service and grants"}
        else:
            if found:
                raise Failure(service["name"] + ": service exists but its one-time credential was not saved; "
                              "inspect service-list and review service-remove before retrying; nothing reset automatically")
            proposal = _admin(_add(service, value["callers"]))
            if not isinstance(proposal, dict) or "client_secret" in proposal:
                raise Failure("Crocodile returned an invalid service-add plan")
        service["proposal"] = proposal
    require_free_ports(value)
    ctx.tunnel_plan = value


def plan(ctx, settings):
    value = ctx.tunnel_plan
    return [f"create restricted {value['account']}; files retained on uninstall",
            f"write or reuse {_ssh_dropin(ctx.instance)}; verify effective policy and reload ssh.service",
            model.ssh_policy(value), "admit only the public key staged by --key, from " + value["box"],
            *["Crocodile service-add plan: " + json.dumps(s["proposal"], sort_keys=True) for s in value["services"]],
            "credentials go directly to the private Windows handoff; never terminal output",
            "type APPLY before host changes; Crocodile confirms each service grant itself"]


def _effective(ctx, value, config=None):
    extra = ("-f", str(config)) if config is not None else ()
    answer = ctx.run("sshd", "-T", *extra, "-C",
                     f"user={value['account']},host={value['box']},addr={value['box']}").stdout
    parsed = dict(line.split(None, 1) for line in answer.splitlines() if " " in line)
    expected = {name.lower(): setting for name, setting in model.ssh_settings(value).items()}
    expected["usepam"] = "yes"
    mismatches = [name for name, setting in expected.items() if parsed.get(name, "none") != setting]
    if mismatches:
        raise Failure("effective SSH policy differs for " + ", ".join(mismatches)
                      + "; no tunnel key admitted; inspect sshd -T for this account")


def install(ctx, settings):
    if input("Type APPLY to install this tunnel: ").strip() != "APPLY":
        raise Failure("tunnel install declined; nothing changed")
    # Re-read every owner after the operator's decision, before effects.
    ready(ctx, settings)
    value = ctx.tunnel_plan
    site = _site(ctx)
    config_path = site / "config.json"
    if os.path.lexists(config_path):
        config = _load(ctx)
        system_account(value["account"], site / "ssh", "Boring tunnel " + ctx.instance,
                       AccountIdentity(**config["identity"]))
    else:
        # Validate and create the site before useradd. The account's ssh home
        # remains absent until its assigned identity is recorded here.
        new_site = not os.path.lexists(site)
        _directory(site, 0o755)
        if new_site:
            ctx.changed.append("prepared tunnel directory " + str(site))
        identity = create_system_account(value["account"], site / "ssh", "Boring tunnel " + ctx.instance, ctx.run)
        ctx.changed.append("created restricted account " + value["account"])
        config = {key: value[key] for key in ("name", "box", "account", "key", "ssh_host")}
        config.update(identity=dict(uid=identity.uid, gid=identity.gid), services=_services(value))
        _write(config_path, json.dumps(config, indent=2) + "\n")
    _directory(site / "credentials", 0o700)
    _directory(site / "ssh/.ssh", 0o755)
    dropin = _ssh_dropin(ctx.instance)
    policy = model.ssh_policy(value)
    with tempfile.TemporaryDirectory(prefix=".boring-tunnel-", dir=SSH_CONFIG.parent) as scratch:
        candidate, staged = Path(scratch) / "sshd_config", Path(scratch) / dropin.name
        staged.write_text(policy, encoding="utf-8")
        candidate.write_text(_ssh_candidate(value["ssh_main"], dropin, staged), encoding="utf-8")
        ctx.run("sshd", "-t", "-f", str(candidate))
        _effective(ctx, value, candidate)
    if _read(SSH_CONFIG) != value["ssh_main"]:
        raise Failure(f"{SSH_CONFIG} changed during validation; review a fresh install plan")
    if _check_ssh_dropin(dropin, policy) != value["ssh_existing"]:
        raise Failure(f"{dropin}: changed during validation; review a fresh install plan")
    if not value["ssh_existing"]:
        _write(dropin, policy, mode=0o644)
        ctx.changed.append(f"installed SSH drop-in {dropin}")
    ctx.run("sshd", "-t")
    _effective(ctx, value)
    ctx.run("systemctl", "reload", "ssh.service")
    handoff = site / "windows"
    _directory(handoff / "credentials", 0o700)
    windows = dict(box=ctx.instance, ssh_host=value["ssh_host"], crocodile_url=value["crocodile_url"],
                   identity_file="credentials/ssh-key", known_hosts_file="credentials/known_hosts", servers={})
    for service in value["services"]:
        credential = _credential(ctx, service)
        if credential is None:
            added = _admin([*_add(service, value["callers"]), "--apply"], apply=True)
            if (not isinstance(added, dict) or not isinstance(added.get("client_id"), str)
                    or not added["client_id"].startswith("svc_")
                    or not isinstance(added.get("client_secret"), str) or not added["client_secret"]):
                raise Failure("Crocodile credential reply invalid; inspect service-list before retrying")
            credential = {k: added[k] for k in ("client_id", "client_secret")}
            # One atomic credential owner, before deriving the Windows export.
            # If this publication/reply is lost, ready names the unresolved service.
            _write(site / "credentials" / (service["short"] + ".json"),
                   json.dumps(credential) + "\n")
        short = service["short"]
        _same(handoff / "credentials" / (short + "-id"), credential["client_id"] + "\n")
        _same(handoff / "credentials" / (short + "-secret"), credential["client_secret"] + "\n")
        windows["servers"][short] = dict(port=service["port"], client_id_file="credentials/" + short + "-id",
                                         secret_file="credentials/" + short + "-secret")
    # Every grant and credential exists before admission. The ordinary Windows
    # settings manifest is published last: its absence makes interrupted setup resumable.
    _same(site / "ssh/.ssh/authorized_keys", value["key"], mode=0o644)
    _same(handoff / "settings.json", json.dumps(windows, indent=2) + "\n")
    ctx.log("private Windows handoff: " + str(handoff) + "; add desktop SSH files and selected D365 settings")



def _removals(ctx, config):
    records, removals = _records(), []
    for service in config["services"]:
        credential = _credential(ctx, service)
        if credential is None:
            if any(r["name"] == service["name"] for r in records):
                raise Failure(service["name"] + ": no saved one-time credential; ownership unresolved, retained")
            continue
        matching = [r for r in records if r["client_id"] == credential["client_id"]]
        if not matching:
            continue
        if len(matching) != 1 or matching[0]["name"] != service["name"] or matching[0]["url"] != service["url"]:
            raise Failure("registered service definition changed; retained")
        arguments = ["service-remove", service["name"], "--expected-definition", json.dumps(matching[0], sort_keys=True)]
        proposal = _admin(arguments)
        ctx.log("Crocodile service-remove plan: " + json.dumps(proposal, sort_keys=True))
        removals.append(arguments)
    return removals


def uninstall(ctx, delete_data):
    config, site = _load(ctx), _site(ctx)
    key = site / "ssh/.ssh/authorized_keys"
    actual = _read(key) if os.path.lexists(key) else None
    if actual not in (None, "", config["key"]):
        raise Failure("authorized_keys changed; account and files retained")
    identity = AccountIdentity(**config["identity"])
    system_account(config["account"], site / "ssh", "Boring tunnel " + ctx.instance, identity)
    removals = _removals(ctx, config)
    ctx.log("revoke key admission, lock and expire " + config["account"] + "; keep every file")
    if input("Type APPLY to remove this tunnel's grants and lock its account: ").strip() != "APPLY":
        raise Failure("tunnel uninstall declined")
    # Ordinary configuration records requested removal so an interruption can
    # finish through uninstall --delete-data, even after the key has been revoked.
    path = site / "config.json"
    config["uninstalled"] = True
    _write(path, json.dumps(config, indent=2) + "\n", expected=_read(path))
    if actual is not None:
        _write(key, "", mode=0o644, expected=actual)
    lock_system_account(config["account"], site / "ssh", "Boring tunnel " + ctx.instance, identity, ctx.run)
    ctx.changed.append("revoked key admission and locked/expired account; files retained")
    processes = ctx.run("pgrep", "-u", str(identity.uid), check=False)
    if processes.returncode != 1:
        raise Failure("tunnel account still has processes or process check failed; close the Windows tunnel before retrying")
    for arguments in removals:
        _admin([*arguments, "--apply"], apply=True)
    return [str(site)]
