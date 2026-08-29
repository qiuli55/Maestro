const { chromium } = require('C:/Users/A/.workbuddy/binaries/node/workspace/node_modules/playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 900, height: 900 } });
  await page.goto('http://127.0.0.1:8090/pet_q_v2/qchibi.html');
  await page.waitForTimeout(2000);

  // 检查所有图层加载状态
  const layerInfo = await page.evaluate(() => {
    const out = [];
    for (const [name, img] of Object.entries(window.__imgs || {})) {
      out.push({ name, complete: img.complete, nw: img.naturalWidth, nh: img.naturalHeight });
    }
    return out;
  });

  console.log('图层加载状态:');
  layerInfo.forEach(l => console.log(`  ${l.name}: ${l.complete ? '✓' : '✗'} ${l.nw}x${l.nh}`));

  await page.screenshot({ path: 'F:/C/pet_video/user_proxy.png' });

  // 看看 canvas 上半部分中心区域的实际像素颜色
  const pixelInfo = await page.evaluate(() => {
    const cv = document.getElementById('cv');
    const ctx = cv.getContext('2d');
    // 帽子应该在 canvas (276, 105) 附近 — 实际位置可能不同
    // 取几个采样点
    const samples = [
      { name: 'canvas_corner', x: 50, y: 50 },
      { name: 'hat_top', x: 280, y: 80 },
      { name: 'hat_center', x: 280, y: 130 },
      { name: 'hat_lower', x: 280, y: 160 },
      { name: 'face_eye', x: 320, y: 320 },
      { name: 'face_nose', x: 320, y: 350 },
      { name: 'body_top', x: 320, y: 400 },
      { name: 'background', x: 100, y: 400 }
    ];
    const out = [];
    for (const s of samples) {
      const p = ctx.getImageData(s.x, s.y, 1, 1).data;
      out.push({ name: s.name, x: s.x, y: s.y, rgba: `${p[0]},${p[1]},${p[2]},${p[3]}` });
    }
    return out;
  });

  console.log('\nCanvas 采样点:');
  pixelInfo.forEach(p => console.log(`  ${p.name} (${p.x},${p.y}): rgba(${p.rgba})`));

  await browser.close();
})();