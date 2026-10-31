from boring_canvas.storage import CanvasStore


def test_reserved_names_survive_restart(tmp_path):
    store = CanvasStore(tmp_path)
    for name in ["con", "aux", "nul", "com1", "canvas"]:
        canvas_id, _ = store.create_named("pond", name)
        assert store.find_named("pond", name).canvas_id == canvas_id
    restarted = CanvasStore(tmp_path)
    assert {row["name"] for row in restarted.list_named("pond")} == {"con", "aux", "nul", "com1", "canvas"}
    assert restarted.find_named("pond", "canvas").canvas_id
    store.close()
    restarted.close()
