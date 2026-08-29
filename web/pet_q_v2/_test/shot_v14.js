// 测试截图脚本：截 idle + walk + lift 多帧
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 720, height: 720 } });
  await page.goto('http://127.0.0.1:8090/pet_q_v2/qchibi.html');
  await page.waitForTimeout(2000);

  // 查看每层加载状态
  const states = await page.evaluate(() => {
    return Object.entries(window.__imgs || {}).map(([k, v]) => ({
      name: k, complete: v.complete, nw: v.naturalWidth, nh: v.naturalHeight
    }));
  });
  console.log('img states:');
  states.forEach(s => console.log('  ' + s.name + ': ' + s.nw + 'x' + s.nh + ' complete=' + s.complete));

  // 截 idle
  await page.screenshot({ path: 'F:/C/pet_video/v14_idle.png' });

  // 切到 walk
  await page.click('button[data-pose="walk"]');
  await page.waitForTimeout(150);
  await page.screenshot({ path: 'F:/C/pet_video/v14_walk.png' });
  await page.waitForTimeout(150);
  await page.screenshot({ path: 'F:/C/pet_video/v14_walk_b.png' });

  // lift
  await page.click('button[data-pose="lift"]');
  await page.waitForTimeout(200);
  await page.screenshot({ path: 'F:/C/pet_video/v14_lift.png' });

  // wave
  await page.click('button[data-pose="wave"]');
  await page.waitForTimeout(200);
  await page.screenshot({ path: 'F:/C/pet_video/v14_wave.png' });

  // magic
  await page.click('button[data-pose="magic"]');
  await page.waitForTimeout(200);
  await page.screenshot({ path: 'F:/C/pet_video/v14_magic.png' });

  await browser.close();
  console.log('done');
})();