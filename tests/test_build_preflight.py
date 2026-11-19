"""The builder checks every input before it creates any output."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("family_build", ROOT / "scripts/build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def test_ferry_artifact_installer_uses_the_packaged_config_owner(tmp_path):
    import json
    import subprocess
    import sys
    from boring_deploy import artifact

    out = tmp_path / "out"
    image = out / "mcp-ferry"
    image.mkdir(parents=True)
    (image / "Dockerfile").write_text("FROM scratch\n")
    record = {"commit": "0" * 40, "services": {"mcp-ferry": {
        "wheel": "boring_mcp_ferry-1.0.0-py3-none-any.whl"}}}
    archive = build.release_artifact(artifact, out, "mcp-ferry", record)
    directory = archive.parent / "boring-mcp-ferry-1.0.0"
    assert (directory / "deploy.d/ferry_config.py").read_bytes() == (
        ROOT / "mcp-ferry/src/boring_mcp_ferry/config.py").read_bytes()
    home = tmp_path / "home"
    (home / "config/services").mkdir(parents=True)
    (home / "config/config.json").write_text(json.dumps({"backends": {
        "a": {"service": "selected.json", "upstream": {"url": "http://a/mcp"}}}}))
    (home / "config/selected.json").write_text("{")
    (home / "config/services/unused.json").write_text("{")
    code = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import boring_deploy, install_steps, functools, os
install_steps.root_input = functools.partial(boring_deploy.root_input, owner=os.getuid())
from types import SimpleNamespace
ctx = boring_deploy.Context(SimpleNamespace(component='boring-mcp-ferry'),
                            Path(sys.argv[2]), None, lambda message: None)
try:
    install_steps._proposed_inputs(ctx, {"config_dir": str(Path(sys.argv[2]) / "config")})
except install_steps.Failure as error:
    assert "selected.json" in str(error), str(error)
else:
    raise AssertionError("malformed selected frontend accepted")
print('packaged preflight ok')
"""
    result = subprocess.run([sys.executable, "-I", "-c", code, str(directory / "deploy.d"), str(home)],
                            check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "packaged preflight ok"


def test_missing_common_wheels_refused_before_output(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "run", lambda *a, **k: pytest.fail("build tool ran"))
    out = tmp_path / "release"
    with pytest.raises(FileNotFoundError, match="Common wheels"):
        build.build(out, "linux", ("fishmarket",), common_wheels=tmp_path / "absent")
    assert not out.exists()


def test_missing_service_lock_refused_before_output(tmp_path, monkeypatch):
    source = tmp_path / "layout"
    (source / "fishmarket").mkdir(parents=True)
    monkeypatch.setattr(build, "ROOT", source)
    monkeypatch.setattr(build, "run", lambda *a, **k: pytest.fail("build tool ran"))
    out = tmp_path / "release"
    with pytest.raises(FileNotFoundError, match="requirements.lock"):
        build.build(out, "linux", ("fishmarket",), common_wheels=tmp_path)
    assert not out.exists()


def test_every_service_has_a_lock_and_a_locked_dockerfile():
    for service in build.SERVICES:
        assert (ROOT / service / "requirements.lock").is_file(), service
        dockerfile = (ROOT / build.DOCKERFILE.get(service, f"{service}/Dockerfile")).read_text()
        assert "--require-hashes" in dockerfile and "ARG PYTHON_IMAGE=" in dockerfile, service

@pytest.mark.parametrize("service", build.SERVICES)
def test_service_wheel_carries_declared_licence_files(tmp_path, monkeypatch, service):
    """Run the real wheel builder; only dependency downloads and assembly are faked."""
    import zipfile
    from boring_deploy import develop

    real_run = build.run
    def run(*args, **kwargs):
        if len(args) > 1 and args[1] == "venv":
            return ""
        if len(args) > 2 and args[1:3] == ("-m", "pip"):
            return ""
        result = real_run(*args, **kwargs)
        return result

    monkeypatch.setattr(build, "load", lambda _: (None, develop))
    monkeypatch.setattr(build, "run", run)
    monkeypatch.setattr(develop, "target_lock", lambda python, source, *args: source)
    monkeypatch.setattr(build, "release_artifact", lambda *args: "assembly not exercised here")
    out = tmp_path / "release"
    build.build(out, "linux", (service,), common_wheels=tmp_path)
    (wheel,) = (out / service).glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if not name.endswith("/"):
                assert len(name.rsplit("/", 1)[-1]) <= 100, name
                assert len(name) <= 160, name  # relative to site-packages
        for filename in ("LICENSE", "COPYING.md"):
            (member,) = [name for name in archive.namelist()
                         if name.endswith(".dist-info/licenses/" + filename)]
            assert archive.read(member) == (ROOT / service / filename).read_bytes()
