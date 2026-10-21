"""The builder checks every input before it creates any output."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("family_build", ROOT / "scripts/build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


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
