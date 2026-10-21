import pytest
from boring_sourcecache_d365.sourcecache import build, Index
from boring_service_kit.outcomes import Refused

def tree(tmp_path):
    source = tmp_path / "source"
    folder = source / "Pkg/Pkg/AxClass"
    folder.mkdir(parents=True)
    (folder / "Class.xml").write_text("<AxClass><Name>Class</Name></AxClass>")
    return source

def test_snapshot_path_and_no_replacement(tmp_path):
    source = tree(tmp_path)
    target = tmp_path / "unicode space ä #.db"
    build(str(source), str(target), "snapshot")
    opened = Index(str(target))
    before = target.read_bytes()
    assert opened.stamp()["snapshot_id"]
    with pytest.raises(Refused, match="already exists"):
        build(str(source), str(target), "replacement")
    assert target.read_bytes() == before
    assert Index(str(target)).stamp() == opened.stamp()

def test_symlinked_files_are_not_followed_out_of_the_tree(tmp_path):
    source = tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Secret.xml").write_text("<AxClass><Name>OutsideOnlyMarker</Name></AxClass>")
    (source / "Pkg/Pkg/AxClass/Linked.xml").symlink_to(outside / "Secret.xml")
    (source / "Pkg/Pkg/AxTable").symlink_to(outside, target_is_directory=True)
    target = tmp_path / "linked.db"
    report = build(str(source), str(target), "linked")
    assert int(report["files"]) == 1
    index = Index(str(target))
    assert index.search("OutsideOnlyMarker")["results"] == []
    assert index.search("Class")["results"]

def test_interruption_leaves_no_published_or_staged_index(tmp_path, monkeypatch):
    source = tree(tmp_path)
    target = tmp_path / "snapshot.db"
    import boring_sourcecache_d365.sourcecache as module
    def interrupted(*args):
        raise RuntimeError("interrupted")
    monkeypatch.setattr(module, "_walk", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        build(str(source), str(target), "snapshot")
    assert not target.exists()
    assert not list(tmp_path.glob(".sourcecache-*"))
