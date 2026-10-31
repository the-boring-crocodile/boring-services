import { execFileSync } from 'node:child_process';

// The test server's store, the way MCP's tools reach it (canvas_store.py; run.sh sets the env).
const store = (args, input) => execFileSync(process.env.CANVAS_PY,
  [process.env.CANVAS_STORE, process.env.CANVAS_STATE, ...args], { input, encoding: 'utf8' });
export const pushScene = (id, svg) => { store(['push', id], svg); };
export const savedElements = (id) => JSON.parse(store(['elements', id]));

// The supported fragment exchange, in a fresh browser context, before any editor actions.
export async function openCanvas(page, base, id, token) {
  const loaded = page.waitForEvent('console', {
    predicate: message => message.text().includes('root.render() called'), timeout: 60000,
  });
  await page.goto(`${base}/canvas/${id}#this-is-unsafe-dont-do-this-auth=${encodeURIComponent(token)}`);
  await page.waitForFunction(() => window.__AUTHED__ === true && !location.hash);
  await page.locator('[data-testid="toolbar-text"]').waitFor({ timeout: 30000 });
  await loaded;
}
