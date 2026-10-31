// E1 + CSP browser check for boring-canvas (local server, never the live one).
// usage: node e1_csp.mjs <base-url>
import { firefox } from 'playwright';
import { openCanvas, pushScene } from './open_canvas.mjs';
const base = process.argv[2];  // a throwaway canvas server, never the live one
const out = { csp_violations: [], console_errors: [], steps: [] };
const step = (s) => { out.steps.push(s); };

const created = JSON.parse(process.env.CANVAS);  // made by run.sh (canvas_store.py): no web route creates a canvas
const { canvas_id: id, token } = created;
const browser = await firefox.launch();
const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
page.on('console', (m) => { if (m.type() === 'error') out.console_errors.push(m.text().slice(0, 300)); });
await page.addInitScript(() => {
  document.addEventListener('securitypolicyviolation', (e) => {
    window.__csp = window.__csp || [];
    window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`);
  });
});
await openCanvas(page, base, id, token);
await page.waitForTimeout(2500);  // let the first save and the version poll settle
step('excalidraw rendered');
out.csp_header = (await (await fetch(`${base}/canvas/${id}`, { headers: { Cookie: `canvas_token=${token}` } })).headers.get('content-security-policy')) || null;

// Draw a rectangle, then push a drawing at once (inside the 1 s save debounce).
await page.locator('[data-testid="toolbar-rectangle"]').click({ force: true });
await page.mouse.move(400, 300); await page.mouse.down();
await page.mouse.move(600, 450, { steps: 8 }); await page.mouse.up();
out.live_types = await page.evaluate(() => document.querySelectorAll('canvas').length);
step('rectangle drawn');
if (process.argv[3] === 'nopush') {
  await page.waitForTimeout(2500);
  const r = await page.reload(); await page.waitForTimeout(1500);
  out.elements_after_plain_reload = await page.evaluate(() => (window.__ELEMENTS__ || []).filter((e) => !e.isDeleted).map((e) => e.type));
  await browser.close(); console.log(JSON.stringify(out, null, 1)); process.exit(0);
}
const reloaded = page.waitForEvent('load', { timeout: 20000 });
pushScene(id, '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60"><rect width="120" height="60" fill="#cde"/></svg>');
step('pushed scene (within the debounce)');
await reloaded;
await page.waitForTimeout(2000);
out.elements_after_reload = await page.evaluate(() => (window.__ELEMENTS__ || []).filter((e) => !e.isDeleted).map((e) => e.type));
out.rectangle_survived = out.elements_after_reload.includes('rectangle');
out.csp_violations = await page.evaluate(() => window.__csp || []);
await page.screenshot({ path: `shot-${Date.now()}.png` });
await browser.close();
console.log(JSON.stringify(out, null, 1));
if (!out.rectangle_survived || out.csp_violations.length) throw Error('push/save/CSP acceptance failed');
