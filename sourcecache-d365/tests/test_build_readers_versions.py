"""Parallel reads build the same index as one reader; the release stamp is never guessed."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import corpus
from boring_sourcecache_d365 import server, sourcecache
from boring_sourcecache_d365.sourcecache import Index, build
from boring_service_kit.outcomes import Refused

TABLES = {
    "files": "SELECT id, path, package, model, element_type, name, kind, locale, label_file, bytes FROM files",
    "content": "SELECT rowid, text FROM content",
    "labels": "SELECT id, file_id, label_file, locale, key, ref, text, description FROM labels",
    "label_refs": "SELECT ref, file_id, line FROM label_refs ORDER BY file_id, line, ref",
    "models": "SELECT * FROM models ORDER BY package, model",
    "meta": "SELECT key, value FROM meta WHERE key NOT IN ('snapshot_id', 'built') ORDER BY key",
}


def rows(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return {table: db.execute(query).fetchall() for table, query in TABLES.items()}


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    root = tmp_path_factory.mktemp("tree")
    base = corpus.write(root)
    # More files than the read-ahead window, so order across batches is exercised.
    extra = base / "ContosoExtensions" / "ContosoExtensions" / "AxClass"
    for number in range(300):
        (extra / f"ContosoGenerated{number:03}.xml").write_text(
            corpus.element("AxClass", f"ContosoGenerated{number:03}", f"  <Source>@SYS1 line {number}</Source>"))
    return base


@pytest.mark.parametrize("readers", [2, 8, 32])
def test_parallel_reads_store_exactly_what_one_reader_stores(tree, tmp_path, readers):
    build(str(tree), str(tmp_path / "one.db"), "same", readers=1)
    build(str(tree), str(tmp_path / "many.db"), "same", readers=readers)
    one, many = rows(tmp_path / "one.db"), rows(tmp_path / "many.db")
    assert one["files"] and one["label_refs"]
    for table in TABLES:
        assert many[table] == one[table], table


def test_a_reader_failure_fails_the_build_and_publishes_nothing(tree, tmp_path, monkeypatch):
    real = sourcecache._read

    def broken(root, path, wanted):
        if path.name == "ContosoGenerated150.xml":
            raise RuntimeError("reader broke")
        return real(root, path, wanted)

    monkeypatch.setattr(sourcecache, "_read", broken)
    with pytest.raises(RuntimeError, match="reader broke"):
        build(str(tree), str(tmp_path / "x.db"), "broken", readers=8)
    assert list(tmp_path.iterdir()) == []  # neither the index nor the staged file


@pytest.mark.parametrize("readers", [0, 65, "8", 2.0])
def test_readers_out_of_range_are_refused(tree, tmp_path, readers):
    with pytest.raises(Refused) as refused:
        build(str(tree), str(tmp_path / "x.db"), "x", readers=readers)
    assert refused.value.code == "invalid_request"
    assert not (tmp_path / "x.db").exists()


def test_the_release_comes_from_build_arguments(tree, tmp_path):
    path = tmp_path / "stamped.db"
    report = build(str(tree), str(path), "stamped", versions={
        "application": "10.0.2790.46", "platform": "7.0.8199.32", "release": "10.0.49",
        "evidence": "InstallationRecords on the VHD, read 2026-10-15"})
    assert report["application_version"] == "10.0.2790.46"
    stamp = Index(str(path)).stamp()
    assert stamp["application_version"] == "10.0.2790.46"
    assert stamp["platform_version"] == "7.0.8199.32"
    assert stamp["release_version"] == "10.0.49"
    assert stamp["version_source"] == "build arguments"
    assert Index(str(path)).meta["version_evidence"].startswith("InstallationRecords")


def test_without_arguments_the_stamp_says_unknown(tree, tmp_path):
    path = tmp_path / "plain.db"
    build(str(tree), str(path), "plain")
    stamp = Index(str(path)).stamp()
    assert (stamp["application_version"], stamp["platform_version"], stamp["version_source"]) == (
        "unknown", "unknown", "unknown")
    assert "release_version" not in stamp


def test_an_old_snapshot_names_its_descriptor_versions(tree, tmp_path):
    path = tmp_path / "old.db"
    build(str(tree), str(path), "old")
    with closing(sqlite3.connect(path)) as db:  # what a builder before 0.1.16 wrote
        db.execute("DELETE FROM meta WHERE key IN ('version_source', 'builder_descriptor_versions')")
        db.execute("UPDATE meta SET value = '10.0.2177.31' WHERE key = 'application_version'")
        db.commit()
    index = Index(str(path))
    assert index.stamp()["version_source"].startswith("model descriptors")
    index.db.close()  # this thread's read connection, before the next write
    with closing(sqlite3.connect(path)) as db:  # corrected by hand, as once for a 10.0.49 database
        db.execute("INSERT INTO meta VALUES ('version_evidence', 'installer records')")
        db.commit()
    assert Index(str(path)).stamp()["version_source"].startswith("installation evidence")


@pytest.mark.parametrize("versions", [
    {"application": "10.0.49 beta"}, {"platform": "seven"}, {"release": "10..49"},
    {"evidence": "records"}, {"application": "10.0.1", "evidence": "a\nb"},
    {"application": "10.0.1", "evidence": "x" * 501}, {"build": "1.2"},
])
def test_bad_versions_are_refused_before_anything_is_written(tree, tmp_path, versions):
    with pytest.raises(Refused) as refused:
        build(str(tree), str(tmp_path / "x.db"), "x", versions=versions)
    assert refused.value.code == "invalid_request"
    assert list(tmp_path.iterdir()) == []


def test_the_command_line_passes_readers_and_versions(tree, tmp_path, capsys):
    path = tmp_path / "cli.db"
    server.main(["build", "--source", str(tree), "--db", str(path), "--name", "cli", "--readers", "4",
                 "--application-version", "10.0.2790.46", "--release-version", "10.0.49"])
    stamp = Index(str(path)).stamp()
    assert (stamp["application_version"], stamp["platform_version"], stamp["release_version"]) == (
        "10.0.2790.46", "unknown", "10.0.49")
