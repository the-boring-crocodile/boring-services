"""The optional tunnel's explicit endpoint declaration and per-account SSH policy.

No default port table or credential/grant store: the operator declares ports;
Crocodile owns grants, client IDs, secrets and public authority.
"""
import base64
import binascii
import ipaddress
import re
from boring_deploy import Failure


SERVICE_NAMES = ("core", "d365", "d365whsapp", "d365web", "d365data")


def declaration(*, name, box, services, callers):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", name):
        raise Failure("--name needs a lowercase name, at most 24 characters")
    try:
        address = ipaddress.ip_address(box)
        if getattr(address, "scope_id", None) is not None:
            raise ValueError("scoped IPv6 address")
        address = str(address)
    except ValueError:
        raise Failure("--box needs one Windows source IP address, not a hostname or subnet") from None
    entries, shorts, ports = [], set(), set()
    for item in services.split(","):
        short, separator, raw = item.partition("=")
        if (not separator or not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", short)
                or short in shorts or not raw.isascii() or not raw.isdigit()):
            raise Failure("--services needs distinct NAME=PORT entries, for example core=18530,d365=18532")
        if short not in SERVICE_NAMES:
            raise Failure(f"--services: unsupported Windows server {short!r}; "
                          + "choose from " + ", ".join(SERVICE_NAMES))
        port = int(raw)
        if not 1024 <= port <= 65535 or port in ports:
            raise Failure("--services needs distinct unprivileged ports from 1024 to 65535")
        shorts.add(short)
        ports.add(port)
        registered = name if short == "core" else name + "-" + short
        entries.append(dict(short=short, name=registered, port=port, url=f"http://127.0.0.1:{port}/mcp"))
    # Crocodile validates caller names, declared roles and grants in its own plan.
    return dict(name=name, box=address, account="tunnel-" + name, services=entries, callers=callers)


def authorized_key(value, text):
    lines = text.strip().splitlines()
    if len(lines) != 1:
        raise Failure("--key needs exactly one plain OpenSSH public key")
    fields = lines[0].split(None, 2)
    if len(fields) < 2 or not fields[0].startswith(("ssh-", "ecdsa-", "sk-")):
        raise Failure("--key needs a plain OpenSSH public key without authorized_keys options")
    try:
        if not base64.b64decode(fields[1], validate=True):
            raise ValueError
    except (ValueError, binascii.Error):
        raise Failure("--key has invalid public-key encoding") from None
    options = ["restrict", "port-forwarding", 'from="' + value["box"] + '"']
    options += ['permitlisten="127.0.0.1:' + str(service["port"]) + '"' for service in value["services"]]
    # ssh-keygen must also validate the staged public key before installation.
    # AllowTcpForwarding remote in ssh_policy is mandatory before admitting it.
    return ",".join(options) + " " + lines[0] + "\n"


def ssh_settings(value):
    """The directives rendered and checked for this account."""
    ports = " ".join("127.0.0.1:" + str(service["port"]) for service in value["services"])
    return {
        "AllowTcpForwarding": "remote",
        "AllowStreamLocalForwarding": "no",
        "PermitOpen": "none",
        "PermitListen": ports,
        "GatewayPorts": "no",
        "MaxSessions": "0",
        "AuthenticationMethods": "publickey",
        "AuthorizedKeysFile": ".ssh/authorized_keys",
        "AuthorizedKeysCommand": "none",
        "TrustedUserCAKeys": "none",
        "PasswordAuthentication": "no",
        "KbdInteractiveAuthentication": "no",
        "AllowAgentForwarding": "no",
        "X11Forwarding": "no",
        "PermitTTY": "no",
        "PermitTunnel": "no",
    }


def ssh_policy(value):
    lines = [f"\n# boring-tunnel {value['name']}", f"Match User {value['account']}"]
    lines.extend(f"    {key} {setting}" for key, setting in ssh_settings(value).items())
    return "\n".join(lines) + "\n"
