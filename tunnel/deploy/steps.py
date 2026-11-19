"""Checks for the optional configuration-only tunnel release."""
from types import SimpleNamespace
from boring_deploy import Failure
import install_steps
import tunnel_model as model


def _active(ctx):
    # Preflight precedes step zero, which resolves --name. Inspect every
    # existing instance so a plain deploy --check finds prior port drift.
    names = [ctx.instance] if ctx.instance else [
        path.parent.name for path in sorted((ctx.home / "instances").glob("*/config.json"))]
    for name in names:
        site = SimpleNamespace(home=ctx.home, instance=name, run=ctx.run)
        config = install_steps._load(site)
        if not config.get("uninstalled", False):
            yield site, config


def _protection(configs):
    if configs:
        inventory = install_steps._admin(["service-ports"])
        for config in configs:
            install_steps.require_protected_ports(config, inventory)


def preflight(ctx):
    try:
        _protection([config for _, config in _active(ctx)])
    except Failure as error:
        return [f"{error}. {error.hint}" if error.hint else str(error)]
    return []


def check(ctx):
    active = []
    for site, config in _active(ctx):
        name = site.instance
        observed = install_steps.look(site)
        if not observed.installed or observed.irregular:
            raise Failure("tunnel " + name + ": incomplete or irregular installation")
        dropin = install_steps._ssh_dropin(name)
        if not install_steps._check_ssh_dropin(dropin, model.ssh_policy(config)):
            raise Failure(f"{dropin}: managed SSH drop-in is missing")
        active.append(config)
    _protection(active)
    ctx.run("sshd", "-t")
    for config in active:
        install_steps._effective(ctx, config)


def smoke(ctx):
    check(ctx)


def completion(ctx):
    return "is installed; start its separate Windows tunnel window when ready"
