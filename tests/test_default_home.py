"""The default service home the README promises is the one release metadata carries."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_names_the_release_default_home():
    build = (ROOT / "scripts" / "build.py").read_text()
    default = re.search(r'"default_home": f"([^"]+)"', build).group(1)
    assert default == "/opt/boring-{service}"
    readme = " ".join((ROOT / "README.md").read_text().split())
    assert "by default the release's `/opt/boring-SERVICE`" in readme
    assert "by default the release's `/opt/docker/" not in readme
