// Exercise the actual inline save owner with delayed HTTP replies, without a CDN.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const template = readFileSync(fileURLToPath(new URL('../../src/boring_canvas/templates/canvas.html', import.meta.url)), 'utf8');
const start = template.indexOf('      let saveTimer = null;');
const end = template.indexOf('      // Leaving the page', start);
assert(start >= 0 && end > start);
function fixture() {
  const requests = [];
  const context = {
    window: { __CANVAS_ID__: 'synthetic' },
    clearTimeout() {}, setTimeout() { return 1; },
    log() {}, setProblem() {}, reasonOf: async () => 'synthetic failure',
    fetch: async (_url, request) => new Promise(resolve => {
      requests.push({ elements: JSON.parse(request.body), respond: ok => resolve({ ok }) });
    }),
  };
  const api = vm.runInNewContext(template.slice(start, end) + '\n({flushSave, scheduleSave})', context);
  return { api, requests };
}
const tick = () => new Promise(resolve => setImmediate(resolve));

// An edit made while the flush is saving must prevent reload.
{
  const { api, requests } = fixture();
  api.scheduleSave([{ id: 'first' }]);
  const flush = api.flushSave();
  await tick();
  api.scheduleSave([{ id: 'newer' }]);
  requests[0].respond(true);
  assert.equal(await flush, false, 'an older save must not authorize reload over a newer edit');
  const latest = api.flushSave();
  await tick();
  assert.deepEqual(requests[1].elements, [{ id: 'newer' }]);
  requests[1].respond(true);
  assert.equal(await latest, true);
}

// Failure of an earlier queued save must not resurrect it after a newer save.
{
  const { api, requests } = fixture();
  api.scheduleSave([{ id: 'old' }]);
  const first = api.flushSave();
  await tick();
  api.scheduleSave([{ id: 'new' }]);
  const second = api.flushSave();
  requests[0].respond(false);
  assert.equal(await first, false);
  await tick();
  assert.deepEqual(requests[1].elements, [{ id: 'new' }]);
  requests[1].respond(true);
  assert.equal(await second, true);
  const empty = api.flushSave();
  await tick();
  assert.equal(requests.length, 2, 'failed older state must never be sent after newer success');
  assert.equal(await empty, true);
}
console.log('PASS: delayed saves retain newer edits; failed older state is never replayed.');
