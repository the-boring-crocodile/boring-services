"""Unit tests for CanvasStore. Pure, no network, no server, no browser."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from boring_canvas import storage
from boring_canvas.storage import (
    PUSHED_SCENE_ELEMENT_ID,
    CanvasNotFound, CanvasStore, CorruptCanvas, InvalidElements, InvalidSVG, InvalidToken,
    NameIndexBroken, NameTaken, RateLimited, TooManyCanvases,
)

from canvas_fixtures import legacy_ownerless_canvas, new_canvas


class CanvasStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanvasStore(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_returns_distinct_id_and_token(self) -> None:
        id_a, token_a = new_canvas(self.store)
        id_b, token_b = new_canvas(self.store)
        self.assertNotEqual(id_a, id_b)
        self.assertNotEqual(token_a, token_b)

    def test_verify_token_accepts_correct_token(self) -> None:
        canvas_id, token = new_canvas(self.store)
        self.assertTrue(self.store.verify_token(canvas_id, token))

    def test_verify_token_rejects_wrong_token(self) -> None:
        canvas_id, _token = new_canvas(self.store)
        self.assertFalse(self.store.verify_token(canvas_id, "not-the-token"))

    def test_read_unknown_canvas_raises(self) -> None:
        with self.assertRaises(CanvasNotFound):
            self.store.read("0" * 32)

    def test_write_scene_with_wrong_token_raises_and_does_not_change_state(self) -> None:
        canvas_id, _token = new_canvas(self.store)
        with self.assertRaises(InvalidToken):
            self.store.write_scene(canvas_id, "wrong-token", "<svg>evil</svg>")
        self.assertIsNone(self.store.read(canvas_id).scene_svg)

    def test_write_scene_with_correct_token_persists(self) -> None:
        canvas_id, token = new_canvas(self.store)
        self.store.write_scene(canvas_id, token, "<svg>rose</svg>")
        self.assertEqual(self.store.read(canvas_id).scene_svg, "<svg>rose</svg>")

    def test_canvas_id_is_used_verbatim_as_filename_stem(self) -> None:
        canvas_id, _token = new_canvas(self.store)
        self.assertTrue((self.store.state_dir / f"{canvas_id}.json").exists())

    def test_path_traversal_like_id_is_rejected(self) -> None:
        with self.assertRaises(CanvasNotFound):
            self.store.read("../../etc/passwd")

    def test_write_scene_strips_script_element(self) -> None:
        canvas_id, token = new_canvas(self.store)
        pushed = '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><circle r="1"/></svg>'
        self.store.write_scene(canvas_id, token, pushed)
        stored = self.store.read(canvas_id).scene_svg
        self.assertNotIn("script", stored)
        self.assertNotIn("alert", stored)
        self.assertIn("circle", stored)

    def test_write_scene_strips_event_handler_attribute(self) -> None:
        canvas_id, token = new_canvas(self.store)
        pushed = '<svg xmlns="http://www.w3.org/2000/svg"><rect onclick="alert(1)" width="1" height="1"/></svg>'
        self.store.write_scene(canvas_id, token, pushed)
        stored = self.store.read(canvas_id).scene_svg
        self.assertNotIn("onclick", stored)
        self.assertNotIn("alert", stored)

    def test_write_scene_strips_javascript_uri(self) -> None:
        canvas_id, token = new_canvas(self.store)
        pushed = ('<svg xmlns="http://www.w3.org/2000/svg">'
                  '<a href="javascript:alert(1)"><rect width="1" height="1"/></a></svg>')
        self.store.write_scene(canvas_id, token, pushed)
        stored = self.store.read(canvas_id).scene_svg
        self.assertNotIn("javascript", stored)
        self.assertNotIn("alert", stored)

    def test_write_scene_strips_foreignobject_iframe(self) -> None:
        # A real bypass: an iframe inside foreignObject has no <script>
        # tag and no on*/javascript: attribute for the other checks.
        canvas_id, token = new_canvas(self.store)
        pushed = ('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                  '<foreignObject width="200" height="100">'
                  '<iframe xmlns="http://www.w3.org/1999/xhtml" src="https://evil.example/phish"/>'
                  '</foreignObject><circle r="1"/></svg>')
        self.store.write_scene(canvas_id, token, pushed)
        stored = self.store.read(canvas_id).scene_svg
        self.assertNotIn("iframe", stored)
        self.assertNotIn("evil.example", stored)
        self.assertNotIn("foreignObject", stored)
        self.assertIn("circle", stored)

    def _stored(self, pushed: str) -> str:
        canvas_id, token = new_canvas(self.store)
        self.store.write_scene(canvas_id, token, pushed)
        return self.store.read(canvas_id).scene_svg

    def test_write_scene_strips_obfuscated_script_uris(self) -> None:
        for value in (" java&#09;script:alert(1)", "JaVa&#x0A;ScRiPt:alert(1)",
                      "&#x7f;javascript:alert(1)", "java\tscript:alert(1)",
                      "vbscript:msgbox(1)", "data:text/html,<b>x</b>",
                      " DATA:text/html;base64,PGI+"):
            with self.subTest(value=value):
                stored = self._stored(
                    '<svg xmlns="http://www.w3.org/2000/svg" '
                    'xmlns:xlink="http://www.w3.org/1999/xlink">'
                    f'<a href="{value.replace("<", "&lt;")}" xlink:href="{value.replace("<", "&lt;")}">'
                    '<rect width="1" height="1"/></a></svg>')
                self.assertNotIn("href", stored)
                self.assertIn("rect", stored)

    def test_write_scene_keeps_embedded_images_and_links(self) -> None:
        png = "data:image/png;base64,iVBORw0KGgo="
        stored = self._stored(
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
            f'<image href="{png}" width="1" height="1"/>'
            f'<image xlink:href="{png}" width="1" height="1"/>'
            '<a href="https://example.org/"><rect width="1" height="1"/></a></svg>')
        self.assertEqual(stored.count(png), 2)
        self.assertIn('href="https://example.org/"', stored)

    def test_write_scene_strips_animation_elements(self) -> None:
        stored = self._stored(
            '<svg xmlns="http://www.w3.org/2000/svg"><a><rect width="1" height="1">'
            '<animate attributeName="x" to="5"/></rect>'
            '<set attributeName="href" to="javascript:alert(1)"/>'
            '<animateTransform attributeName="transform" type="rotate" to="90"/>'
            '<animateMotion path="M0 0 L5 5"/></a></svg>')
        for tag in ("<set", "animate", "animateTransform", "animateMotion", "javascript"):
            self.assertNotIn(tag, stored)
        self.assertIn("rect", stored)

    def test_write_scene_use_may_only_reference_this_document(self) -> None:
        stored = self._stored(
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
            '<defs><circle id="dot" r="1"/></defs>'
            '<use href="#dot"/><use xlink:href="#dot"/>'
            '<use href="https://evil.example/x.svg#a"/>'
            '<use xlink:href="other.svg#a"/></svg>')
        self.assertNotIn("evil.example", stored)
        self.assertNotIn("other.svg", stored)
        self.assertEqual(stored.count('href="#dot"'), 2)

    def test_repo_svgs_sanitize_without_change(self) -> None:
        # The hand-authored SVGs pushed in real use lose nothing.
        import xml.etree.ElementTree as ET
        repo = Path(__file__).resolve().parent.parent
        for name in ("rose.svg", "ausmalbilder.svg"):
            with self.subTest(svg=name):
                raw = (repo / name).read_text(encoding="utf-8")
                self.assertEqual(self._stored(raw),
                                 ET.tostring(ET.fromstring(raw), encoding="unicode"))

    def test_write_scene_leaves_clean_svg_intact(self) -> None:
        canvas_id, token = new_canvas(self.store)
        pushed = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><circle cx="5" cy="5" r="4" fill="red"/></svg>'
        self.store.write_scene(canvas_id, token, pushed)
        stored = self.store.read(canvas_id).scene_svg
        self.assertIn("circle", stored)
        self.assertIn("fill", stored)

    def test_write_scene_rejects_malformed_xml(self) -> None:
        canvas_id, token = new_canvas(self.store)
        with self.assertRaises(InvalidSVG):
            self.store.write_scene(canvas_id, token, "<svg><unclosed></svg>")
        # rejected push must not have overwritten the (empty) stored state
        self.assertIsNone(self.store.read(canvas_id).scene_svg)

    def test_scene_version_changes_after_write_and_is_stable_before(self) -> None:
        canvas_id, token = new_canvas(self.store)
        v_empty_1 = self.store.scene_version(canvas_id)
        v_empty_2 = self.store.scene_version(canvas_id)
        self.assertEqual(v_empty_1, v_empty_2)  # stable with no writes between
        self.store.write_scene(canvas_id, token, '<svg xmlns="http://www.w3.org/2000/svg"/>')
        v_after = self.store.scene_version(canvas_id)
        self.assertNotEqual(v_empty_1, v_after)

    def test_write_elements_persists_and_round_trips(self) -> None:
        canvas_id, token = new_canvas(self.store)
        payload = '[{"type": "rectangle", "id": "a1", "x": 10, "y": 20}]'
        self.store.write_elements(canvas_id, token, payload)
        self.assertEqual(self.store.read(canvas_id).elements_json, payload)

    def test_write_elements_rejects_invalid_json(self) -> None:
        canvas_id, token = new_canvas(self.store)
        with self.assertRaises(InvalidElements):
            self.store.write_elements(canvas_id, token, "not json at all {")
        self.assertIsNone(self.store.read(canvas_id).elements_json)

    def test_write_elements_with_wrong_token_raises(self) -> None:
        canvas_id, _token = new_canvas(self.store)
        with self.assertRaises(InvalidToken):
            self.store.write_elements(canvas_id, "wrong-token", "[]")

    def test_write_scene_preserves_existing_elements(self) -> None:
        canvas_id, token = new_canvas(self.store)
        self.store.write_elements(canvas_id, token, '[{"id": "a1"}]')
        self.store.write_scene(canvas_id, token, '<svg xmlns="http://www.w3.org/2000/svg"/>')
        self.assertEqual(self.store.read(canvas_id).elements_json, '[{"id": "a1"}]')

    def test_write_elements_preserves_existing_scene_svg(self) -> None:
        canvas_id, token = new_canvas(self.store)
        self.store.write_scene(canvas_id, token, '<svg xmlns="http://www.w3.org/2000/svg"/>')
        self.store.write_elements(canvas_id, token, '[{"id": "a1"}]')
        self.assertIsNotNone(self.store.read(canvas_id).scene_svg)

    def test_write_elements_does_not_change_scene_version(self) -> None:
        canvas_id, token = new_canvas(self.store)
        v_before = self.store.scene_version(canvas_id)
        self.store.write_elements(canvas_id, token, '[{"id": "a1"}]')
        v_after = self.store.scene_version(canvas_id)
        self.assertEqual(v_before, v_after)  # browser saving its own drawing must not self-trigger a reload

    def test_write_elements_records_edit_for_new_versioned_element(self) -> None:
        canvas_id, token = new_canvas(self.store)
        self.store.write_elements(canvas_id, token, '[{"id": "a1", "type": "ellipse", "version": 1, "updated": 123}]')
        edits = self.store.get_edits(canvas_id)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["element_id"], "a1")
        self.assertEqual(edits[0]["version"], 1)
        self.assertEqual(edits[0]["client_updated_ms"], 123)

    def test_write_elements_does_not_duplicate_edit_for_unchanged_version(self) -> None:
        canvas_id, token = new_canvas(self.store)
        payload = '[{"id": "a1", "type": "ellipse", "version": 1, "updated": 123}]'
        self.store.write_elements(canvas_id, token, payload)
        self.store.write_elements(canvas_id, token, payload)  # same version, saved again (e.g. re-render)
        self.assertEqual(len(self.store.get_edits(canvas_id)), 1)

    def test_write_elements_records_second_edit_when_version_increases(self) -> None:
        canvas_id, token = new_canvas(self.store)
        self.store.write_elements(canvas_id, token, '[{"id": "a1", "type": "ellipse", "version": 1, "updated": 100}]')
        self.store.write_elements(canvas_id, token, '[{"id": "a1", "type": "ellipse", "version": 2, "updated": 200}]')
        edits = self.store.get_edits(canvas_id)
        self.assertEqual(len(edits), 2)
        self.assertEqual([e["version"] for e in edits], [1, 2])

    def test_write_elements_skips_edit_log_for_element_missing_version(self) -> None:
        canvas_id, token = new_canvas(self.store)
        # No crash, and the write itself still succeeds -- opaque storage
        # doesn't require every element to look like a real Excalidraw one.
        self.store.write_elements(canvas_id, token, '[{"id": "a1"}]')
        self.assertEqual(self.store.get_edits(canvas_id), [])
        self.assertIsNotNone(self.store.read(canvas_id).elements_json)

    def test_page_loads_do_not_log_the_pushed_drawing_as_an_edit(self) -> None:
        canvas_id, token = new_canvas(self.store)
        def pushed(version, x=10, deleted=False):
            return json.dumps([{"id": PUSHED_SCENE_ELEMENT_ID, "type": "image", "version": version,
                                "x": x, "y": 20, "width": 300, "height": 200, "angle": 0,
                                "isDeleted": deleted}])
        self.store.write_elements(canvas_id, token, pushed(1))
        self.store.write_elements(canvas_id, token, pushed(4))  # a reload: fresh version
        self.store.write_elements(canvas_id, token, pushed(2))  # another reload
        edits = self.store.get_edits(canvas_id)
        self.assertEqual(len(edits), 1)
        self.assertTrue(edits[0]["pushed_scene"])
        self.store.write_elements(canvas_id, token, pushed(5, x=99))  # the person moved it
        self.store.write_elements(canvas_id, token, pushed(6, x=99, deleted=True))
        self.assertEqual([e["is_deleted"] for e in self.store.get_edits(canvas_id)], [False, False, True])

    def test_template_uses_the_pushed_element_id(self) -> None:
        template = (Path(storage.__file__).parent / "templates" / "canvas.html").read_text()
        self.assertIn(f'const PUSHED_IMAGE_ID = "{PUSHED_SCENE_ELEMENT_ID}";', template)

    def test_get_edits_for_unknown_canvas_is_empty_not_an_error(self) -> None:
        self.assertEqual(self.store.get_edits("0" * 32), [])

    def test_list_canvases_empty_store(self) -> None:
        self.assertEqual(self.store.list_canvases(), [])

    def test_list_canvases_reports_content_flags_without_tokens(self) -> None:
        empty_id, _ = new_canvas(self.store)
        scene_id, scene_token = new_canvas(self.store)
        self.store.write_scene(scene_id, scene_token, '<svg xmlns="http://www.w3.org/2000/svg"/>')
        listing = {c["canvas_id"]: c for c in self.store.list_canvases()}
        self.assertEqual(set(listing), {empty_id, scene_id})
        self.assertFalse(listing[empty_id]["has_scene"])
        self.assertTrue(listing[scene_id]["has_scene"])
        self.assertFalse(listing[scene_id]["has_elements"])
        self.assertNotIn("token_hash", listing[scene_id])
        self.assertNotIn("token", listing[scene_id])

    def test_list_canvases_skips_foreign_file_name(self) -> None:
        canvas_id, _ = new_canvas(self.store)
        (self.store.state_dir / "not-a-canvas-id.json").write_text("not json")
        listing = self.store.list_canvases()
        self.assertEqual([c["canvas_id"] for c in listing], [canvas_id])

    def _corrupt(self, content: str) -> str:
        canvas_id = "c" * 32
        (self.store.state_dir / f"{canvas_id}.json").write_text(content)
        return canvas_id

    def test_corrupt_canvas_is_listed_with_its_problem_and_logged_once(self) -> None:
        good_id, _ = new_canvas(self.store)
        bad_id = self._corrupt('{"token_hash": "x", "elem')
        with self.assertLogs("boring_canvas.storage", "WARNING") as logs:
            listing = {c["canvas_id"]: c for c in self.store.list_canvases()}
            self.store.list_canvases()
            self.store.sweep_expired(max_age_s=0)
        self.assertEqual(len(logs.records), 1)
        self.assertIn(bad_id, logs.output[0])
        self.assertIn("not valid JSON", listing[bad_id]["problem"])
        self.assertNotIn("problem", listing.get(good_id, {}))

    def test_corrupt_canvas_is_reported_in_limits_status(self) -> None:
        bad_id = self._corrupt("[]")
        with self.assertLogs("boring_canvas.storage", "WARNING"):
            status = self.store.limits_status()
        self.assertEqual(status["corrupt_canvases"],
                         {bad_id: "not a canvas record (no token_hash)"})
        self.assertEqual(status["current_canvas_count"], 1)

    def test_sweep_never_deletes_a_corrupt_canvas(self) -> None:
        old_id, _ = new_canvas(self.store)
        self._backdate(old_id, age_s=200 * 86400)
        bad_id = self._corrupt("not json at all")
        with self.assertLogs("boring_canvas.storage", "WARNING"):
            deleted = self.store.sweep_expired(max_age_s=90 * 86400)
        self.assertEqual(deleted, [old_id])  # the sweep still did its job
        self.assertEqual((self.store.state_dir / f"{bad_id}.json").read_text(), "not json at all")

    def test_read_of_corrupt_canvas_is_a_failure_not_not_found(self) -> None:
        bad_id = self._corrupt("not json at all")
        with self.assertRaises(CorruptCanvas):
            self.store.read(bad_id)

    def test_corrupt_canvas_disappears_from_report_once_deleted(self) -> None:
        bad_id = self._corrupt("not json at all")
        with self.assertLogs("boring_canvas.storage", "WARNING"):
            self.store.limits_status()
        self.store.delete(bad_id)
        self.assertEqual(self.store.limits_status()["corrupt_canvases"], {})

    def test_delete_removes_canvas(self) -> None:
        canvas_id, _ = new_canvas(self.store)
        self.store.delete(canvas_id)
        with self.assertRaises(CanvasNotFound):
            self.store.read(canvas_id)

    def test_delete_unknown_canvas_raises(self) -> None:
        with self.assertRaises(CanvasNotFound):
            self.store.delete("0" * 32)

    def test_delete_clears_in_memory_edit_log(self) -> None:
        canvas_id, token = new_canvas(self.store)
        self.store.write_elements(canvas_id, token, '[{"id": "a1", "type": "ellipse", "version": 1, "updated": 1}]')
        self.store.delete(canvas_id)
        self.assertEqual(self.store.get_edits(canvas_id), [])

    def test_rotate_token_with_correct_token_issues_new_one(self) -> None:
        canvas_id, old_token = new_canvas(self.store)
        new_token = self.store.rotate_token(canvas_id, old_token)
        self.assertNotEqual(new_token, old_token)
        self.assertTrue(self.store.verify_token(canvas_id, new_token))

    def test_rotate_token_invalidates_old_token(self) -> None:
        canvas_id, old_token = new_canvas(self.store)
        self.store.rotate_token(canvas_id, old_token)
        self.assertFalse(self.store.verify_token(canvas_id, old_token))

    def test_rotate_token_with_wrong_token_raises_and_does_not_rotate(self) -> None:
        canvas_id, old_token = new_canvas(self.store)
        with self.assertRaises(InvalidToken):
            self.store.rotate_token(canvas_id, "wrong-token")
        self.assertTrue(self.store.verify_token(canvas_id, old_token))  # unchanged

    def test_rotate_token_unknown_canvas_raises(self) -> None:
        with self.assertRaises(CanvasNotFound):
            self.store.rotate_token("0" * 32, "whatever")

    def test_rotate_token_preserves_scene_and_elements(self) -> None:
        canvas_id, token = new_canvas(self.store)
        self.store.write_scene(canvas_id, token, '<svg xmlns="http://www.w3.org/2000/svg"/>')
        self.store.write_elements(canvas_id, token, '[{"id": "a1"}]')
        new_token = self.store.rotate_token(canvas_id, token)
        canvas = self.store.read(canvas_id)
        self.assertIsNotNone(canvas.scene_svg)
        self.assertEqual(canvas.elements_json, '[{"id": "a1"}]')

    def _backdate(self, canvas_id: str, age_s: float) -> None:
        """Test helper: rewrite a canvas's created_s directly, since
        create() always stamps the real current time and sweep_expired
        needs canvases that are genuinely old to test against."""
        path = self.store.state_dir / f"{canvas_id}.json"
        data = json.loads(path.read_text())
        data["created_s"] = time.time() - age_s
        path.write_text(json.dumps(data))

    def test_sweep_expired_deletes_old_canvas(self) -> None:
        canvas_id, _ = new_canvas(self.store)
        self._backdate(canvas_id, age_s=200 * 86400)  # 200 days old
        deleted = self.store.sweep_expired(max_age_s=90 * 86400)
        self.assertEqual(deleted, [canvas_id])
        with self.assertRaises(CanvasNotFound):
            self.store.read(canvas_id)

    def test_sweep_expired_keeps_young_canvas(self) -> None:
        canvas_id, _ = new_canvas(self.store)
        self._backdate(canvas_id, age_s=5 * 86400)  # 5 days old
        deleted = self.store.sweep_expired(max_age_s=90 * 86400)
        self.assertEqual(deleted, [])
        self.store.read(canvas_id)  # still there, doesn't raise

    def test_canvas_without_created_s_is_reported_not_backfilled_or_deleted(self) -> None:
        # A record without created_s is reported, untouched.
        canvas_id = "0" * 32
        path = self.store.state_dir / f"{canvas_id}.json"
        content = json.dumps({"token_hash": "irrelevant", "scene_svg": None,
                              "elements_json": None})
        path.write_text(content)
        with self.assertLogs("boring_canvas.storage", "WARNING"):
            deleted = self.store.sweep_expired(max_age_s=0)
        self.assertEqual(deleted, [])
        self.assertEqual(path.read_text(), content)
        self.assertIn(canvas_id, self.store.limits_status()["corrupt_canvases"])
        self.assertIsNone(self.store.read(canvas_id).created_s)  # still openable

    def test_sweep_expired_deletes_only_what_is_actually_old(self) -> None:
        old_id, _ = new_canvas(self.store)
        young_id, _ = new_canvas(self.store)
        self._backdate(old_id, age_s=200 * 86400)
        deleted = self.store.sweep_expired(max_age_s=90 * 86400)
        self.assertEqual(deleted, [old_id])
        self.store.read(young_id)  # untouched

    def test_create_triggers_sweep_as_a_side_effect(self) -> None:
        # 'Common usage triggered' cleanup: create()
        # itself sweeps, no separate scheduling mechanism. Uses the
        # store's own configured default, not a hand-picked max_age_s,
        # so this actually exercises what create() really calls.
        from boring_canvas.storage import DEFAULT_MAX_AGE_DAYS
        old_id, _ = new_canvas(self.store)
        self._backdate(old_id, age_s=(DEFAULT_MAX_AGE_DAYS + 10) * 86400)
        new_canvas(self.store)  # the side effect under test -- no direct sweep_expired() call
        with self.assertRaises(CanvasNotFound):
            self.store.read(old_id)

    def test_max_age_days_is_configurable_per_store(self) -> None:
        # Not just DEFAULT_MAX_AGE_DAYS -- a store built with its own
        # max_age_days uses that value, confirmed by actually exercising
        # create()'s side effect, not by reading the constructor argument
        # back.
        short_lived = CanvasStore(Path(self._tmp.name) / "short", max_age_days=5)
        old_id, _ = new_canvas(short_lived)
        path = short_lived.state_dir / f"{old_id}.json"
        data = json.loads(path.read_text())
        data["created_s"] = time.time() - 10 * 86400  # 10 days: expired under 5, not under 42
        path.write_text(json.dumps(data))
        new_canvas(short_lived)  # triggers the sweep
        with self.assertRaises(CanvasNotFound):
            short_lived.read(old_id)

    def test_create_rate_limit_blocks_burst_past_the_limit(self) -> None:
        limited = CanvasStore(Path(self._tmp.name) / "limited", create_rate_limit=2)
        new_canvas(limited)
        new_canvas(limited)
        with self.assertRaises(RateLimited):
            new_canvas(limited)

    def test_create_rate_limit_does_not_count_the_rejected_attempt(self) -> None:
        limited = CanvasStore(Path(self._tmp.name) / "limited2", create_rate_limit=1)
        new_canvas(limited)
        with self.assertRaises(RateLimited):
            new_canvas(limited)
        # confirm only the one real canvas exists -- the rejected attempt
        # didn't leave anything behind
        self.assertEqual(len(limited.list_canvases()), 1)

    def test_create_rate_limit_zero_means_unlimited(self) -> None:
        unlimited = CanvasStore(Path(self._tmp.name) / "unlimited", create_rate_limit=0)
        for _ in range(20):
            new_canvas(unlimited)  # would have blown a real limit; must not raise
        self.assertEqual(len(unlimited.list_canvases()), 20)

    def test_create_rate_limit_window_expires(self) -> None:
        limited = CanvasStore(Path(self._tmp.name) / "windowed", create_rate_limit=1)
        new_canvas(limited)
        with self.assertRaises(RateLimited):
            new_canvas(limited)
        # simulate the window having passed, without a real sleep
        limited._recent_creates = [t - 61 for t in limited._recent_creates]
        new_canvas(limited)  # should succeed now -- old attempt aged out
        self.assertEqual(len(limited.list_canvases()), 2)

    def test_max_canvases_blocks_once_the_ceiling_is_reached(self) -> None:
        capped = CanvasStore(Path(self._tmp.name) / "capped", max_canvases=2,
                              create_rate_limit=0)
        new_canvas(capped)
        capped.create_named("pond", "one")
        with self.assertRaises(TooManyCanvases):
            capped.create_named("pond", "two")
        with self.assertRaises(TooManyCanvases):
            new_canvas(capped)

    def test_old_ownerless_canvases_count_toward_max_canvases(self) -> None:
        capped = CanvasStore(Path(self._tmp.name) / "legacy-cap", max_canvases=2,
                              create_rate_limit=0)
        legacy_ownerless_canvas(capped)
        new_canvas(capped)
        with self.assertRaises(TooManyCanvases):
            new_canvas(capped)

    def test_old_ownerless_canvas_keeps_working_until_it_expires(self) -> None:
        canvas_id, token = legacy_ownerless_canvas(self.store)
        self.assertTrue(self.store.verify_token(canvas_id, token))
        self.store.write_scene(canvas_id, token, SVG)
        self.assertIn("circle", self.store.read(canvas_id).scene_svg)
        self.assertIsNone(self.store.read(canvas_id).owner)
        self._backdate(canvas_id, age_s=(self.store.max_age_days + 1) * 86400)
        new_canvas(self.store)  # creating sweeps
        with self.assertRaises(CanvasNotFound):
            self.store.read(canvas_id)

    def test_store_has_no_ownerless_create(self) -> None:
        self.assertFalse(hasattr(self.store, "create"))
        self.assertNotIn("owned_creates_in_current_window", self.store.limits_status())

    def test_max_canvases_zero_means_unlimited(self) -> None:
        uncapped = CanvasStore(Path(self._tmp.name) / "uncapped", max_canvases=0,
                                create_rate_limit=0)
        for _ in range(15):
            new_canvas(uncapped)
        self.assertEqual(len(uncapped.list_canvases()), 15)

    def test_max_canvases_counts_drop_after_delete(self) -> None:
        capped = CanvasStore(Path(self._tmp.name) / "capped2", max_canvases=1,
                              create_rate_limit=0)
        first_id, _ = new_canvas(capped)
        with self.assertRaises(TooManyCanvases):
            new_canvas(capped)
        capped.delete(first_id)
        new_canvas(capped)  # room again -- must not raise

    def test_limits_status_reports_configured_values_and_current_standing(self) -> None:
        store = CanvasStore(Path(self._tmp.name) / "reported",
                             max_age_days=7, create_rate_limit=5, max_canvases=50)
        new_canvas(store)
        new_canvas(store)
        status = store.limits_status()
        self.assertEqual(status["max_age_days"], 7)
        self.assertEqual(status["create_rate_limit"], 5)
        self.assertEqual(status["max_canvases"], 50)
        self.assertEqual(status["current_canvas_count"], 2)
        self.assertEqual(status["creates_in_current_window"], 2)


SVG = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>'


class NamedCanvasTests(unittest.TestCase):
    """Named canvases per owner (Pond)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanvasStore(Path(self._tmp.name), create_rate_limit=0)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_named_is_found_by_owner_and_name(self) -> None:
        canvas_id, token = self.store.create_named("pondA", "canvas1")
        canvas = self.store.find_named("pondA", "canvas1")
        self.assertEqual((canvas.canvas_id, canvas.owner, canvas.name),
                         (canvas_id, "pondA", "canvas1"))
        self.assertTrue(self.store.verify_token(canvas_id, token))  # web UI token as before

    def test_same_name_per_owner_is_refused_other_owner_independent(self) -> None:
        a_id, _ = self.store.create_named("pondA", "canvas1")
        with self.assertRaises(NameTaken):
            self.store.create_named("pondA", "canvas1")
        b_id, _ = self.store.create_named("pondB", "canvas1")
        self.assertNotEqual(a_id, b_id)
        self.assertEqual(self.store.find_named("pondB", "canvas1").canvas_id, b_id)
        self.assertEqual(self.store._canvas_count(), 2)

    def test_unknown_name_and_other_owners_name_are_not_found(self) -> None:
        self.store.create_named("pondA", "canvas1")
        for owner, name in (("pondA", "nope"), ("pondB", "canvas1"), ("pondA", "../x")):
            with self.subTest(owner=owner, name=name), self.assertRaises(CanvasNotFound):
                self.store.find_named(owner, name)

    def test_invalid_name_or_owner_is_refused(self) -> None:
        for name in ("", "Canvas", "-a", "a_b", "a" * 64, "../x", ".a"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.create_named("pondA", name)
        with self.assertRaises(ValueError):
            self.store.create_named("", "canvas1")
        self.assertEqual(self.store._canvas_count(), 0)

    def test_concurrent_same_name_creates_exactly_one_wins(self) -> None:
        start = threading.Barrier(16)
        outcomes: list[object] = []

        def attempt() -> None:
            start.wait()
            try:
                outcomes.append(self.store.create_named("pondA", "canvas1"))
            except NameTaken as exc:
                outcomes.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [o for o in outcomes if isinstance(o, tuple)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(outcomes), 16)
        self.assertEqual(self.store._canvas_count(), 1)
        self.assertEqual(self.store.find_named("pondA", "canvas1").canvas_id, winners[0][0])

    def test_losing_the_bind_removes_the_new_file(self) -> None:
        # Deterministic version of the race: a binder that does not hold the
        # records lock (stores on one state dir share it since D7, so this is
        # a hand edit or an older release) writes and binds a canvas of the
        # same name after our early check, before our bind. The atomic bind
        # still decides.
        other = CanvasStore(self.store.state_dir)
        real_bind = self.store._bind_name

        def racing_bind(owner: str, name: str, canvas_id: str) -> None:
            theirs = "e" * 32
            storage._atomic_write_json(other._path(theirs), storage._record(storage.Canvas(
                theirs, storage._hash_token("theirs"), None, None, time.time(), owner, name)))
            other._bind_name(owner, name, theirs)
            real_bind(owner, name, canvas_id)

        self.store._bind_name = racing_bind  # type: ignore[method-assign]
        with self.assertRaises(NameTaken):
            self.store.create_named("pondA", "canvas1")
        self.assertEqual(self.store._canvas_count(), 1)
        self.assertEqual([e["name"] for e in self.store.list_named("pondA")], ["canvas1"])

    def test_list_named_shows_only_the_owners_named_canvases(self) -> None:
        legacy_ownerless_canvas(self.store)  # unnamed, ownerless
        self.store.create_named("pondA", "zeta")
        self.store.create_named("pondA", "alpha")
        self.store.create_named("pondB", "alpha")
        listing = self.store.list_named("pondA")
        self.assertEqual([e["name"] for e in listing], ["alpha", "zeta"])
        self.assertTrue(all(set(e) == {"name", "canvas_id", "created_s"} for e in listing))
        self.assertEqual(self.store.list_named("pondC"), [])

    def test_writes_keep_owner_and_name(self) -> None:
        canvas_id, token = self.store.create_named("pondA", "canvas1")
        self.store.write_named_scene("pondA", "canvas1", SVG)
        self.store.write_elements(canvas_id, token, '[{"id": "a", "version": 1}]')
        token = self.store.rotate_token(canvas_id, token)
        self.store.write_scene(canvas_id, token, SVG)
        canvas = self.store.find_named("pondA", "canvas1")
        self.assertEqual((canvas.owner, canvas.name), ("pondA", "canvas1"))
        self.assertIn("circle", canvas.scene_svg or "")
        self.assertEqual(canvas.elements_json, '[{"id": "a", "version": 1}]')

    def test_write_named_scene_of_unknown_name_is_not_found(self) -> None:
        with self.assertRaises(CanvasNotFound):
            self.store.write_named_scene("pondA", "canvas1", SVG)

    def test_expiry_frees_the_name(self) -> None:
        old_id, _ = self.store.create_named("pondA", "canvas1")
        path = self.store.state_dir / f"{old_id}.json"
        data = json.loads(path.read_text())
        data["created_s"] = time.time() - 200 * 86400
        path.write_text(json.dumps(data))
        new_id, _ = self.store.create_named("pondA", "canvas1")  # sweeps first
        self.assertNotEqual(new_id, old_id)
        self.assertFalse(path.exists())
        self.assertEqual(self.store.find_named("pondA", "canvas1").canvas_id, new_id)

    def test_delete_frees_the_name(self) -> None:
        canvas_id, _ = self.store.create_named("pondA", "canvas1")
        self.store.delete(canvas_id)
        self.assertEqual(self.store.list_named("pondA"), [])
        self.store.create_named("pondA", "canvas1")

    def test_delete_of_corrupt_named_canvas_frees_the_name(self) -> None:
        canvas_id, _ = self.store.create_named("pondA", "canvas1")
        (self.store.state_dir / f"{canvas_id}.json").write_text("{not json")
        with self.assertLogs("boring_canvas.storage", "WARNING"):
            self.assertIn("problem", self.store.list_canvases()[0])
        self.assertIn("problem", self.store.list_named("pondA")[0])
        self.store.delete(canvas_id)
        self.store.create_named("pondA", "canvas1")

    def test_unbound_file_is_invisible_by_name(self) -> None:
        # A crash between writing the file and binding the name leaves this.
        canvas_id, _ = self.store.create_named("pondA", "canvas1")
        (self.store.state_dir / "names").rename(self.store.state_dir / "names-gone")
        self.assertEqual(self.store.list_named("pondA"), [])
        new_id, _ = self.store.create_named("pondA", "canvas1")
        self.assertNotEqual(new_id, canvas_id)
        self.store.delete(canvas_id)  # sweeping the orphan leaves the new binding alone
        self.assertEqual(self.store.find_named("pondA", "canvas1").canvas_id, new_id)

    def test_name_bound_to_missing_file_is_reported(self) -> None:
        canvas_id, _ = self.store.create_named("pondA", "canvas1")
        (self.store.state_dir / f"{canvas_id}.json").unlink()  # by hand, around delete()
        with self.assertLogs("boring_canvas.storage", "ERROR"), self.assertRaises(NameIndexBroken):
            self.store.find_named("pondA", "canvas1")
        with self.assertLogs("boring_canvas.storage", "ERROR"):
            self.assertIn("does not exist", self.store.list_named("pondA")[0]["problem"])
        with self.assertLogs("boring_canvas.storage", "ERROR"), self.assertRaises(NameIndexBroken):
            self.store.create_named("pondA", "canvas1")  # not silently repaired


if __name__ == "__main__":
    unittest.main()


class CreateLockTests(unittest.TestCase):
    """Concurrent creates cannot pass the limit checks together. The write
    of the new file is slowed down so that, without the lock, every thread
    would pass the checks before the first file exists."""

    def _race(self, store: CanvasStore, threads: int) -> list[object]:
        import boring_canvas.storage as storage
        real_write = storage._atomic_write_json

        def slow_write(path: Path, data: dict) -> None:
            time.sleep(0.02)
            real_write(path, data)

        start = threading.Barrier(threads)
        outcomes: list[object] = []

        def attempt(i: int) -> None:
            start.wait()
            try:
                outcomes.append(store.create_named("pondA", f"canvas{i}"))
            except (RateLimited, TooManyCanvases) as exc:
                outcomes.append(exc)

        storage._atomic_write_json = slow_write
        try:
            workers = [threading.Thread(target=attempt, args=(i,)) for i in range(threads)]
            for t in workers:
                t.start()
            for t in workers:
                t.join()
        finally:
            storage._atomic_write_json = real_write
        self.assertEqual(len(outcomes), threads)
        return outcomes

    def test_concurrent_creates_cannot_exceed_max_canvases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CanvasStore(Path(tmp), create_rate_limit=0, max_canvases=3)
            outcomes = self._race(store, 10)
            self.assertEqual(sum(isinstance(o, tuple) for o in outcomes), 3)
            self.assertEqual(sum(isinstance(o, TooManyCanvases) for o in outcomes), 7)
            self.assertEqual(store._canvas_count(), 3)

    def test_concurrent_creates_cannot_exceed_the_rate_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CanvasStore(Path(tmp), create_rate_limit=3, max_canvases=0)
            outcomes = self._race(store, 10)
            self.assertEqual(sum(isinstance(o, tuple) for o in outcomes), 3)
            self.assertEqual(sum(isinstance(o, RateLimited) for o in outcomes), 7)
            self.assertEqual(store._canvas_count(), 3)

    def test_create_with_sweep_and_delete_does_not_deadlock(self) -> None:
        # create holds the lock, sweeps, and the sweep deletes a named
        # canvas (which also takes the names lock).
        with tempfile.TemporaryDirectory() as tmp:
            store = CanvasStore(Path(tmp))
            old_id, _ = store.create_named("pondA", "old")
            path = store.state_dir / f"{old_id}.json"
            data = json.loads(path.read_text())
            data["created_s"] = time.time() - (store.max_age_days + 1) * 86400
            path.write_text(json.dumps(data))
            done = threading.Event()

            def create() -> None:
                store.create_named("pondA", "new")
                done.set()

            threading.Thread(target=create, daemon=True).start()
            self.assertTrue(done.wait(5), "create did not finish")
            self.assertFalse(path.exists())


class RecordLockTests(unittest.TestCase):
    """A browser save and an agent push racing on one record keep both."""

    def test_concurrent_writes_lose_nothing(self) -> None:
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            store = CanvasStore(Path(tmp))
            canvas_id, token = store.create_named("pondA", "canvas1")
            svg = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>'
            barrier = threading.Barrier(2)

            def browser() -> None:
                for i in range(50):
                    barrier.wait() if i == 0 else None
                    store.write_elements(canvas_id, token, f'[{{"id": "e{i}", "version": 1}}]')

            def agent() -> None:
                for i in range(50):
                    barrier.wait() if i == 0 else None
                    store.write_named_scene("pondA", "canvas1", svg)
            threads = [threading.Thread(target=browser), threading.Thread(target=agent)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            canvas = store.read(canvas_id)
            self.assertIn('"e49"', canvas.elements_json)
            self.assertIsNotNone(canvas.scene_svg)
            self.assertTrue(store.verify_token(canvas_id, token))


class AtomicWriteTests(unittest.TestCase):
    """A failed record write keeps the previous record and leaves no temp file."""

    def test_failure_before_or_at_rename_cleans_up(self) -> None:
        from unittest import mock
        for step in ("fsync", "replace"):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "record.json"
                storage._atomic_write_json(path, {"version": 1})
                with mock.patch.object(storage.os, step, side_effect=OSError("injected")):
                    with self.assertRaises(OSError):
                        storage._atomic_write_json(path, {"version": 2})
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"version": 1})
                self.assertEqual([p.name for p in Path(tmp).iterdir()], ["record.json"])


class EditBookkeepingBoundTests(unittest.TestCase):
    """Version markers follow the stored scene, the log its 500-entry bound."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = CanvasStore(Path(self._tmp.name))
        self.canvas_id, self.token = new_canvas(self.store)

    def write(self, *elements: tuple[str, int]) -> None:
        self.store.write_elements(self.canvas_id, self.token, json.dumps(
            [{"id": el_id, "type": "rectangle", "version": version} for el_id, version in elements]))

    def test_a_thousand_replacements_stay_within_the_bound(self) -> None:
        for i in range(1000):
            self.write((f"e{i}", 1))  # each scene: one new element replacing the last
        self.assertEqual(self.store._known_versions[self.canvas_id], {"e999": 1})
        edits = self.store.get_edits(self.canvas_id)
        self.assertEqual(len(edits), storage.EDIT_LOG_ENTRIES)
        self.assertEqual([e["element_id"] for e in edits], [f"e{i}" for i in range(500, 1000)])

    def test_edit_window_stays_correct(self) -> None:
        self.write(("a", 1), ("b", 1))
        self.write(("a", 1), ("b", 1))  # re-save: nothing new
        self.write(("a", 2), ("b", 1))  # a edited
        self.write(("b", 1))            # a gone from the scene: forgotten, nothing logged
        self.assertEqual(set(self.store._known_versions[self.canvas_id]), {"b"})
        self.write(("b", 1), ("a", 2))  # a back (undo): logged again, it changed the scene
        self.assertEqual([(e["element_id"], e["version"]) for e in self.store.get_edits(self.canvas_id)],
                         [("a", 1), ("b", 1), ("a", 2), ("a", 2)])

    def test_a_write_to_a_canvas_deleted_elsewhere_drops_its_bookkeeping(self) -> None:
        self.write(("a", 1))
        CanvasStore(Path(self._tmp.name)).delete(self.canvas_id)  # boring-canvas-admin's store
        with self.assertRaises(CanvasNotFound):
            self.write(("a", 2))
        self.assertNotIn(self.canvas_id, self.store._known_versions)
        self.assertEqual(self.store.get_edits(self.canvas_id), [])

