import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import pytest
from boring_knowledge.store import Store
from boring_service_kit.outcomes import Refused

def test_future_schema_refused_without_modification(tmp_path):
    path = tmp_path / "future.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    db.executemany("INSERT INTO meta VALUES (?, ?)", [("schema", "999"), ("role", "global")])
    db.commit()
    db.close()
    before = path.read_bytes()
    with pytest.raises(Refused) as refusal:
        Store(str(path))
    assert refusal.value.code == "unsupported"
    assert path.read_bytes() == before

def test_full_evidence_and_two_writers_survive_restart(tmp_path):
    path = str(tmp_path / "kb.db")
    store = Store(path)
    entry = store.add("legacy-pond", "Shared entry", "original")
    revised = store.revise("developer", entry["id"], 1, "corrected", body="new")
    confirmed = store.confirm("legacy-pond", entry["id"], 2, "x" * 2000)
    assert revised["author"] == "developer" and confirmed["evidence"] == "x" * 2000
    store.db.close()
    reopened = Store(path)
    history = reopened.get(entry["id"], history=True)["history"]
    assert [h["author"] for h in history] == ["legacy-pond", "developer", "legacy-pond"]
    assert [h["kind"] for h in history] == ["confirm", "revise", "add"]
    current = reopened.get(entry["id"])
    assert current["version"] == 3 and current["body"] == "new" and current["evidence"] == "x" * 2000
    reopened.db.close()
    with closing(sqlite3.connect(path)) as db:
        stored = db.execute("SELECT version, author, body, evidence FROM versions WHERE entry_id = ? "
                            "ORDER BY version", (entry["id"],)).fetchall()
    assert stored == [(1, "legacy-pond", "original", None), (2, "developer", "new", None),
                      (3, "legacy-pond", "new", "x" * 2000)]

def test_concurrent_expected_version_has_one_winner(tmp_path):
    store = Store(str(tmp_path / "kb.db"))
    entry = store.add("pond", "Shared entry", "original")
    def revise(author):
        try:
            return store.revise(author, entry["id"], 1, "race", body=author)["author"]
        except Refused as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(revise, ["alice", "bob"]))
    assert result.count("stale") == 1
    assert store.get(entry["id"])["author"] in {"alice", "bob"}
    winner = next(r for r in result if r != "stale")
    current = store.get(entry["id"], history=True)
    assert current["version"] == 2 and len(current["history"]) == 2
    assert current["author"] == winner and current["body"] == winner
