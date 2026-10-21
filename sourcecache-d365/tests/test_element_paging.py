"""Character bounds must not advance past unseen indexed source lines."""
from boring_sourcecache_d365 import sourcecache


def index_for(tmp_path, text):
    source = tmp_path / "source"
    path = source / "Pkg/Model/AxClass/Example.xml"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    database = tmp_path / "index.db"
    sourcecache.build(str(source), str(database), "paging")
    return sourcecache.Index(str(database))


def test_character_budget_preserves_every_complete_line(tmp_path, monkeypatch):
    text = "one\ntwo\nthree\nfour\n"
    index = index_for(tmp_path, text)
    monkeypatch.setattr(sourcecache, "ELEMENT_MAX_CHARS", 8)
    first = index.element("AxClass", "Example", line_count=100)
    assert first["content"] == "one\ntwo\n"
    assert first["next_line"] == 3
    pieces = [first["content"]]
    cursor = first["next_line"]
    while cursor is not None:
        page = index.element("AxClass", "Example", start_line=cursor, line_count=100)
        pieces.append(page["content"])
        assert not page["line_cut"]
        cursor = page["next_line"]
    assert "".join(pieces) == text


def test_single_oversized_line_is_explicit_and_next_line_remains_available(tmp_path, monkeypatch):
    index = index_for(tmp_path, "abcdefghijkl\nnext\n")
    monkeypatch.setattr(sourcecache, "ELEMENT_MAX_CHARS", 8)
    page = index.element("AxClass", "Example")
    assert page["content"] == "abcdefgh"
    assert page["line_cut"] is True
    assert page["returned_lines"] == 1
    assert page["next_line"] == 2
    following = index.element("AxClass", "Example", start_line=2)
    assert following["content"] == "next\n"
    assert following["next_line"] is None
