"""Canvas as an MCP service of the Pond (guest architecture, phase 5).

Serves MCP (SDK MCPServer, Streamable HTTP, stateless) on its own internal
port, in the same process as the stdlib web server, which stays untouched.

- Tools: create_canvas, push_scene, get_elements, get_edits, list_canvases,
  delete_canvas.
  A canvas is addressed by name; names are per caller. A normal outcome (not found, name taken, rate
  limited, ...) is a tool error whose text is the JSON {"code", "message"};
  codes from the Pond's shared set. A taken name adds "reason": "name_taken".
- The SDK answers prompts/list and resources/list with empty lists and
  advertises both in discover; canvas has neither. Left as the SDK does it.
- Sign-in, the caller and the heartbeat come from boring-service-kit's
  `ServiceConfig` (its own issuer, the Crocodile's introspection, or access
  keys): the caller is `calling_author()`, never an argument; tools are
  wrapped with `permitted()` and the kit's compact results and outcomes.
"""
from __future__ import annotations

import json
import shlex
from datetime import datetime, timezone
from typing import Annotated, Any, Callable
from urllib.parse import urlencode

from boring_service_kit.access import calling_author
from boring_service_kit.pond import attach_heartbeat
from boring_service_kit.tools import READ_ONLY, WRITES, as_mcp_tool, refuse_unknown_arguments
from boring_service_kit.access import permitted
from boring_service_kit.calllog import CallLog
from boring_service_kit.outcomes import Refused
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .server import UNSAFE_URL_AUTH_PARAM
from .storage import (
    EDIT_LOG_ENTRIES, NAME_PATTERN, PUSHED_SCENE_ELEMENT_ID, Canvas, CanvasNotFound, CanvasStore, InvalidSVG, NameTaken,
    RateLimited, TooManyCanvases, scene_version_of,
)
from .svg_frame import Unmappable, frame_of, in_svg, view_of


# Bound on the compact JSON of a get_elements/get_edits answer (one compact
# text block on the wire).
MAX_ANSWER_BYTES = 16 * 1024


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode())


# ---------------------------------------------------------------- compact elements

def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compact_element(el: dict[str, Any]) -> dict[str, Any]:
    """What an agent needs from one Excalidraw element: identity, geometry,
    text, colours and links. Rendering internals (seed, version, updated,
    roundness, boundElements, ...) and freehand points are left out; empty,
    null and default values are omitted."""
    out: dict[str, Any] = {"id": el.get("id"), "type": el.get("type")}
    for key in ("x", "y", "width", "height"):
        if _num(el.get(key)):
            out[key] = round(el[key])
    if _num(el.get("angle")) and el["angle"]:
        out["angle"] = round(el["angle"], 2)
    for key in ("strokeColor", "backgroundColor"):
        if el.get(key) not in (None, "", "transparent"):
            out[key] = el[key]
    for key in ("text", "containerId", "groupIds", "frameId", "link", "name",
                "startArrowhead", "endArrowhead"):
        if el.get(key) not in (None, "", []):
            out[key] = el[key]
    for key, short in (("startBinding", "start"), ("endBinding", "end")):
        binding = el.get(key)
        if isinstance(binding, dict) and binding.get("elementId"):
            out[short] = binding["elementId"]
    points = el.get("points")
    if el.get("type") in ("line", "arrow") and isinstance(points, list):
        out["points"] = [[round(p[0]), round(p[1])] for p in points
                         if isinstance(p, list) and len(p) >= 2 and _num(p[0]) and _num(p[1])]
    return out


def compact_elements(elements: Any) -> list[dict[str, Any]]:
    """The live (not deleted) elements of a stored scene, compacted."""
    if not isinstance(elements, list):
        raise ValueError("stored elements are not a JSON list")
    return [compact_element(el) for el in elements
            if isinstance(el, dict) and not el.get("isDeleted")]


def scene_answer(elements: Any, scene_svg: str | None) -> dict[str, Any]:
    """get_elements' answer: the compact live elements and, with a pushed SVG
    the browser has placed, each other element's box in that SVG's own
    coordinates (`svg`) and the SVG's viewBox (`drawing`). When the mapping is
    not known, `svg_coordinates` says why and no `svg` fields are given."""
    if not isinstance(elements, list):
        raise ValueError("stored elements are not a JSON list")
    live = [el for el in elements if isinstance(el, dict) and not el.get("isDeleted")]
    answer: dict[str, Any] = {"elements": [compact_element(el) for el in live]}
    if not scene_svg:
        return answer
    image = next((el for el in live if el.get("id") == PUSHED_SCENE_ELEMENT_ID), None)
    try:
        if image is None:
            raise Unmappable("no browser has placed the pushed drawing yet (or it was deleted)")
        placement = image.get("customData")
        if not isinstance(placement, dict) or placement.get("boringCanvasSceneVersion") != scene_version_of(scene_svg):
            raise Unmappable("browser placement is not confirmed for the current drawing")
        frame = frame_of(view_of(scene_svg), image)
    except Unmappable as exc:
        answer["svg_coordinates"] = f"unavailable: {exc}"
        return answer
    answer["drawing"] = {"element_id": PUSHED_SCENE_ELEMENT_ID, **frame.view.public()}
    for el, out in zip(live, answer["elements"]):
        if el is not image:
            mapped = in_svg(frame, el)
            if mapped is not None:
                out["svg"] = mapped
    return answer


# ---------------------------------------------------------------- server

CanvasName = Annotated[str, Field(pattern=NAME_PATTERN, description=(
    "Canvas name, unique per caller: lowercase letters, digits and '-', starting with a "
    "letter or digit, at most 63 characters."))]


def _iso(created_s: float) -> str:
    return datetime.fromtimestamp(created_s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


INSTRUCTIONS = (
    "Drawings a person sees and can edit in a browser. create_canvas, then push_scene with an SVG; "
    "get_edits reads back what the person drew. Each canvas has its own browser token, separate "
    "from your MCP access. "
)
UNSAFE_URL_INSTRUCTIONS = (
    "Give the person open_url: it signs them in once and carries the canvas token, so it goes to that "
    "person only and nowhere else (not into files, commits or shared notes). open_canvas makes a new "
    "one for an existing canvas."
)
SAFE_URL_INSTRUCTIONS = (
    "This server hands out only the canvas URL, never its browser token: you never see it. Give the "
    "person the url and tell them to ask the operator of the canvas server for the token, who runs "
    "the command create_canvas returns in token_from_operator (it prints a fresh token once; an "
    "older one stops working). The person opens the url and enters the token."
)


def operator_token_command(owner: str, name: str) -> str:
    """The host command that issues the browser token of `owner`'s canvas `name` (admin.py)."""
    return f"sudo boring-canvas-admin token {name} --owner {shlex.quote(owner)}"


def build_server(store: CanvasStore, public_base_url: str, auth: dict[str, Any],
                 on_discover: Callable[[str], None] | None,
                 unsafe_url_auth: bool = False, caller: Callable[[], str] = calling_author) -> MCPServer:
    """The MCPServer with canvas's tools; `auth` and the heartbeat come from boring-service-kit."""
    server = MCPServer(
        "boring-canvas",
        title="Boring Canvas",
        description="Chat-driven drawable canvas (Excalidraw): push SVGs, "
                    "read back hand-drawn scenes.",
        instructions=INSTRUCTIONS + (UNSAFE_URL_INSTRUCTIONS if unsafe_url_auth else SAFE_URL_INSTRUCTIONS),
        **auth,
    )
    attach_heartbeat(server, on_discover)

    def refusal(code: str, message: str, **details: Any) -> Exception:
        return Refused(code, message, **details)

    def tool(permission: str, **options: Any) -> Any:
        def register(fn: Any) -> Any:
            wrapped = as_mcp_tool(permitted(fn, permission), CallLog(None))
            server.tool(annotations=READ_ONLY if permission == "reader" else WRITES, **options)(wrapped)
            return fn
        return register


    def _url(canvas_id: str) -> str:
        return f"{public_base_url}/canvas/{canvas_id}"

    def _canvas(name: str) -> Canvas:
        try:
            return store.find_named(caller(), name)
        except CanvasNotFound:
            raise refusal("not_found", f"no canvas named {name}") from None

    expiry = (f"A canvas is deleted, with everything drawn on it, {store.max_age_days:g} days "
              f"after it was created (when the next canvas is created after that); there is "
              f"no warning.")
    create_description = ("Create a canvas called `name`; returns its name and URL. Names are "
                          f"per caller: only your caller sees and uses its canvases. {expiry} "
                          "list_canvases shows when each one expires.")
    if unsafe_url_auth:
        create_description += (" open_url opens the canvas already signed in: it carries the "
                               "canvas token, so anyone with it can edit the canvas. Give it "
                               "only to the person it is for.")
    else:
        create_description += (" The browser token is not returned to you: give the person the "
                               "url and tell them to ask the operator of the canvas server for "
                               "the token, who runs the command in token_from_operator on the "
                               "server host; the person enters the token on the page.")

    @tool("writer", description=create_description)
    def create_canvas(name: CanvasName) -> dict[str, str]:
        try:
            canvas_id, token = store.create_named(caller(), name)
        except NameTaken:
            # Still the shared code (choose another name), with a reason a
            # caller can test for instead of parsing the message.
            raise refusal("invalid_request", f"a canvas named {name} already exists",
                           reason="name_taken") from None
        except RateLimited as exc:
            raise refusal("busy", str(exc)) from None
        except TooManyCanvases as exc:
            raise refusal("limit", str(exc)) from None
        answer = {"name": name, "url": _url(canvas_id)}
        if unsafe_url_auth:
            answer["open_url"] = f"{_url(canvas_id)}#{urlencode({UNSAFE_URL_AUTH_PARAM: token})}"
        else:
            # The token made here is discarded (only its hash is stored): the
            # operator's command issues the one the person uses.
            answer["token_from_operator"] = operator_token_command(caller(), name)
        return answer

    @tool("writer", description="Replace the pushed drawing of your caller's canvas `name` with an "
                             "SVG, sanitized server-side; hand-drawn elements stay. Through a Pond "
                             "(service_call), pass an SVG file instead of its text: "
                             "\"svg\": {\"$file\": {\"root\": ..., \"path\": ...}}; the Pond "
                             "reads the file and sends its text, so it does not pass through you.")
    def push_scene(name: CanvasName, svg: str) -> dict[str, bool]:
        try:
            store.write_named_scene(caller(), name, svg)
        except CanvasNotFound:
            raise refusal("not_found", f"no canvas named {name}") from None
        except InvalidSVG as exc:
            raise refusal("invalid_request", str(exc)) from None
        return {"ok": True}

    @tool("reader", description=(
        "Read back the current scene of your caller's canvas `name`, compact. A pushed SVG "
        "becomes an element once "
        "a browser has opened the canvas; before that the scene can be empty. One entry per "
        "live element with id, type, x, y, width, height (px, rounded) and, when set: angle "
        "(rad), strokeColor, backgroundColor, text, containerId (shape holding this text), "
        "groupIds, frameId, link, name, start/end (ids an arrow is bound to), "
        "startArrowhead/endArrowhead, points (line/arrow, relative to x,y). Deleted elements, "
        "freehand points and rendering internals are left out. With a pushed SVG placed in "
        "the browser, each other element also has `svg`: its x, y, width, height (and a "
        "line's points) in your SVG's own coordinates (its viewBox, in `drawing`), so a mark "
        "names the box it was drawn on without arithmetic; the box is the element's unrotated "
        "one. When that mapping is not known (the picture rotated, flipped or cropped, or the "
        "SVG's size unknown, or browser placement belongs to another drawing version), "
        "`svg_coordinates` says why instead. A scene over "
        f"{MAX_ANSWER_BYTES} bytes in this form is refused with code \"limit\"."))
    def get_elements(name: CanvasName) -> dict[str, Any]:
        canvas = _canvas(name)
        answer = scene_answer(json.loads(canvas.elements_json) if canvas.elements_json else [],
                              canvas.scene_svg)
        size = _size(answer)
        if size > MAX_ANSWER_BYTES:
            raise refusal("limit", f"scene has {len(answer['elements'])} elements, {size} bytes "
                                    f"even compact; the limit is {MAX_ANSWER_BYTES}")
        return answer

    @tool("reader", description=(
        "List elements changed on your caller's canvas `name` since the backend last restarted "
        "(does not persist across restarts), oldest first: seq, element_id, type, version, "
        "is_deleted, client_updated_ms, server_received_s; your pushed drawing (pushed_scene: "
        "true) counts only when it was moved, resized, rotated or deleted, not when a page "
        "loads it. The newest edits that fit in "
        f"{MAX_ANSWER_BYTES} bytes are returned; `omitted` counts the older ones left out, and "
        "`before` (the seq to pass as `before` next) pages back to them. The backend keeps the "
        f"newest {EDIT_LOG_ENTRIES} edits per canvas."))
    def get_edits(name: CanvasName,
                  before: Annotated[int | None, Field(ge=1, description=(
                      "Only edits with a seq below this: the `before` of the previous answer, "
                      "to page back."))] = None) -> dict[str, Any]:
        edits = store.get_edits(_canvas(name).canvas_id)
        if before is not None:
            edits = [edit for edit in edits if edit.get("seq", 0) < before]
        kept, size = 0, 0
        for edit in reversed(edits):
            size += _size(edit) + 1
            if size > MAX_ANSWER_BYTES:
                break
            kept += 1
        page = edits[len(edits) - kept:]
        answer: dict[str, Any] = {"edits": page, "omitted": len(edits) - kept}
        if answer["omitted"] and page:
            answer["before"] = page[0]["seq"]
        return answer

    @tool("reader", description=(
        "List your caller's canvases by name: name, url, created and expires (UTC, ISO 8601). "
        f"{expiry} Other callers' canvases are not listed."))
    def list_canvases() -> dict[str, list[dict[str, str]]]:
        max_age_s = store.max_age_days * 86400
        return {"canvases": [{"name": entry["name"], "url": _url(entry["canvas_id"]),
                              "created": _iso(entry["created_s"]),
                              "expires": _iso(entry["created_s"] + max_age_s)}
                             for entry in store.list_named(caller())]}

    @tool("writer", description=(
        "Delete your caller's canvas `name`, with everything drawn on it; there is no undo. "
        "Open browsers on it stop working. Only your caller's canvases can be deleted; the "
        "name is free again afterwards."))
    def delete_canvas(name: CanvasName) -> dict[str, bool]:
        try:
            store.delete_named(caller(), name)
        except CanvasNotFound:
            raise refusal("not_found", f"no canvas named {name}") from None
        return {"deleted": True}

    if unsafe_url_auth:
        @tool("writer", description=(
            "A fresh signed-in link for your caller's canvas `name`; browsers already open on it "
            "are signed out and need the new link. open_url carries the canvas token: anyone "
            "with it can edit the canvas. Give it only to the person it is for."))
        def open_canvas(name: CanvasName) -> dict[str, str]:
            try:
                canvas_id, token = store.reissue_named_token(caller(), name)
            except CanvasNotFound:
                raise refusal("not_found", f"no canvas named {name}") from None
            return {"name": name, "url": _url(canvas_id),
                    "open_url": f"{_url(canvas_id)}#{urlencode({UNSAFE_URL_AUTH_PARAM: token})}"}

    refuse_unknown_arguments(server)
    return server
