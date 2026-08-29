const { chromium } = require('C:/Users/A/.workbuddy/binaries/node/workspace/node_modules/playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 720, height: 720 } });
  await page.goto('http://127.0.0.1:8090/pet_q_v2/qchibi.html');
  await page.waitForTimeout(2000);

  const poses = ['idle', 'walk', 'run', 'jump', 'lift', 'sit', 'climb', 'lie', 'wave', 'nod', 'bounce', 'magic', 'attack'];
  for (const p of poses) {
    await page.click(`button[data-pose="${p}"]`);
    await page.waitForTimeout(150);
    await page.screenshot({ path: `F:/C/pet_video/v15_${p}.png` });
  }

  await browser.close();
  console.log('done');
})();