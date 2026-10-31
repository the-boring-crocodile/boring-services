// Real editor/save/reload/export with all off-origin requests refused.
import { createRequire } from 'node:module';
import { openCanvas, savedElements } from './open_canvas.mjs';
const require = createRequire(import.meta.url);
const { firefox } = require(process.env.PLAYWRIGHT_MODULE);
const base = process.argv[2];
const { canvas_id: id, token } = JSON.parse(process.env.CANVAS);  // {canvas_id, token}: see canvas_store.py; no web route creates a canvas
const browser = await firefox.launch();
try {
  const context = await browser.newContext({ viewport: { width: 1200, height: 800 } });
  const outside = [];
  await context.route('**/*', route => {
    const origin = new URL(route.request().url()).origin;
    if (origin === new URL(base).origin) return route.continue();
    outside.push(origin);
    return route.abort();
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.name));
  await page.addInitScript(() => document.addEventListener('securitypolicyviolation', event => {
    window.__csp = window.__csp || {};
    const key = event.violatedDirective + ':' + (event.blockedURI.startsWith('http') ? new URL(event.blockedURI).origin : event.blockedURI);
    window.__csp[key] = (window.__csp[key] || 0) + 1;
  }));
  async function open(reload = false) {
    if (!reload) return openCanvas(page, base, id, token);
    const ready = page.waitForEvent('console', { predicate: message =>
      message.text().includes('root.render() called'), timeout: 60000 });
    await page.reload();
    await ready;
    await page.locator('[data-testid="toolbar-text"]').waitFor({ timeout: 30000 });
  }
  await open();
  await page.locator('[data-testid="toolbar-text"]').click({ force: true });
  await page.mouse.click(500, 300);
  await page.keyboard.type('Local assets acceptance');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(1800);
  const saved = JSON.stringify(savedElements(id));
  if (!saved.includes('Local assets acceptance')) throw Error('drawing did not persist');
  await open(true);
  await page.waitForTimeout(2000);
  await page.locator('[data-testid="main-menu-trigger"]').click();
  await page.locator('[data-testid="image-export-button"]').click();
  await page.waitForTimeout(2000);
  console.log(JSON.stringify({dialogs: await page.locator('.Modal').count(), preview: await page.locator('[class*=preview]').evaluateAll(nodes => nodes.map(n => n.className)), errors, csp: await page.evaluate(() => window.__csp || 0)}));
  await page.locator('.ImageExportModal__preview canvas, .ExportDialog__preview canvas')
    .first().waitFor({ timeout: 30000 });
  if (outside.length || errors.length || await page.evaluate(() => Object.keys(window.__csp || {}).length))
    throw Error('offline/CSP acceptance failed: external=' + outside.length + ', errors=' + errors.length);
  console.log('PASS: local editor, text/font, save/reload and export; zero external requests/CSP errors');
} finally { await browser.close(); }
