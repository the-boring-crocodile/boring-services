// Mermaid to Excalidraw with a math label: the bundled Mermaid loads the bundled
// KaTeX (its own chunk), renders under the page's CSP, and Insert saves the result.
import { firefox } from 'playwright';
import { openCanvas, savedElements } from './open_canvas.mjs';
const base = process.argv[2];
const { canvas_id: id, token } = JSON.parse(process.env.CANVAS);  // made by run.sh (canvas_store.py)
const browser = await firefox.launch();
const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
const errors = [];
page.on('console', (m) => { if (m.type() === 'error' || /Content-Security-Policy|CSP/i.test(m.text())) errors.push(m.text().slice(0, 300)); });
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message.slice(0, 300)));
const katex = [];
page.on('response', (r) => { if (/\/katex-[A-Z0-9]+\.js$/.test(r.url())) katex.push(r.status()); });
await page.addInitScript(() => document.addEventListener('securitypolicyviolation', (e) => { (window.__csp = window.__csp || []).push(`${e.violatedDirective} ${e.blockedURI}`); }));
await openCanvas(page, base, id, token); await page.waitForTimeout(1500);
await page.locator('[data-testid="dropdown-menu-button"]').click();
await page.getByText('Mermaid to Excalidraw').click();
await page.locator('.ttd-dialog textarea').fill('flowchart LR\n  A["$$x^2 + \\frac{1}{2}$$"] --> B[plain]');
await page.waitForTimeout(4000);
await page.getByRole('button', { name: 'Insert' }).click();
await page.waitForTimeout(3000);  // past the 1 s save debounce
const saved = savedElements(id);
const elements = Array.isArray(saved) ? saved : saved.elements || [];
const result = { katex_chunk: katex, elements: elements.length, types: [...new Set(elements.map(e => e.type))].sort(),
                 csp: await page.evaluate(() => window.__csp || []), errors };
console.log(JSON.stringify(result, null, 1));
await browser.close();
if (!katex.includes(200) || elements.length < 3 || result.csp.length || errors.length) throw Error('Mermaid/KaTeX acceptance failed');
