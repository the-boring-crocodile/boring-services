"""Deployment generation reads and backups use the named SQLite file."""
from contextlib import closing
import sqlite3
from types import SimpleNamespace

from deploy_context import deploy_context
from test_deploy_smoke import load_steps


def test_generation_and_backup_round_trip_a_path_with_a_fragment(tmp_path):
    home = tmp_path / "Notes #2"
    home.mkdir()
    database = home / "notebook.db"
    with closing(sqlite3.connect(database)) as db:
        db.execute("CREATE TABLE entries(title)")
        db.execute("INSERT INTO entries VALUES (?)", ("fixture",))
        db.commit()
    ctx = deploy_context(home=home, release=SimpleNamespace(details={
        "data": {"file": "notebook.db", "query": "SELECT count(*) FROM entries"}}))
    steps = load_steps()
    assert steps.data_generation(ctx) == 1
    copies = tmp_path / "copies"
    copies.mkdir()
    target = steps.backup(ctx, copies)
    assert target == copies / database.name
    with closing(sqlite3.connect(target)) as db:
        assert db.execute("SELECT title FROM entries").fetchall() == [("fixture",)]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Notes #2", "copies"]
    assert sorted(p.name for p in home.iterdir()) == ["notebook.db"]
