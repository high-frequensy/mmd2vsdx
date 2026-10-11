// Render a Mermaid file to SVG or PNG in Chrome: a small stand-in for `mmdc` (mermaid-cli) that needs only
// puppeteer-core and the few files tools/fetch_assets.py puts in vendor/. It follows mermaid-cli's renderMermaid() step
// for step (same page set-up, fonts, Mermaid call, font embedding, screenshot), so the output is identical.
// Usage: node render.cjs -i diagram.mmd -o out.svg|out.png -c mermaid-config.json -p puppeteer.json [-b white] [-s scale] [-q]
const fs = require("fs");
const http = require("http");
const path = require("path");

const HERE = __dirname;
const MODULES = process.env.MMDC_MODULES || path.join(HERE, "node_modules");
const VENDOR = path.join(HERE, "vendor", "node_modules");
const puppeteer = require(path.join(MODULES, "puppeteer-core"));

const arg = (flag, dflt) => { const i = process.argv.indexOf(flag); return i < 0 ? dflt : process.argv[i + 1]; };
const [input, output, configFile, pptrFile] = ["-i", "-o", "-c", "-p"].map((f) => arg(f));
const background = arg("-b", "white"), scale = Number(arg("-s", "1"));
if (!input || !output) { console.error("usage: node render.cjs -i in.mmd -o out.svg|png [-c config] [-p puppeteer.json] [-b color] [-s scale]"); process.exit(2); }
const format = path.extname(output).slice(1);
if (!["svg", "png"].includes(format)) { console.error("output must be .svg or .png"); process.exit(2); }

const CSS = [   // loaded into the page like mermaid-cli does: icon fonts, Mermaid's default fonts, KaTeX
  "@fortawesome/fontawesome-free/css/brands.css", "@fortawesome/fontawesome-free/css/regular.css",
  "@fortawesome/fontawesome-free/css/solid.css", "@fortawesome/fontawesome-free/css/fontawesome.css",
  "@fontsource-variable/recursive/index.css", "@fontsource/open-sans/400.css", "@fontsource/open-sans/400-italic.css",
  "katex/dist/katex.css"];
const TYPES = { ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
                ".html": "text/html" };

// serve vendor/ on localhost so the page, its scripts and its fonts share one origin
const server = http.createServer((req, res) => {
  const p = path.normalize(path.join(VENDOR, decodeURIComponent(new URL(req.url, "http://x").pathname)));
  if (p.startsWith(VENDOR + path.sep) && fs.existsSync(p) && fs.statSync(p).isFile()) {
    res.writeHead(200, { "content-type": TYPES[path.extname(p)] || "text/plain", "access-control-allow-origin": "*" });
    return res.end(fs.readFileSync(p));
  }
  if (req.url === "/") {   // charset matters: without it Chrome picks different fallback fonts (so different text widths) for symbols like ☃
    res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    return res.end("<!doctype html>\n<html>\n  <body>\n    <div id=\"container\"></div>\n  </body>\n</html>\n");
  }
  res.writeHead(404); res.end();
});

(async () => {
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${server.address().port}`;
  const definition = fs.readFileSync(input, "utf8");
  const mermaidConfig = configFile ? JSON.parse(fs.readFileSync(configFile, "utf8")) : {};
  const launch = Object.assign({ headless: "shell" }, pptrFile ? JSON.parse(fs.readFileSync(pptrFile, "utf8")) : {});
  if (!launch.executablePath) throw new Error("no browser: set CHROME_PATH and re-run ./setup.sh (puppeteer.json needs an executablePath)");
  const browser = await puppeteer.launch(launch);
  try {
    const page = await browser.newPage();
    page.on("console", (msg) => { const t = msg.text();   // 404s are fontEmbedder looking for LICENSE files next to a font; a missing real file fails the page load
      if (!t.startsWith("Failed to load resource")) console.warn(t); });
    await page.setViewport({ width: 0, height: 0, deviceScaleFactor: scale });
    await page.goto(base + "/");
    await page.$eval("body", (body, bg) => { body.style.background = bg; }, background);
    await Promise.all(CSS.map((href) => page.evaluate(({ href }) => {
      const link = document.createElement("link");
      link.setAttribute("crossorigin", "anonymous");
      link.setAttribute("rel", "stylesheet");
      link.setAttribute("href", href);
      link.setAttribute("class", "mermaid-cli-css");
      const loaded = new Promise((resolve, reject) => { link.onload = () => resolve(); link.onerror = () => reject(new Error(`Failed to load stylesheet: ${href}`)); });
      document.head.appendChild(link);
      return loaded;
    }, { href: `${base}/${href}` })));
    await page.addScriptTag({ url: `${base}/mermaid/dist/mermaid.min.js` });
    await page.$eval("#container", async (container, { definition, mermaidConfig, background }) => {
      await Promise.all(Array.from(document.fonts, (font) => font.load()));
      mermaid.initialize({ startOnLoad: false, ...mermaidConfig });
      const { svg: svgText } = await mermaid.render("my-svg", definition, container);   // throws on an invalid diagram
      container.innerHTML = svgText;
      container.getElementsByTagName("svg")[0].style.backgroundColor = background;
    }, { definition, mermaidConfig, background });

    if (format === "svg") {
      const xml = await page.$eval("svg", async (svg, { url }) => {
        const { fontEmbedder } = await import(url);   // embeds the web fonts the diagram uses, so the SVG stands alone
        await fontEmbedder({ document, svg });
        return new XMLSerializer().serializeToString(svg);   // valid XML for foreignObject HTML (<br> -> <br/>)
      }, { url: `${base}/@mermaid-js/mermaid-cli/src/browser/fontEmbedder.js` });
      fs.writeFileSync(output, new TextEncoder().encode(xml));
    } else {
      const clip = await page.$eval("svg", (svg) => {
        const r = svg.getBoundingClientRect();
        return { x: Math.floor(r.left), y: Math.floor(r.top), width: Math.ceil(r.width), height: Math.ceil(r.height) };
      });
      await page.setViewport({ width: clip.x + clip.width, height: clip.y + clip.height, deviceScaleFactor: scale });
      fs.writeFileSync(output, await page.screenshot({ clip }));
    }
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e.message || e); process.exit(1); });
