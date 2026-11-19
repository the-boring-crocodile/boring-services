"""Exercise CLI wiring without binding sockets or reading credentials."""
import pytest
from unittest.mock import patch
from boring_canvas import server


def test_config_reaches_server(tmp_path):
    (tmp_path / "keys.json").write_text('{"issuer": "key:canvas-test", "keys": []}')
    (tmp_path / "config.json").write_text('{"url": "http://127.0.0.1:18511/mcp", "mode": "keys", "keys": {"file": "keys.json"}}')
    argv = ["boring-canvas", "--config", str(tmp_path / "config.json")]
    with patch("sys.argv", argv), patch.object(server, "serve") as serve:
        server.main()
    assert serve.call_count == 1
    assert serve.call_args.args[1].url == "http://127.0.0.1:18511/mcp"


def test_without_config_is_refused():
    with patch("sys.argv", ["boring-canvas"]), patch.object(server, "serve") as serve:
        with pytest.raises(SystemExit):
            server.main()
    serve.assert_not_called()
