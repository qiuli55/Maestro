const { chromium } = require('C:/Users/A/.workbuddy/binaries/node/workspace/node_modules/playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 720, height: 720 } });
  await page.goto('http://127.0.0.1:8090/pet_q_v2/qchibi.html?debug');
  await page.waitForTimeout(2000);

  await page.click('button[data-pose="nod"]');
  await page.waitForTimeout(225);

  // 在浏览器中执行 hook，捕获所有 drawLayer 调用
  const result = await page.evaluate(() => {
    // 替换 drawLayer 来记录参数
    const origDrawLayer = window.drawLayer;
    const calls = [];
    window.drawLayer = function(layer, rx, ry, rot) {
      if (layer.name === 'headwear' || layer.name === 'face' ||
          layer.name === 'ears' || layer.name === 'front hair') {
        calls.push({ name: layer.name, rx, ry, rot });
      }
      return origDrawLayer.call(this, layer, rx, ry, rot);
    };

    // 触发一次渲染
    return new Promise(resolve => {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          resolve(calls);
        });
      });
    });
  });

  console.log('drawLayer calls:');
  result.forEach(c => console.log(`  ${c.name}: rx=${c.rx.toFixed(2)}, ry=${c.ry.toFixed(2)}, rot=${c.rot.toFixed(2)}`));

  await browser.close();
})();