"""Filesystem acceptance: installed owners, cross-process lock and publication."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from boring_fishmarket.storage import Store
from boring_fishmarket.helper import TransferClient
from test_market import market as market_fixture, authenticated, upload, owner, URL

market = market_fixture


def test_process_lock_released_after_child_exit(tmp_path):
    root = tmp_path / "state"
    child = subprocess.Popen(
        [
            sys.executable,
            "-I",  # ignores PYTHONPATH: the parent's import path is passed explicitly
            "-c",
            "import json, sys; sys.path[:0] = json.loads(sys.argv[2]); "
            "from boring_fishmarket.storage import Store; "
            "s=Store(sys.argv[1]); print('ready', flush=True); sys.stdin.readline(); s.close()",
            str(root),
            json.dumps(sys.path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        # Bound readiness; finally terminates and reaps only this fixture child.
        import threading

        ready = threading.Event()
        lines = []

        def read():
            lines.append(child.stdout.readline())
            ready.set()

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        assert ready.wait(10), "lock fixture did not start"
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert lines == ["ready\n"]
        with pytest.raises(ValueError):
            Store(root)
    finally:
        if child.poll() is None:
            try:
                child.communicate("\n", timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=5)
        child.stdin.close()
        child.stdout.close()
    assert child.returncode == 0
    recovered = Store(root)
    recovered.close()


def test_publication_collision_keeps_remote_and_existing_file(
    market, tmp_path, monkeypatch
):
    async def run():
        app, store, key, *_ = market
        async with app.web.router.lifespan_context(app.web):
            async with await authenticated(app, key) as http:
                async with TransferClient(URL, key, http) as client:
                    receipt = await client.call(
                        "create_transfer", {"direction": "upload", "max_bytes": 10}
                    )
                    assert (await upload(http, receipt, b"new")).status_code == 200
                    target = tmp_path / "received-ä.txt"
                    link = os.link

                    def compete(source, destination, *args, **kwargs):
                        Path(destination).write_bytes(b"existing")
                        return link(source, destination, *args, **kwargs)

                    with monkeypatch.context() as patch:
                        patch.setattr(os, "link", compete)
                        with pytest.raises(FileExistsError):
                            await client.collect(receipt["id"], target)
                    assert target.read_bytes() == b"existing"
                    assert store.status(receipt["id"], owner())["state"] == "ready"
                    assert not list(tmp_path.glob(".fishmarket-*"))
                    target.unlink()
                    await client.collect(receipt["id"], target)
                    assert target.read_bytes() == b"new"

    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="native Windows DACL acceptance")
def test_collected_file_inherits_private_parent_acl(market, tmp_path):
    # Python's Windows 0700 temp directories can use OWNER RIGHTS. Verify
    # its actual owner, allowed principals and unchanged inherited rights.
    async def run():
        app, _, key, *_ = market
        async with app.web.router.lifespan_context(app.web):
            async with await authenticated(app, key) as http:
                async with TransferClient(URL, key, http) as client:
                    receipt = await client.call(
                        "create_transfer", {"direction": "upload", "max_bytes": 10}
                    )
                    assert (await upload(http, receipt, b"private")).status_code == 200
                    target = tmp_path / "received.txt"
                    await client.collect(receipt["id"], target)
                    script = """$ErrorActionPreference='Stop'
$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
function Principal($id) {
  if($id -eq $sid){return 'current-user'}
  if($id -eq 'S-1-5-18'){return 'SYSTEM'}
  if($id -eq 'S-1-5-32-544'){return 'Administrators'}
  if($id -eq 'S-1-3-4'){return 'OWNER RIGHTS'}
  return 'other'
}
function Describe($path) {
  $a=Get-Acl -LiteralPath $path
  $rows=@(foreach($e in $a.Access) {
    $id=$e.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
    $who=Principal $id
    [ordered]@{principal=$who;type=[string]$e.AccessControlType;inherited=$e.IsInherited;rights=[int]$e.FileSystemRights}
  })
  [ordered]@{owner=(Principal $a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value);protected=$a.AreAccessRulesProtected;rules=$rows}
}
$parent=Describe (Split-Path -Parent $env:FISHMARKET_ACL_PATH)
$file=Describe $env:FISHMARKET_ACL_PATH
@{parent=$parent;file=$file} | ConvertTo-Json -Compress -Depth 5
if(@($file.rules).Count -eq 0 -or -not $parent.protected){exit 2}
$trusted=@('current-user','SYSTEM','Administrators')
if($parent.owner -notin $trusted -or $file.owner -notin $trusted){exit 4}
foreach($e in $parent.rules){if($e.type -ne 'Allow' -or $e.principal -eq 'other'){exit 3}}
foreach($e in $file.rules){if($e.type -ne 'Allow' -or -not $e.inherited -or $e.principal -eq 'other'){exit 3}}
$p=@($parent.rules | ForEach-Object { "$($_.principal):$($_.type):$($_.rights)" } | Sort-Object)
$f=@($file.rules | ForEach-Object { "$($_.principal):$($_.type):$($_.rights)" } | Sort-Object)
if(@(Compare-Object $p $f).Count -ne 0){exit 5}
"""
                    result = subprocess.run(
                        [
                            "powershell.exe",
                            "-NoProfile",
                            "-NonInteractive",
                            "-Command",
                            script,
                        ],
                        env={**os.environ, "FISHMARKET_ACL_PATH": str(target)},
                        capture_output=True,
                        timeout=15,
                    )
                    assert result.returncode == 0, (
                        "collected file DACL differs from private parent contract; "
                        + result.stdout.decode("utf-8", errors="replace")
                    )

    asyncio.run(run())
