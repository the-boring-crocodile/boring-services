"""Marks in the pushed SVG's coordinates (svg_frame): the browser's own drawing
model (intrinsic size stretched to the element, viewBox fitted inside it)."""
from __future__ import annotations

import unittest

from boring_canvas.svg_frame import Unmappable, frame_of, in_svg, view_of

NS = 'xmlns="http://www.w3.org/2000/svg"'


def image(x=0.0, y=0.0, width=100.0, height=100.0, **fields):
    return {"id": "boring-canvas-pushed-scene", "type": "image", "x": x, "y": y,
            "width": width, "height": height, "angle": 0, "scale": [1, 1], "crop": None, **fields}


class ViewTests(unittest.TestCase):
    def test_viewbox_with_comma_and_negative_origin(self):
        view = view_of(f'<svg {NS} viewBox="-10,-20 200 100"/>')
        self.assertEqual((view.x, view.y, view.width, view.height), (-10, -20, 200, 100))
        self.assertEqual((view.intrinsic_width, view.intrinsic_height), (200, 100))

    def test_size_only_is_a_viewbox_from_the_origin(self):
        view = view_of(f'<svg {NS} width="300px" height="150"/>')
        self.assertEqual((view.x, view.y, view.width, view.height), (0, 0, 300, 150))

    def test_unknown_sizes_are_refused_not_guessed(self):
        for svg in (f'<svg {NS}/>', f'<svg {NS} width="50%" height="10" viewBox="0 0 1 1"/>',
                    f'<svg {NS} width="10" viewBox="0 0 1 1"/>', f'<svg {NS} viewBox="0 0 0 5"/>',
                    f'<svg {NS} viewBox="0 0 a 5"/>',
                    f'<svg {NS} viewBox="0 0 1 1" preserveAspectRatio="xMidYMid wobble"/>'):
            with self.subTest(svg=svg), self.assertRaises(Unmappable):
                view_of(svg)


class FrameTests(unittest.TestCase):
    def test_moved_and_scaled_picture(self):
        """viewBox 0 0 400 200 drawn 800x400 at (1000, 500): canvas px / 2, shifted."""
        frame = frame_of(view_of(f'<svg {NS} viewBox="0 0 400 200"/>'),
                         image(1000, 500, 800, 400))
        mark = {"type": "rectangle", "x": 1200, "y": 600, "width": 100, "height": 40}
        self.assertEqual(in_svg(frame, mark), {"x": 100.0, "y": 50.0, "width": 50.0, "height": 20.0})

    def test_viewbox_origin_counts(self):
        frame = frame_of(view_of(f'<svg {NS} viewBox="50 -30 100 100"/>'), image(0, 0, 200, 200))
        self.assertEqual(frame.point(20, 40), (60.0, -10.0))

    def test_meet_centres_a_wide_viewbox_in_a_square_picture(self):
        """viewBox 200x100 in a 100x100 intrinsic box (xMidYMid meet): scale 0.5,
        25 px empty above; the picture shown at 3x."""
        svg = f'<svg {NS} width="100" height="100" viewBox="0 0 200 100"/>'
        frame = frame_of(view_of(svg), image(0, 0, 300, 300))
        self.assertEqual(frame.point(0, 75), (0.0, 0.0))
        self.assertEqual(frame.point(300, 225), (200.0, 100.0))

    def test_slice_and_min_alignment(self):
        svg = (f'<svg {NS} width="100" height="100" viewBox="0 0 200 100" '
               'preserveAspectRatio="xMinYMin slice"/>')
        frame = frame_of(view_of(svg), image(0, 0, 100, 100))
        self.assertEqual(frame.point(100, 100), (100.0, 100.0))  # scale 1, the right half cut

    def test_none_stretches(self):
        svg = (f'<svg {NS} width="100" height="100" viewBox="0 0 200 100" '
               'preserveAspectRatio="none"/>')
        frame = frame_of(view_of(svg), image(0, 0, 100, 100))
        self.assertEqual(frame.point(50, 50), (100.0, 50.0))

    def test_a_person_stretching_the_picture(self):
        """Resized to 2x wide, 1x high: x and y scale differently."""
        frame = frame_of(view_of(f'<svg {NS} viewBox="0 0 100 100"/>'), image(0, 0, 200, 100))
        mark = {"type": "arrow", "x": 100, "y": 50, "width": 40, "height": 10,
                "points": [[0, 0], [40, 10]]}
        self.assertEqual(in_svg(frame, mark), {"x": 50.0, "y": 50.0, "width": 20.0, "height": 10.0,
                                               "points": [[0.0, 0.0], [20.0, 10.0]]})

    def test_rotated_flipped_or_cropped_pictures_are_refused(self):
        view = view_of(f'<svg {NS} viewBox="0 0 1 1"/>')
        for fields in ({"angle": 0.3}, {"scale": [-1, 1]}, {"crop": {"x": 1}}, {"width": 0},
                       {"x": None}):
            with self.subTest(fields=fields), self.assertRaises(Unmappable):
                frame_of(view, image(**fields))

    def test_an_element_without_a_box_has_no_svg_fields(self):
        frame = frame_of(view_of(f'<svg {NS} viewBox="0 0 1 1"/>'), image())
        self.assertIsNone(in_svg(frame, {"type": "text"}))


if __name__ == "__main__":
    unittest.main()
