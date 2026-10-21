"""Rare filtered search must not perform FTS matching once per metadata row."""
import sqlite3

from boring_sourcecache_d365.sourcecache import Index, SCHEMA


def test_rare_match_with_metadata_filter_has_bounded_query_work(tmp_path):
    path = tmp_path / "synthetic.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.executemany(
        "INSERT INTO files VALUES (?, ?, 'Fixture', 'Model', 'AxTable', ?, 'element', NULL, NULL, 20)",
        ((n, f"{n}.xml", f"Name{n:05d}") for n in range(1, 10001)))
    db.executemany("INSERT INTO content(rowid,text) VALUES (?,?)",
                   ((n, "rare synthetic needle" if n == 9999 else "ordinary text")
                    for n in range(1, 10001)))
    db.commit()
    db.close()
    index = Index(str(path), stall_seconds=0)
    steps = 0

    def budget():
        nonlocal steps
        steps += 100
        return int(steps > 10000)

    index.db.set_progress_handler(budget, 100)
    try:
        found = index.search("rare synthetic needle", element_type="AxTable", limit=5)
        assert found["total_files"] == 1
        assert [r["name"] for r in found["results"]] == ["Name09999"]
        assert steps <= 10000
    finally:
        index.db.close()
