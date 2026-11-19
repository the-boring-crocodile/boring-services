# Boring Tunnel (optional, Linux)

The Linux half of pondNT's separate desktop tunnel. It installs a restricted
SSH forwarding account and registers the selected Windows servers with the
installed Crocodile. It has no daemon, container or application runtime.
Other Services, Pirates and the Crocodile do not depend on it.

The release uses Common's normal deploy, release verification and operation lock.
The host needs Python 3.12 or newer, OpenSSH server/client tools, systemd's
ssh.service and the matching Crocodile admin. Windows must reach the Crocodile's
public HTTPS URL and this host's SSH port 22. The host's effective SSH policy
must allow the exact restrictions shown in the plan. Review earlier
Match rules for the actual Windows source and Linux listener if the host uses
host names, local addresses or ports in its SSH policy.

## Install one box

Stage the Windows person's plain public SSH key in a root-owned, protected file.
Keep the private key on Windows. The standard tunnel range is 18530–18539.
Select unused ports, identical on both ends. The installer checks that the
ports are free and that Crocodile protects them from Pond and Pirate binds.
For a custom port, add it to `server.protected_ports` in the Crocodile
description and apply its site render before installing the tunnel. Do the
same before upgrading a retained instance whose ports sit outside the
protected ranges. The names core, d365, d365whsapp, d365web and d365data select
pondNT's servers.
The box name prefixes their Crocodile registration names (core is just BOX).
The caller list is explicit; Crocodile owns grants and reader/writer roles.

    sudo RELEASE/deploy --install --name box --box 203.0.113.7 --key /root/box.pub --ssh-host ssh.example.test --services core=18530,d365=18532,d365whsapp=18534,d365web=18536,d365data=18538 --callers pond=writer

Review the plan and type APPLY. Crocodile separately shows and confirms each
service-add plan. Adding --check to the same command writes nothing.
The account is tunnel-box, with its private group and nologin shell.
The plan names `/etc/ssh/sshd_config.d/92-boring-tunnel-NAME.conf`, the
component's per-instance Match User drop-in. The main sshd_config must have an
Include that matches this file. The drop-in limits remote TCP forwarding to
the selected loopback ports, disables local and Unix-socket forwarding and
shell sessions, and selects the managed authorized key file. Installation
checks the staged config with `sshd -t` and checks the account's effective
settings with `sshd -T -C` before publication. After publication it checks
again before reload and key admission. Earlier matching settings can take
precedence. The main config and other drop-ins stay operator-owned. A changed
or foreign file at the planned path is refused.

The private handoff is /opt/boring-tunnel/instances/box/windows/. Transfer it
through an operator-approved private channel to the Windows person's private
tunnel directory; never publish it or print its credentials. Add the desktop's
SSH private key and verified known_hosts file at the paths settings.json names.
Warehouse, browser and data entries also need that person's application settings,
as the [Windows guide](https://github.com/the-boring-crocodile/boring-pondnt/blob/main/docs/DEVBOX_TUNNEL.md)
describes. Linux cannot supply those desktop secrets/settings.
Crocodile's inventory supplies crocodile_url; no copied role/access policy.

## Update or recover

    sudo RELEASE/deploy --name box

Release update checks the retained account/key and effective SSH policy.
`deploy --check` names any retained instance port missing from the
Crocodile's protected set. A running tunnel may already hold its own ports
during an ordinary release update.
If first installation was interrupted, rerun its original install command:
saved identity and credentials are reused and existing grants are kept.
Different pending inputs or changed identities/credentials refuse.
An existing service without a saved one-time credential is an explicit unknown
outcome: inspect service-list and review its service-remove plan before retrying.
Nothing automatically resets credentials, deletes grants or adopts an account.
If account creation succeeded but its initial config could not be saved, its
unrecorded identity also needs operator reconciliation; it is never guessed.
No second receipt store or process record is maintained.

## Uninstall

Close the Windows tunnel window first, then:

    sudo RELEASE/uninstall --name box --yes --delete-data

Review the removal plans and type APPLY. Key admission is revoked; the matched
account is password-locked and expired. Existing processes stop removal before
any grant is removed: close the old connection and rerun the command.
Each matching service is removed through Crocodile's guarded service-remove.
Here --delete-data also permits finishing retained partial removal: it does
not delete the account or any files. Account, group, SSH policy, configuration,
credentials, Windows handoff and release files stay for explicit operator review.
Uninstall also retains the per-instance SSH drop-in for the locked account.
Core grants are for fully trusted callers: even file reads can reveal other
credentials. Give a reader only the selected specialist registration.

## Build source

    python3 scripts/build_tunnel.py --common-wheels COMMON_WHEELS --out NEW_DIRECTORY

This optional artifact is separate from the standard service image builds.
