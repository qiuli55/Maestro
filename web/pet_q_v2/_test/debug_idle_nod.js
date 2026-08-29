const { chromium } = require('C:/Users/A/.workbuddy/binaries/node/workspace/node_modules/playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 720, height: 720 } });

  // 测试 1: idle, headOffY=0 (默认)
  await page.goto('http://127.0.0.1:8090/pet_q_v2/qchibi.html');
  await page.waitForTimeout(2000);
  await page.screenshot({ path: 'F:/C/pet_video/idem_idle.png' });

  // 测试 2: nod, headOffY=0 (强制)
  await page.click('button[data-pose="nod"]');
  await page.waitForTimeout(225);
  await page.screenshot({ path: 'F:/C/pet_video/idem_nod_real.png' });

  // 测试 3: 切回 idle, 但保持 headOffY 不为 0
  // 不行，因为切换 idle 会让 headOffY = 0

  await browser.close();
})();