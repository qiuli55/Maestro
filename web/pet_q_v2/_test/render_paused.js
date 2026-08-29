const { chromium } = require('C:/Users/A/.workbuddy/binaries/node/workspace/node_modules/playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 720, height: 720 } });
  await page.goto('http://127.0.0.1:8090/pet_q_v2/qchibi.html');
  await page.waitForTimeout(2000);

  // 暂停动画
  await page.evaluate(() => { window.paused = true; });

  // 用 page.evaluate 来手动设置 pose 和 t0
  await page.evaluate(() => {
    // 通过 click 切换到 nod
    document.querySelector('button[data-pose="nod"]').click();
  });

  await page.waitForTimeout(100);
  await page.screenshot({ path: 'F:/C/pet_video/v15_nod_t0.png' });
  await page.waitForTimeout(125);
  await page.screenshot({ path: 'F:/C/pet_video/v15_nod_t1.png' });
  await page.waitForTimeout(125);
  await page.screenshot({ path: 'F:/C/pet_video/v15_nod_t2.png' });

  await browser.close();
  console.log('done');
})();