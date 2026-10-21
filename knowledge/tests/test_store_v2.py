"""Schema 3: confirmations, age, snippets around the hit, context through FTS, bounded totals."""

from __future__ import annotations

import sqlite3

import pytest

from boring_knowledge import store as store_module
from boring_knowledge.store import Store
from boring_service_kit.outcomes import Refused


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "notebook.db"))


def test_confirm_is_a_version_with_fresh_evidence(store):
    entry = store.add("pond", "A quirk", "Body text.", evidence="seen once")
    confirmed = store.confirm("garden", entry["id"], 1, "seen again on Windows CI run 3")
    assert confirmed["version"] == 2 and confirmed["kind"] == "confirm" and confirmed["author"] == "garden"
    assert confirmed["body"] == "Body text." and confirmed["evidence"] == "seen again on Windows CI run 3"
    assert confirmed["reason"] == "confirmed against fresh evidence"
    kinds = [h["kind"] for h in store.get(entry["id"], history=True)["history"]]
    assert kinds == ["confirm", "add"]
    with pytest.raises(Refused):
        store.confirm("garden", entry["id"], 2, "")


def test_age_days_and_max_age_filter(store):
    entry = store.add("pond", "Fresh entry", "New.")
    assert store.get(entry["id"])["age_days"] == 0.0
    store.db.execute("UPDATE entries SET updated = '2020-01-01T00:00:00Z' WHERE id = ?", (entry["id"],))
    store.add("pond", "Another fresh one", "Newer.")
    assert store.search(max_age_days=30)["total"] == 1
    assert store.search()["total"] == 2
    with pytest.raises(Refused):
        store.search(max_age_days=0)


def test_snippet_is_centred_on_the_hit(store):
    body = "Filler words. " * 40 + "The needle sits here." + " More filler." * 40
    store.add("pond", "Long entry", body)
    snippet = store.search("needle")["entries"][0]["snippet"]
    assert "needle" in snippet and snippet.startswith("...") and snippet.endswith("...")
    assert len(snippet) <= store_module.SNIPPET_CHARS + 6


def test_context_goes_through_the_index(store):
    store.add("pond", "D365 thing", "Body.", context="Dynamics 365 SCM 10.0.49")
    store.add("pond", "Other thing", "Body.", context="Ubuntu 22.04")
    assert store.search(context="10.0.49")["total"] == 1
    assert store.search(context="ubuntu")["total"] == 1  # case-insensitive
    assert store.search("Body", context="Ubuntu")["total"] == 1
    with pytest.raises(Refused):
        store.search(context="ab")


def test_totals_are_bounded(store, monkeypatch):
    monkeypatch.setattr(store_module, "COUNT_CAP", 3)
    for n in range(5):
        store.add("pond", f"Entry {n} about jobs", "jobs everywhere")
    result = store.search("jobs", limit=2)
    assert result["total"] == 3 and result["total_capped"] and result["truncated"]
    assert "total_capped" not in store.search("about \"4\"")


def test_recent_uses_the_updated_index(store):
    plan = " ".join(row[3] for row in store.db.execute(
        "EXPLAIN QUERY PLAN SELECT e.id FROM entries e WHERE e.retired = 0 ORDER BY e.updated DESC LIMIT 20"))
    assert "entries_updated" in plan


def test_the_full_text_query_runs_once(store):
    """Regression: as a join, SQLite re-ran MATCH per entry (8 s at 10,000). Now a once-evaluated set."""
    for n in range(30):
        store.add("pond", f"Entry {n}", "timeout job body", context="product 3.0")
    sql = ("SELECT e.id FROM entries e WHERE e.id IN (SELECT rowid FROM entry_text WHERE entry_text MATCH ?) "
           "AND e.retired = 0 ORDER BY e.updated DESC, e.id DESC LIMIT 20")
    plan = " | ".join(row[3] for row in store.db.execute("EXPLAIN QUERY PLAN " + sql, ['"timeout job"']))
    assert "LIST SUBQUERY" in plan, plan  # evaluated once into a lookup set
    assert store.search("timeout job")["total"] == 30
    assert store.search("timeout job", context="product 3")["total"] == 30


def test_keyword_search_across_fields_and_evidence(store):
    entry = store.add("meadow", "WMOM Message processor", "The message processor starts later.",
                      context="mandatory from 10.0.47", evidence="ordering entity 10948")
    other = store.add("meadow", "Other service", "No processor here.", evidence="ticket 7345")
    for query in ("WMOM", "Message processor", "WMOM processor", "processor WMOM",
                  "WMOM message processor", "ordering entity", "10948", "mandatory from 10.0.47"):
        assert [row["id"] for row in store.search(query)["entries"]] == [entry["id"]], query
    assert store.search("WMOM 7345")["total"] == 0
    assert store.search("processor", context="10.0.47")["total"] == 1
    assert store.search("processor", context="nonexistent")["total"] == 0
    store.revise("meadow", entry["id"], 1, "new evidence", evidence="changed citation")
    assert store.search("10948")["total"] == 0
    assert store.search("changed citation")["total"] == 1
    store.confirm("garden", entry["id"], 2, "fresh source 8711")
    assert store.search("changed citation")["total"] == 0
    assert store.search("source 8711")["total"] == 1
    assert other["id"] != entry["id"]
    assert store.search("WMOM xx")["total"] == 0
    for query in ("xx", " "):
        with pytest.raises(Refused) as exc:
            store.search(query)
        assert exc.value.code == "invalid_request"


def test_an_older_notebook_is_refused_unchanged(tmp_path):
    path = tmp_path / "notebook.db"
    Store(str(path)).db.close()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE meta SET value='2' WHERE key='schema'")
    before = path.read_bytes()
    with pytest.raises(Refused, match="schema 2; this release reads schema 3"):
        Store(str(path))
    assert path.read_bytes() == before
