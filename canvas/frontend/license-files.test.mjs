import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readdir, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { licenseWriter } from './license-files.mjs';

test('scopes and versions survive; identical nested copies share a file', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'canvas-licenses-'));
  try {
    const write = licenseWriter(directory);
    const content = Buffer.from('copyright and permission\n');
    const a = await write('@radix-ui/react-slot', '1.2.3', 'LICENSE', content);
    const b = await write('@radix-ui/react-slot', '1.2.3', 'LICENSE', content);
    assert.equal(a, 'licenses/@radix-ui__react-slot@1.2.3__LICENSE');
    assert.equal(a, b);
    assert.equal((await readdir(directory)).length, 1);
    assert.deepEqual(await readFile(join(directory, a.slice('licenses/'.length))), content);
  } finally { await rm(directory, { recursive: true }); }
});

test('different notice bytes are kept with a short stable distinguishing suffix', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'canvas-licenses-'));
  try {
    const write = licenseWriter(directory);
    const a = await write('example', '1.0.0', 'LICENSE', Buffer.from('first'));
    const b = await write('example', '1.0.0', 'LICENSE', Buffer.from('second'));
    const c = await write('example', '1.0.0', 'LICENSE', Buffer.from('second'));
    assert.notEqual(a, b);
    assert.equal(b, c);
    assert.match(b, /__LICENSE__[a-f0-9]{12}$/);
    assert.equal((await readdir(directory)).length, 2);
    assert.equal((await readFile(join(directory, a.slice(9)))).toString(), 'first');
    assert.equal((await readFile(join(directory, b.slice(9)))).toString(), 'second');
    assert.ok(b.slice(9).length <= 100);
  } finally { await rm(directory, { recursive: true }); }
});
