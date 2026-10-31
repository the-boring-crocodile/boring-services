# Notices the npm packages do not ship

`build.mjs` copies every bundled package's own licence file into
`assets/licenses/`. Some bundled packages ship none, and Excalidraw's fonts
come without their licences. Their notices live here, taken from upstream;
`build.mjs` refuses to build when a bundled package or a font
family has neither.

`packages.json` maps a package name (exact, or a prefix ending in `/` or `-`)
to a file in `packages/`:

| File | Source |
|---|---|
| excalidraw.txt | github.com/excalidraw/excalidraw, LICENSE (MIT) |
| radix-ui.txt | github.com/radix-ui/primitives, LICENSE (MIT) |
| micromark.txt | github.com/micromark/micromark, license (MIT) |
| dagre-d3-es.txt | github.com/tbo47/dagre-es, LICENSE.md (MIT) |
| react-remove-scroll-bar.txt | github.com/theKashey/react-remove-scroll-bar, LICENSE (MIT) |
| fastdom.txt | github.com/wilsonpage/fastdom, README.md "License" (MIT; the package ships no licence file) |

`fonts/` holds one notice per font family the build ships from Excalidraw's
`dist/prod/fonts/`. Liberation Sans 1.05 (GPL-2.0 with the Liberation font
exception) is left out on purpose (`build.mjs`):

| Family | Licence | Source |
|---|---|---|
| Assistant | OFL-1.1 | github.com/google/fonts, ofl/assistant/OFL.txt |
| Cascadia | OFL-1.1 | github.com/microsoft/cascadia-code, LICENSE |
| ComicShanns | MIT | github.com/shannpersand/comic-shanns, LICENSE (also in the font) |
| Excalifont | OFL-1.1 | stated on plus.excalidraw.com/excalifont; the font itself only says "All rights reserved" |
| Lilita | OFL-1.1 | github.com/google/fonts, ofl/lilitaone/OFL.txt |
| Nunito | OFL-1.1 | github.com/google/fonts, ofl/nunito/OFL.txt |
| Virgil | OFL-1.1 | github.com/excalidraw/virgil, LICENSE.md |
| Xiaolai | OFL-1.1 | github.com/lxgw/kose-font, OFL.txt |

The copyright lines were compared with the fonts' own name tables. When
Excalidraw is updated, check these again: a new font family or a newly bundled
package without a licence file stops the build.
