// 测试截图脚本：截 idle + walk 多帧
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 720, height: 720 } });
  await page.goto('http://127.0.0.1:8090/pet_q_v2/_test/test_qchibi.html');
  await page.waitForTimeout(3000);

  // 查看每层加载状态
  const states = await page.evaluate(() => {
    return Object.entries(window.__imgs || {}).map(([k, v]) => ({
      name: k, complete: v.complete, nw: v.naturalWidth, nh: v.naturalHeight
    }));
  });
  console.log('img states:');
  states.forEach(s => console.log('  ' + s.name + ': ' + s.nw + 'x' + s.nh + ' complete=' + s.complete));

  // 截 idle
  await page.screenshot({ path: 'F:/C/pet_video/test_idle.png' });

  // 切到 walk
  await page.click('button[data-pose="walk"]');
  await page.waitForTimeout(150);
  await page.screenshot({ path: 'F:/C/pet_video/test_walk_a.png' });
  await page.waitForTimeout(150);
  await page.screenshot({ path: 'F:/C/pet_video/test_walk_b.png' });
  await page.waitForTimeout(150);
  await page.screenshot({ path: 'F:/C/pet_video/test_walk_c.png' });

  await browser.close();
})();