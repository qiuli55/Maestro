const { chromium } = require('C:/Users/A/.workbuddy/binaries/node/workspace/node_modules/playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 720, height: 720 } });

  // 收集 console.log
  const logs = [];
  page.on('console', msg => logs.push(msg.text()));

  await page.goto('http://127.0.0.1:8090/pet_q_v2/qchibi.html');
  await page.waitForTimeout(2000);

  await page.click('button[data-pose="nod"]');
  await page.waitForTimeout(225);

  console.log('console logs:');
  logs.forEach(l => console.log('  ' + l));

  await browser.close();
})();