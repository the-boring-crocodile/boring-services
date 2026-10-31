"""Unit tests for CanvasStore. Pure, no network, no server, no browser."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from boring_canvas import storage
from boring_canvas.storage import (
    PUSHED_SCENE_ELEMENT_ID,
    CanvasNotFound, CanvasStore, InvalidElements, InvalidSVG, InvalidToken,
    NameTaken, RateLimited, StoreRefused, TooManyCanvases,
)

from canvas_fixtures import ClosesStores, backdate, legacy_ownerless_canvas, new_canvas, push


class CanvasStoreTests(ClosesStores):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = self.open_store(Path(self._tmp.name))

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

    def test_write_scene_with_correct_token_persists(self) -> None:
        canvas_id, token = new_canvas(self.store)
        push(self.store, canvas_id, "<svg>rose</svg>")
        self.assertEqual(self.store.read(canvas_id).scene_svg, "<svg>rose</svg>")

    def test_write_scene_strips_script_element(self) -> None:
        canvas_id, token = new_canvas(self.store)
        pushed = '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><circle r="1"/></svg>'
        push(self.store, canvas_id, pushed)
        stored = self.store.read(canvas_id).scene_svg
        self.assertNotIn("script", stored)
        self.assertNotIn("alert", stored)
        self.assertIn("circle", stored)

    def test_write_scene_strips_event_handler_attribute(self) -> None:
        canvas_id, token = new_canvas(self.store)
        pushed = '<svg xmlns="http://www.w3.org/2000/svg"><rect onclick="alert(1)" width="1" height="1"/></svg>'
        push(self.store, canvas_id, pushed)
        stored = self.store.read(canvas_id).scene_svg
        self.assertNotIn("onclick", stored)
        self.assertNotIn("alert", stored)

    def test_write_scene_strips_javascript_uri(self) -> None:
        canvas_id, token = new_canvas(self.store)
        pushed = ('<svg xmlns="http://www.w3.org/2000/svg">'
                  '<a href="javascript:alert(1)"><rect width="1" height="1"/></a></svg>')
        push(self.store, canvas_id, pushed)
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
        push(self.store, canvas_id, pushed)
        stored = self.store.read(canvas_id).scene_svg
        self.assertNotIn("iframe", stored)
        self.assertNotIn("evil.example", stored)
        self.assertNotIn("foreignObject", stored)
        self.assertIn("circle", stored)

    def _stored(self, pushed: str) -> str:
        canvas_id, token = new_canvas(self.store)
        push(self.store, canvas_id, pushed)
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
        push(self.store, canvas_id, pushed)
        stored = self.store.read(canvas_id).scene_svg
        self.assertIn("circle", stored)
        self.assertIn("fill", stored)

    def test_write_scene_rejects_malformed_xml(self) -> None:
        canvas_id, token = new_canvas(self.store)
        with self.assertRaises(InvalidSVG):
            push(self.store, canvas_id, "<svg><unclosed></svg>")
        # rejected push must not have overwritten the (empty) stored state
        self.assertIsNone(self.store.read(canvas_id).scene_svg)

    def test_scene_version_changes_after_write_and_is_stable_before(self) -> None:
        canvas_id, token = new_canvas(self.store)
        v_empty_1 = self.store.scene_version(canvas_id)
        v_empty_2 = self.store.scene_version(canvas_id)
        self.assertEqual(v_empty_1, v_empty_2)  # stable with no writes between
        push(self.store, canvas_id, '<svg xmlns="http://www.w3.org/2000/svg"/>')
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
        push(self.store, canvas_id, '<svg xmlns="http://www.w3.org/2000/svg"/>')
        self.assertEqual(self.store.read(canvas_id).elements_json, '[{"id": "a1"}]')

    def test_write_elements_preserves_existing_scene_svg(self) -> None:
        canvas_id, token = new_canvas(self.store)
        push(self.store, canvas_id, '<svg xmlns="http://www.w3.org/2000/svg"/>')
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
        push(self.store, scene_id, '<svg xmlns="http://www.w3.org/2000/svg"/>')
        listing = {c["canvas_id"]: c for c in self.store.list_canvases()}
        self.assertEqual(set(listing), {empty_id, scene_id})
        self.assertFalse(listing[empty_id]["has_scene"])
        self.assertTrue(listing[scene_id]["has_scene"])
        self.assertFalse(listing[scene_id]["has_elements"])
        self.assertNotIn("token_hash", listing[scene_id])
        self.assertNotIn("token", listing[scene_id])

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

    def test_sweep_expired_deletes_old_canvas(self) -> None:
        canvas_id, _ = new_canvas(self.store)
        backdate(self.store, canvas_id, age_s=200 * 86400)  # 200 days old
        deleted = self.store.sweep_expired(max_age_s=90 * 86400)
        self.assertEqual(deleted, [canvas_id])
        with self.assertRaises(CanvasNotFound):
            self.store.read(canvas_id)

    def test_sweep_expired_keeps_young_canvas(self) -> None:
        canvas_id, _ = new_canvas(self.store)
        backdate(self.store, canvas_id, age_s=5 * 86400)  # 5 days old
        deleted = self.store.sweep_expired(max_age_s=90 * 86400)
        self.assertEqual(deleted, [])
        self.store.read(canvas_id)  # still there, doesn't raise

    def test_sweep_expired_deletes_only_what_is_actually_old(self) -> None:
        old_id, _ = new_canvas(self.store)
        young_id, _ = new_canvas(self.store)
        backdate(self.store, old_id, age_s=200 * 86400)
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
        backdate(self.store, old_id, age_s=(DEFAULT_MAX_AGE_DAYS + 10) * 86400)
        new_canvas(self.store)  # the side effect under test -- no direct sweep_expired() call
        with self.assertRaises(CanvasNotFound):
            self.store.read(old_id)

    def test_max_age_days_is_configurable_per_store(self) -> None:
        # Not just DEFAULT_MAX_AGE_DAYS -- a store built with its own
        # max_age_days uses that value, confirmed by actually exercising
        # create()'s side effect, not by reading the constructor argument
        # back.
        short_lived = self.open_store(Path(self._tmp.name) / "short", max_age_days=5)
        old_id, _ = new_canvas(short_lived)
        backdate(short_lived, old_id, 10 * 86400)  # 10 days: expired under 5, not under 42
        new_canvas(short_lived)  # triggers the sweep
        with self.assertRaises(CanvasNotFound):
            short_lived.read(old_id)

    def test_create_rate_limit_blocks_burst_past_the_limit(self) -> None:
        limited = self.open_store(Path(self._tmp.name) / "limited", create_rate_limit=2)
        new_canvas(limited)
        new_canvas(limited)
        with self.assertRaises(RateLimited):
            new_canvas(limited)

    def test_create_rate_limit_does_not_count_the_rejected_attempt(self) -> None:
        limited = self.open_store(Path(self._tmp.name) / "limited2", create_rate_limit=1)
        new_canvas(limited)
        with self.assertRaises(RateLimited):
            new_canvas(limited)
        # confirm only the one real canvas exists -- the rejected attempt
        # didn't leave anything behind
        self.assertEqual(len(limited.list_canvases()), 1)

    def test_create_rate_limit_zero_means_unlimited(self) -> None:
        unlimited = self.open_store(Path(self._tmp.name) / "unlimited", create_rate_limit=0)
        for _ in range(20):
            new_canvas(unlimited)  # would have blown a real limit; must not raise
        self.assertEqual(len(unlimited.list_canvases()), 20)

    def test_create_rate_limit_window_expires(self) -> None:
        limited = self.open_store(Path(self._tmp.name) / "windowed", create_rate_limit=1)
        new_canvas(limited)
        with self.assertRaises(RateLimited):
            new_canvas(limited)
        # simulate the window having passed, without a real sleep
        limited._recent_creates = [t - 61 for t in limited._recent_creates]
        new_canvas(limited)  # should succeed now -- old attempt aged out
        self.assertEqual(len(limited.list_canvases()), 2)

    def test_max_canvases_blocks_once_the_ceiling_is_reached(self) -> None:
        capped = self.open_store(Path(self._tmp.name) / "capped", max_canvases=2,
                              create_rate_limit=0)
        new_canvas(capped)
        capped.create_named("pond", "one")
        with self.assertRaises(TooManyCanvases):
            capped.create_named("pond", "two")
        with self.assertRaises(TooManyCanvases):
            new_canvas(capped)

    def test_old_ownerless_canvases_count_toward_max_canvases(self) -> None:
        capped = self.open_store(Path(self._tmp.name) / "legacy-cap", max_canvases=2,
                              create_rate_limit=0)
        legacy_ownerless_canvas(capped)
        new_canvas(capped)
        with self.assertRaises(TooManyCanvases):
            new_canvas(capped)

    def test_old_ownerless_canvas_keeps_working_until_it_expires(self) -> None:
        canvas_id, token = legacy_ownerless_canvas(self.store)
        self.assertTrue(self.store.verify_token(canvas_id, token))
        push(self.store, canvas_id, SVG)
        self.assertIn("circle", self.store.read(canvas_id).scene_svg)
        self.assertIsNone(self.store.read(canvas_id).owner)
        backdate(self.store, canvas_id, age_s=(self.store.max_age_days + 1) * 86400)
        new_canvas(self.store)  # creating sweeps
        with self.assertRaises(CanvasNotFound):
            self.store.read(canvas_id)

    def test_store_has_no_ownerless_create(self) -> None:
        self.assertFalse(hasattr(self.store, "create"))
        self.assertNotIn("owned_creates_in_current_window", self.store.limits_status())

    def test_max_canvases_zero_means_unlimited(self) -> None:
        uncapped = self.open_store(Path(self._tmp.name) / "uncapped", max_canvases=0,
                                create_rate_limit=0)
        for _ in range(15):
            new_canvas(uncapped)
        self.assertEqual(len(uncapped.list_canvases()), 15)

    def test_max_canvases_counts_drop_after_delete(self) -> None:
        capped = self.open_store(Path(self._tmp.name) / "capped2", max_canvases=1,
                              create_rate_limit=0)
        first_id, _ = new_canvas(capped)
        with self.assertRaises(TooManyCanvases):
            new_canvas(capped)
        capped.delete(first_id)
        new_canvas(capped)  # room again -- must not raise

    def test_limits_status_reports_configured_values_and_current_standing(self) -> None:
        store = self.open_store(Path(self._tmp.name) / "reported",
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


class NamedCanvasTests(ClosesStores):
    """Named canvases per owner (Pond)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = self.open_store(Path(self._tmp.name), create_rate_limit=0)

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
        self.assertEqual(self.store._count(), 2)

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
        self.assertEqual(self.store._count(), 0)

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
        self.assertEqual(self.store._count(), 1)
        self.assertEqual(self.store.find_named("pondA", "canvas1").canvas_id, winners[0][0])

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
        self.store.reissue_named_token("pondA", "canvas1")
        canvas = self.store.find_named("pondA", "canvas1")
        self.assertEqual((canvas.owner, canvas.name), ("pondA", "canvas1"))
        self.assertIn("circle", canvas.scene_svg or "")
        self.assertEqual(canvas.elements_json, '[{"id": "a", "version": 1}]')

    def test_write_named_scene_of_unknown_name_is_not_found(self) -> None:
        with self.assertRaises(CanvasNotFound):
            self.store.write_named_scene("pondA", "canvas1", SVG)

    def test_expiry_frees_the_name(self) -> None:
        old_id, _ = self.store.create_named("pondA", "canvas1")
        backdate(self.store, old_id, 200 * 86400)
        new_id, _ = self.store.create_named("pondA", "canvas1")  # sweeps first
        self.assertNotEqual(new_id, old_id)
        with self.assertRaises(CanvasNotFound):
            self.store.read(old_id)
        self.assertEqual(self.store.find_named("pondA", "canvas1").canvas_id, new_id)

    def test_delete_frees_the_name(self) -> None:
        canvas_id, _ = self.store.create_named("pondA", "canvas1")
        self.store.delete(canvas_id)
        self.assertEqual(self.store.list_named("pondA"), [])
        self.store.create_named("pondA", "canvas1")

class CreateLockTests(ClosesStores):
    """Concurrent creates cannot pass the limit checks together. Minting the
    id is slowed down so that, without the transaction, every thread would
    pass the checks before the first row exists."""

    def _race(self, store: CanvasStore, threads: int) -> list[object]:
        real_uuid4 = storage.uuid.uuid4

        def slow_uuid4():
            time.sleep(0.02)
            return real_uuid4()

        start = threading.Barrier(threads)
        outcomes: list[object] = []

        def attempt(i: int) -> None:
            start.wait()
            try:
                outcomes.append(store.create_named("pondA", f"canvas{i}"))
            except (RateLimited, TooManyCanvases) as exc:
                outcomes.append(exc)

        storage.uuid.uuid4 = slow_uuid4
        try:
            workers = [threading.Thread(target=attempt, args=(i,)) for i in range(threads)]
            for t in workers:
                t.start()
            for t in workers:
                t.join()
        finally:
            storage.uuid.uuid4 = real_uuid4
        self.assertEqual(len(outcomes), threads)
        return outcomes

    def test_concurrent_creates_cannot_exceed_max_canvases(self) -> None:
        tmp = self.temp_dir()
        store = self.open_store(Path(tmp), create_rate_limit=0, max_canvases=3)
        outcomes = self._race(store, 10)
        self.assertEqual(sum(isinstance(o, tuple) for o in outcomes), 3)
        self.assertEqual(sum(isinstance(o, TooManyCanvases) for o in outcomes), 7)
        self.assertEqual(store._count(), 3)

    def test_concurrent_creates_cannot_exceed_the_rate_limit(self) -> None:
        tmp = self.temp_dir()
        store = self.open_store(Path(tmp), create_rate_limit=3, max_canvases=0)
        outcomes = self._race(store, 10)
        self.assertEqual(sum(isinstance(o, tuple) for o in outcomes), 3)
        self.assertEqual(sum(isinstance(o, RateLimited) for o in outcomes), 7)
        self.assertEqual(store._count(), 3)

    def test_create_with_sweep_and_delete_does_not_deadlock(self) -> None:
        # create sweeps (a transaction of its own) before its own transaction.
        tmp = self.temp_dir()
        store = self.open_store(Path(tmp))
        old_id, _ = store.create_named("pondA", "old")
        backdate(store, old_id, (store.max_age_days + 1) * 86400)
        done = threading.Event()

        def create() -> None:
            store.create_named("pondA", "new")
            done.set()

        threading.Thread(target=create, daemon=True).start()
        self.assertTrue(done.wait(5), "create did not finish")
        with self.assertRaises(CanvasNotFound):
            store.read(old_id)


class RecordLockTests(ClosesStores):
    """A browser save and an agent push racing on one record keep both."""

    def test_concurrent_writes_lose_nothing(self) -> None:
        import threading
        tmp = self.temp_dir()
        store = self.open_store(Path(tmp))
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


class EditBookkeepingBoundTests(ClosesStores):
    """Version markers follow the stored scene, the log its 500-entry bound."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = self.open_store(Path(self._tmp.name))
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
        self.open_store(Path(self._tmp.name)).delete(self.canvas_id)  # boring-canvas-admin's store
        with self.assertRaises(CanvasNotFound):
            self.write(("a", 2))
        self.assertNotIn(self.canvas_id, self.store._known_versions)
        self.assertEqual(self.store.get_edits(self.canvas_id), [])


class LegacyImportTests(ClosesStores):
    """0.2.17 and older kept one JSON file per canvas; the first open imports them."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def legacy(self, canvas_id: str, **fields: object) -> Path:
        record = {"token_hash": storage._hash_token("tok-" + canvas_id), "scene_svg": None,
                  "elements_json": None, "created_s": time.time(), **fields}
        path = self.dir / f"{canvas_id}.json"
        path.write_text(json.dumps(record, indent=2))
        return path

    def test_files_are_imported_once_and_left_unchanged(self) -> None:
        named = self.legacy("a" * 32, owner="pondA", name="plan", scene_svg="<svg/>",
                            elements_json='[{"id": "x", "version": 2}]')
        ownerless = self.legacy("b" * 32)
        (self.dir / "names").mkdir()
        (self.dir / "config.json").write_text("{}")  # not a canvas file: ignored
        before = {p: p.read_bytes() for p in (named, ownerless)}
        with self.assertLogs("boring_canvas.storage", "WARNING") as logs:
            store = self.open_store(self.dir)
        self.assertIn("imported 2 canvases", logs.output[0])
        canvas = store.find_named("pondA", "plan")
        self.assertEqual((canvas.canvas_id, canvas.scene_svg, canvas.elements_json),
                         ("a" * 32, "<svg/>", '[{"id": "x", "version": 2}]'))
        self.assertTrue(store.verify_token("a" * 32, "tok-" + "a" * 32))
        self.assertIsNone(store.read("b" * 32).owner)
        self.assertEqual({p: p.read_bytes() for p in before}, before)  # a rollback reads them
        # A second open (the admin, a restart) imports nothing again.
        store.delete("b" * 32)
        again = self.open_store(self.dir)
        with self.assertRaises(CanvasNotFound):
            again.read("b" * 32)

    def test_an_unusable_file_refuses_the_open_and_imports_nothing(self) -> None:
        self.legacy("a" * 32)
        (self.dir / ("c" * 32 + ".json")).write_text("{broken")
        with self.assertRaises(StoreRefused) as caught:
            self.open_store(self.dir)
        self.assertIn("c" * 32 + ".json", str(caught.exception))
        (self.dir / ("c" * 32 + ".json")).unlink()  # the operator moves it aside
        self.assertEqual(len(self.open_store(self.dir).list_canvases()), 1)

    def test_records_missing_fields_or_half_owned_are_refused(self) -> None:
        for fields in ({"created_s": None}, {"token_hash": None}, {"owner": "pondA"},
                       {"owner": "pondA", "name": "Bad Name"}, {"scene_svg": 3}):
            with self.subTest(fields=fields):
                tmp = self.temp_dir()
                self.dir = Path(tmp)
                self.legacy("a" * 32, **fields)
                with self.assertRaises(StoreRefused):
                    self.open_store(self.dir)

    def test_two_files_with_one_name_are_refused(self) -> None:
        self.legacy("a" * 32, owner="pondA", name="plan")
        self.legacy("b" * 32, owner="pondA", name="plan")
        with self.assertRaisesRegex(StoreRefused, "a{32}.json and b{32}.json both name pondA/plan"):
            self.open_store(self.dir)
        self.legacy("b" * 32, owner="pondA", name="other")
        self.assertEqual(len(self.open_store(self.dir).list_named("pondA")), 2)

    def test_unknown_schema_is_refused_unchanged(self) -> None:
        store = self.open_store(self.dir)
        with store._write():
            store.db.execute("UPDATE meta SET value = '99' WHERE key = 'schema'")
        store.close()
        with self.assertRaises(StoreRefused):
            self.open_store(self.dir)


class TwoProcessesTests(ClosesStores):
    """The admin's store beside the server's: one database, serialized writes."""

    def test_saves_and_reissues_from_two_stores_are_kept_or_refused(self) -> None:
        tmp = self.temp_dir()
        server, admin = self.open_store(Path(tmp)), self.open_store(Path(tmp))
        canvas_id, token = server.create_named("pondA", "canvas1")
        errors: list[Exception] = []

        def saves() -> None:
            for i in range(40):
                try:
                    server.write_elements(canvas_id, token, f'[{{"id": "e{i}", "version": 1}}]')
                except Exception as exc:  # reported below
                    errors.append(exc)

        def reissues() -> None:
            for _ in range(40):
                admin.reissue_named_token("pondA", "canvas1")

        threads = [threading.Thread(target=saves), threading.Thread(target=reissues)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # Once the admin reissued, the old token no longer saves: refused, never lost silently.
        self.assertTrue(all(isinstance(e, InvalidToken) for e in errors))
        self.assertFalse(server.verify_token(canvas_id, token))
        # Every save that was not refused is kept: the stored scene is the last of them.
        kept = 40 - len(errors)
        stored = self.open_store(Path(tmp)).read(canvas_id).elements_json
        if kept:
            self.assertEqual([e["id"] for e in json.loads(stored)], [f"e{kept - 1}"])

    def test_writes_from_two_processes_interleave_without_loss(self) -> None:
        tmp = self.temp_dir()
        self.open_store(Path(tmp)).close()  # schema first, then two writers
        code = ("import sys; from pathlib import Path; from boring_canvas.storage import CanvasStore\n"
                "s = CanvasStore(Path(sys.argv[1]), create_rate_limit=0, max_canvases=0)\n"
                "for i in range(25): s.create_named(sys.argv[2], f'c{i}')\n")
        procs = [subprocess.Popen([sys.executable, "-c", code, tmp, owner]) for owner in ("pondA", "pondB")]
        self.assertEqual([p.wait(timeout=60) for p in procs], [0, 0])
        store = self.open_store(Path(tmp))
        self.assertEqual((len(store.list_named("pondA")), len(store.list_named("pondB"))), (25, 25))


if __name__ == "__main__":
    unittest.main()
