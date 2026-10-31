"""Canvas page rendering, CSP and fixed web-route vocabulary."""
from __future__ import annotations
import html as html_lib
import json
import re
import hashlib
from pathlib import Path

from .storage import scene_version_of

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "canvas.html"


def page_title(name: str | None, authed: bool) -> str:
    """Browser tab title: the canvas name for a signed-in viewer, so several
    open canvases can be told apart; nothing about the canvas otherwise."""
    if authed and name:
        return f"{html_lib.escape(name)} \u00b7 Boring Canvas"
    return "Boring Canvas"

_SLOT = re.compile(r"%%([A-Z_]+)%%")


def script_json(json_text: str) -> str:
    """JSON text made safe to embed in an inline <script> element.

    The HTML parser ends a script element at the first "</script", whatever
    JavaScript would make of it, so a stored string such as
    "</script><script>..." would otherwise break out of the data and run.
    "<", ">" and "&" can only occur inside JSON strings, where the escapes
    below mean the same characters; U+2028/2029 are escaped for older
    JavaScript parsers. The value the page reads is unchanged.
    """
    return (
        json_text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_page(
    template: str,
    *,
    name: str | None,
    canvas_id: str,
    authed: bool,
    scene_svg: str | None,
    elements_json: str | None,
    nonce: str = "",
) -> str:
    """Fill the page template's %%SLOT%% markers in a single pass.

    One pass means a value is never scanned for further markers: a scene,
    element text or canvas name that contains "%%ELEMENTS_JSON%%" stays text.
    An unknown marker in the template is a bug and raises KeyError.
    """
    values = {
        "TITLE": page_title(name, authed),
        "CANVAS_ID": canvas_id,
        "AUTHED": "true" if authed else "false",
        "NONCE": nonce,
        "ASSET_REVISION": hashlib.sha256((Path(__file__).parent / "assets" / "BUILD.json").read_bytes()).hexdigest()[:16],
        "SCENE_VERSION_JSON": json.dumps(scene_version_of(scene_svg)) if authed else "null",
        "SCENE_SVG_JSON": script_json(json.dumps(scene_svg)) if authed else "null",
        "ELEMENTS_JSON": (
            script_json(elements_json) if (authed and elements_json) else "null"
        ),
    }
    return _SLOT.sub(lambda match: values[match.group(1)], template)


ASSETS = Path(__file__).parent / "assets"


def page_csp(nonce: str) -> str:
    """The canvas page's Content-Security-Policy for one response."""
    return "; ".join((
        "default-src 'none'",
        f"script-src 'nonce-{nonce}' 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "worker-src 'self' blob:",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "object-src 'none'",
    ))


_ROUTE_CANVAS = re.compile(r"^/canvas/([0-9a-f]{32})/?$")
_ROUTE_SESSION = re.compile(r"^/canvas/([0-9a-f]{32})/session/?$")
_ROUTE_VERSION = re.compile(r"^/canvas/([0-9a-f]{32})/version/?$")
_ROUTE_ELEMENTS = re.compile(r"^/canvas/([0-9a-f]{32})/elements/?$")

COOKIE_NAME = "canvas_token"

# The query-param name IS the warning:
# a URL gets pasted into Slack, screenshotted, bookmarked, shared by
# accident. A bland "?token=..." hides what's happening; this doesn't.
# Reuses the exact phrase already established everywhere else this
# feature is discussed (the server flag's help text), rather than inventing
# another slightly-different name.
UNSAFE_URL_AUTH_PARAM = "this-is-unsafe-dont-do-this-auth"

# Largest request body accepted (POST .../session, .../elements),
# checked against Content-Length before a byte of the body is read. storage.py
# has no size limit of its own; the largest legitimate body is a browser's
# elements save (freehand strokes keep every point: some hundred KB for a
# busy drawing; a placed SVG with embedded raster images can reach a few
# MB). 8 MiB is that plus
# headroom, and small enough that a stranger cannot make the shared web/MCP
# process buffer arbitrary amounts before any token is checked.
MAX_BODY_BYTES = 8 * 1024 * 1024
_CONTENT_LENGTH = re.compile(r"[0-9]{1,20}")


_QUERY = re.compile(r"\?\S*")  # to the next whitespace, quotes included


def without_query(line: str) -> str:
    """A request line for the log without its query string: an old query link (before
    0.2.13) still carries a canvas token there, and logs are kept, copied and read."""
    return _QUERY.sub("?…", line)
