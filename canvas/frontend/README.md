# Packaged Canvas editor

Run `npm ci --ignore-scripts --no-audit`, then `node build.mjs` here.
The checked-in lock pins Excalidraw 0.18.1, React/ReactDOM 19.0.0 and
esbuild 0.25.5. `overrides` in package.json raise nanoid and lodash-es past
Excalidraw's and Mermaid's exact pins to versions without the known advisories
(2026-10-30); drop an override once upstream requires a fixed version itself.
Generated assets ship in the Python wheel. Browsers require
no CDN or package-registry access. BUILD.json inventories their hashes;
DEPENDENCIES.json records locked package provenance and which packages are
bundled; licenses/ contains the installed package notices, and each font
directory gets its LICENSE.txt. Notices that upstream does not ship are kept in
[notices/](notices/README.md); the build stops when a bundled package or a font
family has none.

The bundle-time local-font-fallback transform removes one verified upstream
fallback expression. Excalidraw otherwise adds esm.sh URLs to every FontFace
even with a configured local asset directory. The transform refuses a changed
upstream expression. Upstream installed files remain unmodified.

Local isolated Firefox evidence: editor, text/font, save/reload and image-export
preview succeeded with off-origin requests refused and zero CSP violations.
Test: tests/browser/run.sh (offline_assets.mjs among the checks), which sets
the fixture runtime and makes each check's canvas with
tests/browser/canvas_store.py in the throwaway server's state directory (no
web route creates a canvas). Never point it at production. Installed-wheel and deployment acceptance are separate.
