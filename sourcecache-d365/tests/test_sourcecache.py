"""boring-sourcecache-d365 against the synthetic corpus, every lookup cross-checked.

The lesson of an earlier prototype: never trust one tool's count without an
independent one. Each search and reference lookup is compared with a naive
scan of the corpus files, and each of its four label findings has its own case.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import corpus
from boring_sourcecache_d365.sourcecache import LABEL_REF, Index, build, classify, parse_labels
from boring_service_kit.outcomes import Failed, Refused


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("corpus")
    base = corpus.write(root)
    index_path = root / "d365-synthetic.db"
    report = build(str(base), str(index_path), "d365-synthetic-10.0.99")
    return Index(str(index_path)), corpus.text_files(base), report


def stored(index_path, query):
    """Rows read straight from the snapshot file, past Index and the build report."""
    with closing(sqlite3.connect(Path(index_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute(query).fetchall()


def naive_files(files, needle):
    return {path for path, text in files.items() if needle.lower() in text.lower()}


def naive_refs(files, ref):
    pattern = re.compile(r"(?<![A-Za-z0-9_@])" + re.escape(ref) + r"(?![A-Za-z0-9_])", re.IGNORECASE)
    found = set()
    for path, text in files.items():
        if "/LabelResources/" in path:
            continue
        for number, line in enumerate(text.split("\n"), start=1):
            if pattern.search(line):
                found.add((path, number))
    return found


# --- building and keys ----------------------------------------------------------


def test_build_reports_counts_and_versions(built):
    index, files, report = built
    assert int(report["files"]) == len(files)  # every text file, binaries and bin/ skipped
    assert report["models"] == 5
    assert {path for (path,) in stored(index.path, "SELECT path FROM files")} == set(files)
    assert stored(index.path, "SELECT COUNT(*) FROM files") == [(len(files),)]
    assert set(stored(index.path, "SELECT package, model FROM models")) == set(corpus.MODELS)
    assert stored(index.path, "SELECT COUNT(*) FROM models") == [(len(corpus.MODELS),)]
    assert index.meta["name"] == "d365-synthetic-10.0.99"
    assert index.meta["application_version"] == "10.0.2177.31"
    assert index.meta["platform_version"] == "7.0.7701.12"


def test_models_carry_layer_and_publisher(built):
    index, _, _ = built
    models = {(m["package"], m["model"]): m for m in index.info()["models"]}
    assert models[("ApplicationSuite", "Foundation")]["layer"] == "sys"
    assert models[("FabrikamWms", "FabrikamWms")]["layer"] == "isv"
    assert models[("ContosoExtensions", "ContosoExtensions")]["layer"] == "cus"
    assert models[("ContosoExtensions", "ContosoExtensions")]["publisher"] == "Contoso"


def test_classify_paths():
    assert classify(("Pkg", "Model", "AxClass", "Foo.xml"))["kind"] == "element"
    label = classify(("Pkg", "Model", "AxLabelFile", "LabelResources", "en-us", "Lbl.en-us.label.txt"))
    assert (label["kind"], label["label_file"], label["locale"]) == ("label", "Lbl", "en-us")
    assert classify(("Pkg", "Descriptor", "Model.xml"))["kind"] == "descriptor"


# --- search ----------------------------------------------------------------------


@pytest.mark.parametrize("needle", ["AllowLocOverridePut", "allowlocoverrideput", "@SYS1", "WHSHelper",
                                    "Location ID is not valid", "putLimitText"])
def test_search_matches_a_naive_scan(built, needle):
    index, files, _ = built
    result = index.search(needle, limit=100)
    assert {r["path"] for r in result["results"]} == naive_files(files, needle)
    assert result["total_files"] == len(naive_files(files, needle))


def test_prefixed_camelcase_is_found(built):
    # The prototype's 44/44 case in miniature: WHSAllowLocOverridePut contains AllowLocOverridePut.
    index, _, _ = built
    names = {r["name"] for r in index.search("AllowLocOverridePut")["results"]}
    assert {"InventLocationValidator", "WHSPutRule", "FabrikamPutHandler"} <= names


def test_search_filters_and_scope(built):
    index, _, _ = built
    standard = {r["model"] for r in index.search("PutLimit", scope="standard")["results"]}
    custom = {r["model"] for r in index.search("WHSHelper", scope="custom")["results"]}
    assert "ContosoExtensions" not in standard and custom == {"ContosoExtensions"}
    classes = index.search("WHSHelper", element_type="AxClass")["results"]
    assert {r["element_type"] for r in classes} == {"AxClass"}


def test_search_lines_and_paging(built):
    index, _, _ = built
    first = index.search("@SYS", limit=2)
    assert len(first["results"]) == 2 and first["truncated"]
    rest = index.search("@SYS", limit=100, offset=2)
    assert first["total_files"] == 2 + len(rest["results"])
    hit = next(r for r in index.search("WHSAllowLocOverridePut")["results"] if r["name"] == "InventLocationValidator")
    assert hit["matching_lines"] == 2 and all("WHSAllowLocOverridePut" in line["text"] for line in hit["lines"])


@pytest.mark.parametrize("query", ["ab", "  x ", "a\nb"])
def test_search_refuses_what_trigrams_cannot_answer(built, query):
    with pytest.raises(Refused):
        built[0].search(query)


def test_quotes_in_queries_are_safe(built):
    assert built[0].search('return "@WHS')["total_files"] == 1


# --- element -------------------------------------------------------------------


def test_element_by_type_and_name(built):
    index, _, _ = built
    table = index.element("AxTable", "whsputrule")
    assert table["model"] == "Foundation" and "<Label>@SYS10</Label>" in table["content"]
    assert "\r" not in table["content"]


def test_element_ambiguous_names_list_candidates(built):
    index, _, _ = built
    result = index.element("AxClass", "WHSHelper")
    assert result["ambiguous"] and {c["model"] for c in result["candidates"]} == {"WarehouseCore",
                                                                                   "ContosoExtensions"}
    chosen = index.element("AxClass", "WHSHelper", model="ContosoExtensions")
    assert "ExtensionOf" in chosen["content"] and chosen["layer"] == "cus"
    with pytest.raises(Refused):
        index.element("AxClass", "DoesNotExist")


# --- labels: the prototype's four findings, by construction --------------------


def test_both_label_syntaxes_are_found(built):
    index, _, _ = built
    legacy = index.labels(label_id="@SYS100")
    assert [(l["text"], l["locale"]) for l in legacy["labels"]] == [("Location ID is not valid", "en-US"),
                                                                    ("Lagerplatz-ID ist ungültig", "de")]
    new_style = index.labels(label_id="WHSLabels:PutLimit")
    assert new_style["ref"] == "@WHSLabels:PutLimit"
    assert new_style["labels"][0]["text"] == "Put limit %1 exceeded"


def test_text_search_covers_all_locale_spellings(built):
    # en-US, en-us and en-Us folders: one locale, matched case-insensitively.
    index, _, _ = built
    refs = {l["ref"] for l in index.labels(text="put", locale="en-US", limit=50)["labels"]}
    assert {"@WHSLabels:PutLimit", "@FabrikamLabels:PutDone"} <= refs  # folders en-us and en-Us
    assert index.labels(text="Debitor", locale="en-US")["total"] == 0  # and de stays out


def test_text_search_finds_text_across_files_and_ranks_exact_first(built):
    index, _, _ = built
    result = index.labels(text="Location ID is not valid")
    assert result["labels"][0]["ref"] == "@SYS100"
    assert {l["ref"] for l in result["labels"]} == {"@SYS100", "@ContosoLabels:PutOverride"}


@pytest.mark.parametrize("ref", ["@SYS1", "@SYS10", "@SYS100", "@SYS1000", "@WHSLabels:PutLimit",
                                 "@WHSLabels:PutLimitExceeded", "@ContosoLabels:PutOverride"])
def test_references_are_exact_and_match_a_naive_scan(built, ref):
    # @SYS1 must not count @SYS10 or @SYS100; PutLimit must not count PutLimitExceeded.
    index, files, _ = built
    result = index.labels(label_id=ref, references=True, limit=500)
    found = {(r["path"], r["line"]) for r in result["references"]}
    assert found == naive_refs(files, ref)
    assert result["references_total"] == len(found)


def test_bom_does_not_corrupt_the_first_label(built):
    # The fourth finding: the first entry of a file with a BOM.
    index, _, _ = built
    assert [l["text"] for l in index.labels(label_id="@SYS1", locale="en-US")["labels"]] == ["Customer"]
    assert index.labels(label_id="WHSLabels:AllowLocOverride")["labels"][0]["text"] == "Allow location override"


def test_descriptions_belong_to_the_label_above():
    labels = list(parse_labels("@SYS1=Customer\n ;Short name.\n@SYS2=Vendor\nX=a=b\n"))
    assert labels == [("@SYS1", "Customer", "Short name."), ("@SYS2", "Vendor", None), ("X", "a=b", None)]


def test_label_ref_pattern_boundaries():
    found = [m.group(0) for m in LABEL_REF.finditer('x("@SYS1 %1"); y="@SYS10"; z=me@SYS1; @Lbl:Key_2.')]
    assert found == ["@SYS1", "@SYS10", "@Lbl:Key_2"]


def test_labels_refusals(built):
    index, _, _ = built
    for kwargs in ({}, {"text": "abc", "label_id": "@SYS1"}, {"text": "abc", "references": True},
                   {"text": "ab"}):
        with pytest.raises(Refused):
            index.labels(**kwargs)


# --- serving: one connection per thread, the stall guard, warm-up ---


def test_label_id_limit_caps_the_labels_and_puts_en_us_first(built):
    index, _, _ = built
    full = index.labels(label_id="@SYS100")
    assert full["labels_total"] == 2 and "labels_truncated" not in full
    one = index.labels(label_id="@SYS100", limit=1)
    assert [l["locale"] for l in one["labels"]] == ["en-US"]
    assert one["labels_total"] == 2 and one["labels_truncated"] is True


def test_each_thread_has_its_own_connection(built):
    import threading
    index, _, _ = built
    seen = {}

    def grab(key):
        seen[key] = index.db
        index.search("Location")

    threads = [threading.Thread(target=grab, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen[0] is not seen[1] and index.db not in (seen[0], seen[1])


def test_a_slow_query_does_not_hold_up_another_thread(built):
    import threading
    import time
    index, _, _ = built
    started = threading.Event()

    def slow():
        conn = index.db
        started.set()
        conn.execute("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3000000) "
                     "SELECT COUNT(*) FROM n").fetchone()

    thread = threading.Thread(target=slow)
    thread.start()
    started.wait()
    t0 = time.monotonic()
    index.search("Location")
    fast = time.monotonic() - t0
    thread.join()
    assert fast < 0.5


def test_the_stall_guard_stops_a_query_and_frees_the_connection(built, monkeypatch):
    index, _, _ = built
    monkeypatch.setattr(index, "stall_seconds", 0.05)
    heavy = ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 100000000) "
             "SELECT COUNT(*) FROM n")
    with pytest.raises(Failed) as stopped:
        index._guarded(lambda: index.db.execute(heavy).fetchone())
    assert stopped.value.code == "timeout" and "stall guard" in stopped.value.message
    assert index.search("Location")["total_files"] > 0  # the connection works again


def test_the_stall_guard_names_the_pond_when_the_pond_set_it(built, monkeypatch):
    index, _, _ = built
    heavy = ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 100000000) "
             "SELECT COUNT(*) FROM n")
    monkeypatch.setattr(index, "stall_seconds", 0.05)
    with pytest.raises(Failed) as own:
        index._guarded(lambda: index.db.execute(heavy).fetchone())
    assert "--stall-seconds" in own.value.message  # loopback: the operator's own setting
    monkeypatch.setattr(index, "pond_bound", True)
    with pytest.raises(Failed) as pond:
        index._guarded(lambda: index.db.execute(heavy).fetchone())
    assert "the Pond waits no longer" in pond.value.message and "--stall-seconds" not in pond.value.message


def test_no_stall_guard_when_off(built, monkeypatch):
    index, _, _ = built
    monkeypatch.setattr(index, "stall_seconds", 0)
    assert index._guarded(lambda: index.db.execute(
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 300000) "
        "SELECT COUNT(*) FROM n").fetchone()[0]) == 300000


def test_warm_up_reads_the_whole_file(built):
    index, _, _ = built
    assert index.warm["state"] in ("cold", "warm")
    index.warm_up()
    assert index.warm["state"] == "warm" and index.warm["mb"] >= 0
    index.start_warm_up().join()
    assert index.warm["state"] == "warm"



# --- --locales: only the label languages someone reads --------------------------


@pytest.fixture(scope="module")
def built_en(tmp_path_factory):
    root = tmp_path_factory.mktemp("corpus-en")
    base = corpus.write(root)
    index_path = root / "d365-synthetic-en.db"
    report = build(str(base), str(index_path), "d365-synthetic-en", locales=["EN-us"])
    return Index(str(index_path)), report


def test_locales_leave_other_label_files_out_entirely(built, built_en):
    full, _, full_report = built
    english, report = built_en
    assert report["locales"] == "en-us" and full_report["locales"] == "all"
    assert int(report["skipped_other_locales"]) > 0 and full_report["skipped_other_locales"] == 0
    assert int(report["files"]) == int(full_report["files"]) - int(report["skipped_other_locales"])
    _, files, _ = built
    other_locales = {path for path in files if "/LabelResources/" in path
                     and path.split("/LabelResources/")[1].split("/")[0].lower() != "en-us"}
    assert other_locales  # the corpus has German label files to leave out
    assert {path for (path,) in stored(english.path, "SELECT path FROM files")} == set(files) - other_locales
    assert int(report["skipped_other_locales"]) == len(other_locales)
    assert {l["locale"].lower() for l in english.labels(label_id="@SYS100")["labels"]} == {"en-us"}
    assert english.search("Lagerplatz")["total_files"] == 0  # the German text is not searchable either
    assert full.search("Lagerplatz")["total_files"] > 0


def test_locales_keep_every_reference(built, built_en):
    full, _, _ = built
    english, _ = built_en
    for ref in ("@SYS1", "@SYS10", "@SYS100", "@WHSLabels:AllowLocOverride"):
        assert (english.labels(label_id=ref, references=True, limit=500)["references_total"]
                == full.labels(label_id=ref, references=True, limit=500)["references_total"])


def test_a_locale_outside_the_index_is_refused(built_en):
    english, _ = built_en
    with pytest.raises(Refused) as refused:
        english.labels(text="Lagerplatz", locale="de")
    assert refused.value.code == "not_found" and refused.value.details["locales"] == "en-us"
    assert english.labels(text="Location", locale="en-US")["total"] > 0


def test_build_creates_its_folder_and_refuses_clearly(tmp_path):
    """--db on a missing folder: the folder is made; an impossible one is a clear refusal, not a traceback."""
    source = tmp_path / "PackagesLocalDirectory"
    (source / "Pkg" / "Pkg" / "AxClass").mkdir(parents=True)
    (source / "Pkg" / "Pkg" / "AxClass" / "C.xml").write_text("<AxClass><Name>C</Name></AxClass>")
    report = build(str(source), str(tmp_path / "new" / "deeper" / "x.db"), "t")
    assert int(report["files"]) == 1 and (tmp_path / "new" / "deeper" / "x.db").exists()
    (tmp_path / "afile").write_text("not a folder")
    with pytest.raises(Refused) as refused:
        build(str(source), str(tmp_path / "afile" / "x.db"), "t")
    assert refused.value.code == "forbidden" and "--db" in refused.value.message


def test_descriptor_case_publisher_and_unknown_on_existing_snapshot(tmp_path):
    import sqlite3
    root = corpus.write(tmp_path)
    path = tmp_path / 'classification.db'
    build(str(root), str(path), 'classification')
    with sqlite3.connect(path) as db:
        db.execute("UPDATE models SET package=lower(package), model=lower(model), publisher='Microsoft' WHERE publisher='Microsoft Corporation'")
        db.execute("UPDATE models SET publisher=NULL WHERE publisher='Fabrikam ISV'")
        # A file that lacks a descriptor must remain in all, never become custom.
        db.execute("UPDATE files SET model='MissingDescriptor' WHERE name='InventLocationValidator'")
    before = path.read_bytes()
    index = Index(str(path))
    info = index.info()
    assert any(m['files'] > 0 and m['scope'] == 'standard' for m in info['models'])
    assert all(r['scope'] == 'standard' for r in index.search('PutLimit', scope='standard')['results'])
    all_hits = index.search('AllowLocOverridePut', limit=100)['results']
    unknown = [r for r in all_hits if r['name'] == 'InventLocationValidator']
    assert unknown and unknown[0]['scope'] == 'unknown' and unknown[0]['layer'] is None
    assert next(r for r in all_hits if r['name']=='FabrikamPutHandler')['scope']=='unknown'
    assert all(r['scope']=='custom' for r in index.search('WHSHelper',scope='custom')['results'])
    assert not any(r['name'] == 'InventLocationValidator' for r in index.search('AllowLocOverridePut', scope='custom')['results'])
    element = index.element('AxTable', 'WHSPutRule')
    assert element['scope'] == 'standard' and element['layer'] == 'sys'
    assert path.read_bytes() == before


def test_the_index_refuses_writes_and_leaves_the_file_as_it_was(tmp_path):
    root = corpus.write(tmp_path)
    path = tmp_path / "readonly.db"
    build(str(root), str(path), "readonly")
    before = path.read_bytes()
    index = Index(str(path))
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        index.db.execute("INSERT INTO meta VALUES ('written', 'by a reader')")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        index.db.execute("DELETE FROM files")
    index.db.close()
    assert path.read_bytes() == before
    assert stored(path, "SELECT COUNT(*) FROM meta WHERE key = 'written'") == [(0,)]


def test_case_colliding_descriptors_are_refused(tmp_path):
    import sqlite3
    root = corpus.write(tmp_path)
    path = tmp_path / 'collision.db'
    build(str(root), str(path), 'collision')
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO models SELECT lower(package), lower(model), layer, layer_name, publisher, version, refs FROM models LIMIT 1")
    with pytest.raises(Refused, match='ambiguous'):
        Index(str(path))
