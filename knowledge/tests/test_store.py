"""The notebook's store: versions, authors, search, roles."""

from __future__ import annotations

import pytest

from boring_knowledge.store import Store
from boring_service_kit.outcomes import Refused


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "notebook.db"))


def add_pip_entry(store, author="pond"):
    return store.add(author, "pip drops win32-only dependencies when resolving for Windows on Linux",
                     "Resolve with `uv --python-platform x86_64-pc-windows-msvc` instead; pip evaluates "
                     "`sys_platform == \"win32\"` markers against the build host.",
                     tags=["Python", "packaging", "windows"], context="pip 24, uv 0.12",
                     evidence="a Windows build run", by="Crocodile (Opus 5.5)")


def test_add_records_author_and_first_version(store):
    entry = add_pip_entry(store)
    assert entry["version"] == 1 and entry["author"] == "pond" and entry["created_by"] == "pond"
    assert entry["tags"] == ["python", "packaging", "windows"]  # normalised to lowercase
    assert entry["confidence"] == "observed" and entry["by"] == "Crocodile (Opus 5.5)"


def test_revisions_append_and_keep_every_author(store):
    entry = add_pip_entry(store)
    revised = store.revise("garden", entry["id"], 1, "pip 25 still behaves this way", context="pip 24-25, uv 0.12")
    assert revised["version"] == 2 and revised["author"] == "garden" and revised["created_by"] == "pond"
    assert revised["title"] == entry["title"]  # unchanged fields carry over
    history = store.get(entry["id"], history=True)["history"]
    assert [(h["version"], h["author"], h["reason"]) for h in history] == [
        (2, "garden", "pip 25 still behaves this way"), (1, "pond", None)]


def test_stale_revision_is_refused(store):
    entry = add_pip_entry(store)
    store.revise("garden", entry["id"], 1, "first", body="New body.")
    with pytest.raises(Refused) as info:
        store.revise("orchard", entry["id"], 1, "second, from an old read", body="Other body.")
    assert info.value.code == "stale" and info.value.details["current_version"] == 2
    current = store.get(entry["id"], history=True)
    assert current["version"] == 2 and len(current["history"]) == 2
    assert current["author"] == "garden" and current["body"] == "New body."


def test_retire_is_a_version_and_hides_from_search(store):
    entry = add_pip_entry(store)
    retired = store.retire("meadow", entry["id"], 1, "fixed upstream in pip 26")
    assert retired["retired"] and retired["version"] == 2 and retired["reason"] == "fixed upstream in pip 26"
    assert store.search("pip drops")["total"] == 0
    assert store.search("pip drops", include_retired=True)["total"] == 1
    with pytest.raises(Refused):
        store.revise("pond", entry["id"], 2, "revive", body="x")
    current = store.get(entry["id"], history=True)
    assert current["version"] == 2 and len(current["history"]) == 2 and current["retired"]
    assert current["body"] == entry["body"]
    store.db.close()
    reopened = Store(store.path)
    assert reopened.get(entry["id"])["retired"] and reopened.get(entry["id"])["version"] == 2
    assert reopened.search("pip drops")["total"] == 0
    assert reopened.search("pip drops", include_retired=True)["total"] == 1


def test_search_by_text_tag_context_author(store):
    add_pip_entry(store)
    store.add("garden", "Windows install via QEMU: watch disk growth, not the screen",
              "The answer file shuts Windows down at the end; QEMU exiting means done.",
              tags=["qemu", "windows"], context="Windows Server 2022")
    assert store.search("QEMU")["total"] == 1
    assert store.search("x86_64-pc-windows")["entries"][0]["snippet"].startswith("Resolve with")
    assert store.search(tag="windows")["total"] == 2
    assert store.search(tag="WINDOWS", author="garden")["total"] == 1
    assert store.search(context="Server 2022")["total"] == 1
    assert store.search("winDOWS")["total"] == 2  # case-insensitive
    with pytest.raises(Refused):
        store.search("ab")


def test_author_filter_matches_the_author_a_result_shows(store):
    entry = add_pip_entry(store, author="pond")
    store.revise("garden", entry["id"], 1, "clearer", body="Use uv's --python-platform instead.")
    found = store.search(author="garden")
    assert found["total"] == 1
    assert found["entries"][0]["author"] == "garden" and found["entries"][0]["created_by"] == "pond"
    assert store.search(author="pond")["total"] == 0


def test_search_survives_quotes_and_pages(store):
    for n in range(5):
        store.add("pond", f'Entry "{n}" about jobs', f"Body {n} mentions job_wait.", tags=["jobs"])
    first = store.search('"1" about', limit=10)
    assert first["total"] == 1
    page = store.search(tag="jobs", limit=2, offset=0)
    assert len(page["entries"]) == 2 and page["truncated"] and page["total"] == 5


def test_revised_text_is_what_search_finds(store):
    entry = add_pip_entry(store)
    store.revise("pond", entry["id"], 1, "clearer", body="Use uv for Windows targets.")
    assert store.search("build host")["total"] == 0
    assert store.search("uv for Windows")["total"] == 1


@pytest.mark.parametrize("kwargs", [
    {"title": "ab", "body": "x"},
    {"title": "valid title", "body": ""},
    {"title": "valid title", "body": "x" * 8001},
    {"title": "valid title", "body": "x", "tags": ["has space"]},
    {"title": "valid title", "body": "x", "tags": [f"t{n}" for n in range(11)]},
    {"title": "valid title", "body": "x", "confidence": "certain"},
])
def test_invalid_entries_are_refused(store, kwargs):
    with pytest.raises(Refused) as info:
        store.add("pond", **kwargs)
    assert info.value.code == "invalid_request"


def test_role_is_fixed_at_creation(tmp_path):
    path = str(tmp_path / "premise.db")
    Store(path, role="premise")
    with pytest.raises(Refused):
        Store(path, role="global")  # a premise notebook never turns global by a flag
    assert Store(path, role="premise").role == "premise"


def test_revision_needs_a_reason_and_a_change(store):
    entry = add_pip_entry(store)
    with pytest.raises(Refused):
        store.revise("pond", entry["id"], 1, "", body="x")
    with pytest.raises(Refused):
        store.revise("pond", entry["id"], 1, "no change")
    with pytest.raises(Refused) as info:
        store.revise("pond", 999, 1, "missing", body="x")
    assert info.value.code == "not_found"
    current = store.get(entry["id"], history=True)
    assert current["version"] == 1 and len(current["history"]) == 1 and current["body"] == entry["body"]
