"""The canonical Ferry parser lets the root installer own file admission."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config(monkeypatch):
    spec = importlib.util.spec_from_file_location("input_ferry_config", ROOT / "mcp-ferry/src/boring_mcp_ferry/config.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_injected_reader_owns_every_content_read(config, tmp_path):
    path = tmp_path / "config/config.json"
    documents = {
        path: json.dumps({"backends": {"a": {"service": "front.json",
            "upstream": {"url_file": "../url", "headers_file": "../headers.json"},
            "guide_file": "../guide.md"}}}),
        path.parent / "../url": "http://example.test/mcp\n",
        path.parent / "../headers.json": '{"X-Api-Key": "synthetic"}',
        path.parent / "../guide.md": "A guide",
    }
    calls = []
    def read_text(source, encoding):
        calls.append((source, encoding))
        return documents[source]
    ferry = config.read(path, read_text=read_text)
    backend, = ferry.backends
    assert (backend.url, backend.headers, backend.guide) == (
        "http://example.test/mcp", {"X-Api-Key": "synthetic"}, "A guide")
    assert calls == [(path, "utf-8"), (path.parent / "../url", "utf-8"),
                     (path.parent / "../headers.json", "utf-8"), (path.parent / "../guide.md", "utf-8-sig")]


@pytest.mark.parametrize("field", ["config", "url_file", "headers_file", "guide_file"])
def test_reader_refusal_is_never_hidden_by_optional_guide_or_json_diagnostics(config, tmp_path, field):
    class InputRefused(Exception):
        pass
    path = tmp_path / "config.json"
    data = {"backends": {"a": {"service": "front.json", "upstream": {
        "url_file": "url", "headers_file": "headers.json"}, "guide_file": "guide.md"}}}
    documents = {"config.json": json.dumps(data), "url": "http://example.test/mcp",
                 "headers.json": "{}", "guide.md": "guide"}
    blocked = {"config": "config.json", "url_file": "url", "headers_file": "headers.json",
               "guide_file": "guide.md"}[field]
    def read_text(source, encoding):
        if source.name == blocked:
            raise InputRefused("unsafe input")
        return documents[source.name]
    with pytest.raises(InputRefused, match="unsafe input"):
        config.read(path, read_text=read_text)
