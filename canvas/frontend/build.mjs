// Locked dependencies become wheel assets; browsers never contact npm/CDNs.
import { build } from 'esbuild';
import { mkdir, readFile, writeFile, readdir, stat, rm } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { dirname, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
const root = dirname(fileURLToPath(import.meta.url));
const out = resolve(root, '../src/boring_canvas/assets');
await rm(out, { recursive: true, force: true });
await mkdir(out, { recursive: true });
const result = await build({
  absWorkingDir: root, metafile: true, entryPoints: ['editor.js'], outdir: out,
  bundle: true, splitting: true, format: 'esm', platform: 'browser',
  minify: true, legalComments: 'linked', conditions: ['production'],
  define: { 'process.env.NODE_ENV': '"production"' },
  loader: { '.woff': 'file', '.woff2': 'file', '.ttf': 'file', '.svg': 'file' },
  plugins: [{
    name: 'local-font-fallback',
    setup(builder) {
      // 0.18.0 always adds its CDN font URL even when local assets are configured.
      // Keep the locked upstream bytes intact; remove that one fallback at bundle time.
      builder.onLoad({ filter: /excalidraw[/\\]dist[/\\]prod[/\\].*\.js$/ }, async args => {
        let contents = await readFile(args.path, 'utf8');
        if (contents.includes('ASSETS_FALLBACK_URL')) {
          const span = 'return r.push(new URL(n,jn.ASSETS_FALLBACK_URL)),r';
          if (contents.split(span).length !== 2) throw Error('locked font fallback changed');
          contents = contents.replace(span, 'return r');
        }
        return { contents, loader: 'js' };
      });
    }
  }]
});
async function plainCopy(source, destination) {
  if ((await stat(source)).isDirectory()) {
    await mkdir(destination, { recursive: true });
    for (const item of await readdir(source))
      await plainCopy(resolve(source, item), resolve(destination, item));
  } else await writeFile(destination, await readFile(source));
}
await plainCopy(resolve(root, 'node_modules/@excalidraw/excalidraw/dist/prod/fonts'),
         resolve(out, 'fonts'));
// Not shipped: Liberation Sans 1.05 (GPL-2.0 with a font exception, which asks
// for its source alongside). Excalidraw registers it as font 9 for server-side
// rendering; the picker does not offer it. Such text is drawn in the browser's
// default font.
await rm(resolve(out, 'fonts', 'Liberation'), { recursive: true });
// Packages whose code is in the bundle: the deepest node_modules entry of each input.
const bundled = new Set();
for (const input of Object.keys(result.metafile.inputs)) {
  const at = input.lastIndexOf('node_modules/');
  if (at < 0) continue;
  const rest = input.slice(at + 'node_modules/'.length).split('/');
  bundled.add(input.slice(0, at) + 'node_modules/' + rest.slice(0, rest[0].startsWith('@') ? 2 : 1).join('/'));
}
const lock = JSON.parse(await readFile(resolve(root, 'package-lock.json'), 'utf8'));
const licenses = Object.entries(lock.packages).filter(([name]) => name).map(([name, item]) =>
  ({ package: name, version: item.version, license: item.license || 'see package notice',
     bundled: bundled.has(name), integrity: item.integrity }));
// Notices for bundled packages that ship no licence file (notices/README.md).
const noticeDir = resolve(root, 'notices');
const extra = JSON.parse(await readFile(resolve(noticeDir, 'packages.json'), 'utf8'));
function extraNotice(name) {
  const bare = name.slice(name.lastIndexOf('node_modules/') + 'node_modules/'.length);
  const key = Object.keys(extra).find(k => k === bare || (/[/-]$/.test(k) && bare.startsWith(k)));
  return key && extra[key];
}
await mkdir(resolve(out, 'licenses'), { recursive: true });
const unnoticed = [];
for (const name of Object.keys(lock.packages).filter(Boolean)) {
  const directory = resolve(root, name);
  let notices;
  try { notices = await readdir(directory); } catch { continue; } // uninstalled optional platform package
  notices = notices.filter(n => /^(license|licence|copying|notice)([.\-_]|$)/i.test(n));
  for (const notice of notices) {
    const filename = name.replaceAll('/', '__') + '__' + notice;
    await plainCopy(resolve(directory, notice), resolve(out, 'licenses', filename));
  }
  if (notices.length || !bundled.has(name)) continue;
  const file = extraNotice(name);
  if (!file) { unnoticed.push(name); continue; }
  await plainCopy(resolve(noticeDir, 'packages', file),
                  resolve(out, 'licenses', name.replaceAll('/', '__') + '__LICENSE'));
}
if (unnoticed.length) throw Error('bundled without a licence notice: ' + unnoticed.join(', '));
// Every font family carries its licence next to it.
const families = (await readdir(resolve(out, 'fonts'))).sort();
const fontNotices = (await readdir(resolve(noticeDir, 'fonts'))).map(n => n.replace(/\.txt$/, '')).sort();
if (families.join() !== fontNotices.join())
  throw Error(`font notices ${fontNotices} do not match font families ${families}`);
for (const family of families)
  await plainCopy(resolve(noticeDir, 'fonts', family + '.txt'), resolve(out, 'fonts', family, 'LICENSE.txt'));
await writeFile(resolve(out, 'DEPENDENCIES.json'), JSON.stringify(licenses, null, 2) + '\n');
const manifest = {};
async function inventory(directory) {
  for (const item of (await readdir(directory, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name, 'en'))) {
    const name = resolve(directory, item.name);
    if (item.isDirectory()) await inventory(name);
    else if (item.name !== 'BUILD.json')
      manifest[relative(out, name)] = createHash('sha256').update(await readFile(name)).digest('hex');
  }
}
await inventory(out);
await writeFile(resolve(out, 'BUILD.json'),
  JSON.stringify({ tool: 'esbuild 0.25.5', transform: 'Excalidraw 0.18.0: omit remote font fallback; retain configured local URLs', artifacts: manifest }, null, 2) + '\n');
