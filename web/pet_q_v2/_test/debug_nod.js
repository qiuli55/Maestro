const { chromium } = require('C:/Users/A/.workbuddy/binaries/node/workspace/node_modules/playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 720, height: 720 } });
  await page.goto('http://127.0.0.1:8090/pet_q_v2/qchibi.html');
  await page.waitForTimeout(2000);

  // 切到 nod
  await page.click('button[data-pose="nod"]');
  await page.waitForTimeout(225);

  // 在浏览器中查看 LAYERS 实际计算的 rx, ry
  const result = await page.evaluate(() => {
    // 模拟 loop 中的计算
    const pose = 'nod';
    const poseDef = POSES[pose];
    const t = (performance.now() - t0) / 1000;
    let headOffY = Math.abs(Math.sin(t * Math.PI * 2 * poseDef.speed)) * 4;
    let headRot = Math.sin(t * Math.PI * 2 * poseDef.speed) * 8;
    let frontHairOff = Math.sin(t * Math.PI * 2 * poseDef.speed) * 2;
    return {
      t: t,
      headOffY: headOffY,
      headRot: headRot,
      frontHairOff: frontHairOff,
      hatOffset: headOffY * 5,
      faceName: 'face',
      headwearName: 'headwear'
    };
  });
  console.log('nod 状态下 headOffY 等参数:');
  console.log(JSON.stringify(result, null, 2));

  await browser.close();
})();