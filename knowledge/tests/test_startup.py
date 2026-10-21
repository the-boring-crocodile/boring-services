"""Invalid startup configuration must not create or migrate a notebook."""
import json
import sqlite3
import pytest
from boring_knowledge.server import main

@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("config_kind", ["missing", "malformed", "missing_secret"])
def test_invalid_http_config_has_no_database_effects(tmp_path, existing, config_kind):
    db = tmp_path / "notebook.db"
    if existing:
        with sqlite3.connect(db) as connection:
            connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            connection.executemany("INSERT INTO meta VALUES (?, ?)", [("schema", "1"), ("role", "global")])
        before = db.read_bytes()
    args = ["http", "--db", str(db)]
    if config_kind != "missing":
        config = tmp_path / "config.json"
        config.write_text("{" if config_kind == "malformed" else json.dumps({
            "url": "http://127.0.0.1:18512/mcp",
            "oauth": {"issuer": "https://issuer.example", "introspection_url": "https://issuer.example/introspect",
                      "client_id": "fixture", "secret_file": "not-present"},
            "access": []}))
        args += ["--config", str(config)]
    with pytest.raises((SystemExit, ValueError, OSError)):
        main(args)
    if existing:
        assert db.read_bytes() == before
    else:
        assert not db.exists()
    assert not list(tmp_path.glob("notebook.db-*"))
