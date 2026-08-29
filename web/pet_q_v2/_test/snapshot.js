const { chromium } = require('C:/Users/A/.workbuddy/binaries/node/workspace/node_modules/playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 900, height: 900 } });
  await page.goto('http://127.0.0.1:8090/pet_q_v2/qchibi.html');
  await page.waitForTimeout(1500);
  await page.screenshot({ path: 'F:/C/pet_video/server_idle.png' });
  await browser.close();
  console.log('snapshot done');
})();