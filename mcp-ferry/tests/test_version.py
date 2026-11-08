"""The version the ferry reports to backends is the package's own (it said 0.1.3 through 0.1.4)."""
import re
from pathlib import Path

from boring_mcp_ferry import __version__


def test_version_matches_pyproject():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert __version__ == re.search(r'^version = "([^"]+)"', text, re.M).group(1)
