// Rasterise an SVG with Chrome at 2x. Usage: node shot.cjs in.svg out.png puppeteer.json
const fs = require("fs");
const path = require("path");
const puppeteer = require(path.join(process.env.MMDC_MODULES, "puppeteer-core"));

(async () => {
  const svg = fs.readFileSync(process.argv[2], "utf8");
  const [w, h] = svg.match(/width="([\d.]+)" height="([\d.]+)"/).slice(1).map(Number);
  const browser = await puppeteer.launch(JSON.parse(fs.readFileSync(process.argv[4], "utf8")));
  const page = await browser.newPage();
  await page.setViewport({ width: Math.ceil(w), height: Math.ceil(h), deviceScaleFactor: 2 });
  await page.setContent(`<!doctype html><html><body style="margin:0">${svg}</body></html>`);
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: process.argv[3], clip: { x: 0, y: 0, width: w, height: h } });
  await browser.close();
})();
