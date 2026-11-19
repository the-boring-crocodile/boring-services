"""A rejected configuration uses the shared request cause before serving."""
import json
import pytest
from boring_mcp_ferry.server import main


def test_invalid_config_is_refused_invalid_request(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text('{"not_backends": {}}')
    with pytest.raises(SystemExit) as stopped:
        main(["http", "--config", str(config)])
    assert stopped.value.code == 2
    outcome = json.loads(capsys.readouterr().err)
    assert outcome["status"] == "refused"
    assert outcome["code"] == "invalid_request"
