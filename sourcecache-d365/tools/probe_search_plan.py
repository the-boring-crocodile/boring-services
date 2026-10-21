"""Measure the actual Index.search SQL on disposable synthetic data only."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time

from boring_sourcecache_d365.sourcecache import Index, SCHEMA


def main():
    with tempfile.TemporaryDirectory(prefix="sourcecache-plan-") as folder:
        path = Path(folder) / "synthetic.db"
        db = sqlite3.connect(path)
        db.executescript(SCHEMA)
        db.execute("INSERT INTO models VALUES ('Synthetic', 'Fixture', 0, 'sys', 'Microsoft', '1', '')")
        db.executemany(
            "INSERT INTO files VALUES (?, ?, 'Synthetic', 'Fixture', 'AxTable', ?, 'element', NULL, NULL, 100)",
            ((n, f"fixture/{n}.xml", f"Table{n:05d}") for n in range(1, 10001)))
        db.executemany("INSERT INTO content (rowid,text) VALUES (?,?)",
                       ((n, "select firstonly\n" +
                         ("static SyntheticTable find(\n" if n == 9999 else "unrelated body\n"))
                        for n in range(1, 10001)))
        db.commit()
        db.close()
        index = Index(str(path), stall_seconds=2)
        try:
            for label, query, element in (
                ("rare-unfiltered", "static SyntheticTable find(", None),
                ("rare-filtered", "static SyntheticTable find(", "AxTable"),
                ("dense-filtered", "select firstonly", "AxTable"),
            ):
                statements = []
                index.db.set_trace_callback(statements.append)
                start = time.monotonic()
                try:
                    result = index.search(query, element_type=element, limit=5)
                    outcome = {"total": result["total_files"]}
                except Exception as error:
                    outcome = {"error": type(error).__name__}
                elapsed = time.monotonic() - start
                index.db.set_trace_callback(None)
                plans = []
                for sql in statements:
                    if "FROM content" in sql and "JOIN files" in sql:
                        plans.append({"kind": "count" if "COUNT(*)" in sql else "rows",
                                      "plan": [r[3] for r in index.db.execute("EXPLAIN QUERY PLAN " + sql)]})
                print(json.dumps({"case": label, "sqlite": sqlite3.sqlite_version,
                                  "seconds": round(elapsed, 4), **outcome, "plans": plans}), flush=True)
        finally:
            index.db.close()


if __name__ == "__main__":
    main()
