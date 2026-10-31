// A hand-drawn element over the pushed drawing stays above it after a push
// and reload (2026-10-02: the page used to re-insert the drawing last, i.e.
// on top, burying strokes drawn on it). Throwaway server, never the live one.
import { firefox } from 'playwright';
import { openCanvas, pushScene } from './open_canvas.mjs';
const base = process.argv[2];
const { canvas_id: id, token } = JSON.parse(process.env.CANVAS);  // made by run.sh (canvas_store.py): no web route creates a canvas
const push = (fill) => pushScene(id,
  `<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><rect width="400" height="300" fill="${fill}"/></svg>`);
await push('#ffffff');
const browser = await firefox.launch();
const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
const rendered = () => page.waitForEvent('console', { predicate: (m) => m.text().includes('root.render() called'), timeout: 60000 });
let ready;
await openCanvas(page, base, id, token); await page.waitForTimeout(2500);
// Draw in the middle of the view, where the drawing landed.
await page.locator('[data-testid="toolbar-rectangle"]').click({ force: true });
await page.mouse.move(560, 380); await page.mouse.down(); await page.mouse.move(640, 440, { steps: 6 }); await page.mouse.up();
await page.waitForTimeout(2000);
ready = rendered();
await push('#fffff0');  // a new drawing: the page reloads
await ready; await page.waitForTimeout(2500);
const order = await page.evaluate(() => (window.__ELEMENTS__ || []).filter((e) => !e.isDeleted).map((e) => e.type));
ready = rendered();
await page.reload(); await ready; await page.waitForTimeout(2500);
const orderAfterReload = await page.evaluate(() => (window.__ELEMENTS__ || []).filter((e) => !e.isDeleted).map((e) => e.type));
const onTop = (o) => o.indexOf('rectangle') > o.indexOf('image') && o.includes('image');
console.log(JSON.stringify({ order, orderAfterReload, stroke_on_top: onTop(order) && onTop(orderAfterReload) }, null, 1));
await browser.close();
if (!onTop(order) || !onTop(orderAfterReload)) throw Error('strokes were covered by the pushed drawing');
