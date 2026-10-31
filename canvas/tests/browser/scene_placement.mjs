// Current SVG identity must survive Excalidraw save, replacement and reopening.
import { firefox } from 'playwright';
import { openCanvas, pushScene, savedElements } from './open_canvas.mjs';
const base = process.argv[2];
const { canvas_id: id, token } = JSON.parse(process.env.CANVAS);
const pushedId = 'boring-canvas-pushed-scene';
const push = size => pushScene(id,
  `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 1000 1000"><rect width="100" height="100"/></svg>`);
const browser = await firefox.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
  const read = async () => savedElements(id);
  async function placed(size, annotation = false) {
    const version = await page.evaluate(() => window.__SCENE_VERSION__);
    for (let attempt = 0; attempt < 100; attempt++) {
      const response = await read();
      const elements = response.elements || [];
      const image = elements.find(e => e.id === pushedId && !e.isDeleted);
      const mark = elements.find(e => e.type === 'rectangle' && !e.isDeleted);
      if (image?.width === size && image?.customData?.boringCanvasSceneVersion === version
          && (!annotation || mark)) return { version, annotation: mark?.id };
      await page.waitForTimeout(100);
    }
    throw Error('current placement identity was not saved by the editor');
  }
  await push(100);
  await openCanvas(page, base, id, token);
  const first = await placed(100);
  await page.locator('[data-testid="toolbar-rectangle"]').click({ force: true });
  await page.mouse.move(300, 250); await page.mouse.down();
  await page.mouse.move(350, 300, { steps: 6 }); await page.mouse.up();
  const marked = await placed(100, true);
  const rendered = () => page.waitForEvent('console', {
    predicate: message => message.text().includes('root.render() called'), timeout: 60000,
  });
  let ready = rendered();
  await push(200);
  await ready;
  const replaced = await placed(200, true);
  if (first.version === replaced.version || marked.annotation !== replaced.annotation)
    throw Error('replacement lost scene identity or annotation');
  ready = rendered();
  await page.reload();
  await ready;
  const reopened = await placed(200, true);
  if (reopened.version !== replaced.version || reopened.annotation !== marked.annotation)
    throw Error('reopening lost placement identity or annotation');
  console.log('PASS: placement version round-trips through editor save, changed SVG and reopening; annotation retained');
} finally { await browser.close(); }
