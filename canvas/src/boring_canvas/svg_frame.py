"""Hand-drawn marks in the pushed SVG's own coordinates.

The browser shows a pushed SVG as one Excalidraw image element
(storage.PUSHED_SCENE_ELEMENT_ID): placed and possibly resized by the person,
in canvas pixels. The agent drew the SVG in its user units (its viewBox). To
say which box a mark means, a canvas-pixel position is mapped back in two
steps, as the browser draws it:

    the image element's box  <-  the SVG's intrinsic size (width/height),
                                 stretched to the element's box
    the intrinsic box        <-  the viewBox, fitted by preserveAspectRatio
                                 (meet or slice with its alignment, or none)

Without width/height the intrinsic size takes the viewBox's proportions, so
the viewBox maps straight onto the element. Only what is known is mapped: a
rotated, flipped or cropped picture, an SVG whose size the browser had to
guess, or an attribute that does not parse gives a reason instead of
numbers (never a guess).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

_NUMBER = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
_LIST = re.compile(rf"^\s*({_NUMBER})[\s,]+({_NUMBER})[\s,]+({_NUMBER})[\s,]+({_NUMBER})\s*$")
_LENGTH = re.compile(rf"^\s*({_NUMBER})\s*(px)?\s*$")
_ALIGN = re.compile(r"^x(Min|Mid|Max)Y(Min|Mid|Max)$")
_FRACTION = {"Min": 0.0, "Mid": 0.5, "Max": 1.0}


class Unmappable(Exception):
    """Why marks cannot be given in the SVG's coordinates (the message says)."""


@dataclass(frozen=True)
class View:
    """The pushed SVG's coordinate system: its viewBox and how it fits a viewport."""
    x: float
    y: float
    width: float
    height: float
    intrinsic_width: float  # the picture's own size (width/height), user units' viewport
    intrinsic_height: float
    align: tuple[float, float] | None  # None: preserveAspectRatio="none"
    slice: bool = False

    def public(self) -> dict[str, Any]:
        return {"viewBox": [self.x, self.y, self.width, self.height]}


def view_of(scene_svg: str) -> View:
    """The coordinate system of a stored (already sanitized, well-formed) SVG."""
    try:
        root = ET.fromstring(scene_svg)
    except ET.ParseError as exc:  # stored scenes parse; reported, not guessed
        raise Unmappable(f"the pushed SVG does not parse: {exc}") from None
    raw = root.get("viewBox")
    given = [root.get(key) for key in ("width", "height")]
    sizes = [_LENGTH.match(value) if value is not None else None for value in given]
    if any(value is not None and size is None for value, size in zip(given, sizes)):
        raise Unmappable("the pushed SVG's width/height are not plain px lengths: "
                         "its size in the browser is not known here")
    if raw is not None:
        match = _LIST.match(raw)
        if not match:
            raise Unmappable(f"viewBox {raw!r} is not four numbers")
        x, y, width, height = (float(v) for v in match.groups())
        if width <= 0 or height <= 0:
            raise Unmappable("the pushed SVG's viewBox has no area")
        if all(sizes):
            intrinsic = (float(sizes[0].group(1)), float(sizes[1].group(1)))
        elif any(sizes):
            raise Unmappable("the pushed SVG gives only one of width/height: "
                             "its proportions in the browser are not known here")
        else:
            intrinsic = (width, height)  # the viewBox's proportions
    elif all(sizes):
        x, y = 0.0, 0.0
        width, height = intrinsic = (float(sizes[0].group(1)), float(sizes[1].group(1)))
    else:
        raise Unmappable("the pushed SVG has no viewBox and no width/height: "
                         "the browser guessed its size")
    if min(width, height, *intrinsic) <= 0:
        raise Unmappable("the pushed SVG has no area")
    words = (root.get("preserveAspectRatio") or "xMidYMid meet").split()
    if words and words[0] == "defer":
        words = words[1:]
    if not words or len(words) > 2 or (len(words) == 2 and words[1] not in ("meet", "slice")):
        raise Unmappable(f"preserveAspectRatio {root.get('preserveAspectRatio')!r} does not parse")
    if words[0] == "none":
        return View(x, y, width, height, *intrinsic, None)
    align = _ALIGN.match(words[0])
    if not align:
        raise Unmappable(f"preserveAspectRatio {root.get('preserveAspectRatio')!r} does not parse")
    return View(x, y, width, height, *intrinsic, (_FRACTION[align.group(1)], _FRACTION[align.group(2)]),
                slice=len(words) == 2 and words[1] == "slice")


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class Frame:
    """Canvas pixels -> SVG user units for one placement of the pushed picture."""
    view: View
    scale_x: float  # canvas px per user unit
    scale_y: float
    offset_x: float  # canvas px of the viewBox's origin
    offset_y: float

    def point(self, x: float, y: float) -> tuple[float, float]:
        return (self.view.x + (x - self.offset_x) / self.scale_x,
                self.view.y + (y - self.offset_y) / self.scale_y)

    def length(self, dx: float, dy: float) -> tuple[float, float]:
        return dx / self.scale_x, dy / self.scale_y


def frame_of(view: View, image: dict[str, Any]) -> Frame:
    """The mapping for the pushed picture as the browser placed it."""
    if not all(_num(image.get(key)) for key in ("x", "y", "width", "height")):
        raise Unmappable("the pushed picture has no position or size yet")
    if image.get("angle"):
        raise Unmappable("the pushed picture is rotated")
    if image.get("scale") not in (None, [1, 1]):
        raise Unmappable("the pushed picture is flipped")
    if image.get("crop"):
        raise Unmappable("the pushed picture is cropped")
    ix, iy, iw, ih = (float(image[key]) for key in ("x", "y", "width", "height"))
    if iw <= 0 or ih <= 0:
        raise Unmappable("the pushed picture has no area")
    # Within the intrinsic box (W x H): the viewBox fitted, in intrinsic units.
    W, H = view.intrinsic_width, view.intrinsic_height
    if view.align is None:
        fx, fy, ox, oy = W / view.width, H / view.height, 0.0, 0.0
    else:
        fit = max if view.slice else min
        fx = fy = fit(W / view.width, H / view.height)
        ox, oy = (W - view.width * fx) * view.align[0], (H - view.height * fy) * view.align[1]
    # The intrinsic box stretched onto the element's box.
    sx, sy = iw / W, ih / H
    return Frame(view, fx * sx, fy * sy, ix + ox * sx, iy + oy * sy)


def _round(value: float) -> float:
    return round(value, 1) + 0.0  # no -0.0


def in_svg(frame: Frame, element: dict[str, Any]) -> dict[str, Any] | None:
    """The element's box (unrotated, as Excalidraw keeps it) in user units, and a
    line's or arrow's points relative to that box; None without a box."""
    if not all(_num(element.get(key)) for key in ("x", "y")):
        return None
    x, y = frame.point(element["x"], element["y"])
    out: dict[str, Any] = {"x": _round(x), "y": _round(y)}
    if _num(element.get("width")) and _num(element.get("height")):
        width, height = frame.length(element["width"], element["height"])
        out.update(width=_round(width), height=_round(height))
    points = element.get("points")
    if element.get("type") in ("line", "arrow") and isinstance(points, list):
        out["points"] = [[_round(v) for v in frame.length(p[0], p[1])] for p in points
                         if isinstance(p, list) and len(p) >= 2 and _num(p[0]) and _num(p[1])]
    return out
