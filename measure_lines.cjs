// Ask Chrome how it actually laid out a Mermaid SVG, so Visio can reproduce it exactly.
// Usage: node measure_lines.cjs diagram.svg puppeteer.json [diagram.mmd]  -> JSON on stdout:
// {labels: {key: {lines: [[[text, style bits], ...], ...], hard: [bool per line], color, size, lineHeight, font, bg, align, box: [x, y, w, h]}},
//  paints: [{fill, stroke, sw, dash, fillOpacity, strokeOpacity, filter}], edges: {data-id: [start, end]},
//  images: [{key, box, png (base64)}]}   images = node pictures and icons, captured alone at 4x
// edges come from Mermaid's own parser (given the .mmd): an edge with an explicit id (e1@-->) has no endpoints in its data-id.
// label key = node/cluster element id, edge data-id, or "__title__"; boxes are in SVG root user units.
// Handles both HTML labels (foreignObject) and plain SVG <text> labels; paints cover every drawable in document order.
const fs = require("fs");
const path = require("path");
const puppeteer = require(path.join(process.env.MMDC_MODULES, "puppeteer-core"));

// label parts captured as pictures: Font Awesome icons (glyphs from an icon font) and KaTeX formulas
const PIC_CLASSES = ["fa", "fab", "fak", "fal", "far", "fas", "katex"];

(async () => {
  const svg = fs.readFileSync(process.argv[2], "utf8");
  const browser = await puppeteer.launch(JSON.parse(fs.readFileSync(process.argv[3], "utf8")));
  const page = await browser.newPage();
  const html = `<!doctype html><html><body style="margin:0">${svg}</body></html>`;
  // "load" waits for <image> hrefs too; a dead image host must not hang the conversion
  const show = (p) => p.setContent(html, { waitUntil: "load", timeout: 20000 }).catch(() => {});
  await show(page);
  await page.evaluate(() => document.fonts.ready);
  const out = await page.evaluate((PIC_CLASSES) => {
    const root = document.querySelector("svg");
    const vb = root.viewBox.baseVal, rr = root.getBoundingClientRect();
    const toUser = (r) => [vb.x + (r.left - rr.left) * vb.width / rr.width, vb.y + (r.top - rr.top) * vb.height / rr.height,
                           r.width * vb.width / rr.width, r.height * vb.height / rr.height];
    // a run's style: "bits|color|size px|font family"; bits: 1 bold, 2 italic, 4 underline (Visio Character.Style),
    // 16 strikethrough (Character.Strikethru). paint = "color" for HTML text, "fill" for SVG text.
    const styleOf = (el, stop, paint = "color") => {
      const cs = getComputedStyle(el), w = cs.fontWeight;
      let st = (w === "bold" || parseInt(w) >= 600 ? 1 : 0) | (/italic|oblique/.test(cs.fontStyle) ? 2 : 0);
      for (let e = el; e && e !== stop; e = e.parentElement) {   // text-decoration is not inherited: walk up
        const d = getComputedStyle(e).textDecorationLine;
        st |= (d.includes("underline") ? 4 : 0) | (d.includes("line-through") ? 16 : 0);
      }
      return [st, cs[paint], parseFloat(cs.fontSize), cs.fontFamily].join("|");
    };
    const edgeIds = [...document.querySelectorAll("path[data-id]")].map((p) => p.getAttribute("data-id"));
    const keyOf = (el) => {
      if (el.matches && el.matches("text.flowchartTitleText")) return "__title__";
      const edge = el.closest("g[data-id]:not(.cluster)");
      if (edge) return edge.getAttribute("data-id");
      const lab = el.closest("g.edgeLabel[id]");   // swimlane layout: id="edge-label-<src>-<dst>-<edge data-id>"
      if (lab) { const eid = edgeIds.find((e) => lab.id.endsWith("-" + e)); if (eid) return eid; }
      const holder = el.closest("g.node, g.cluster, g.icon-shape, g.image-shape, g.rough-node");
      return holder ? holder.id : null;
    };
    const tidy = (lines) => lines
      .map((l) => { if (l.length) { l[0][0] = l[0][0].replace(/^\s+/, ""); l[l.length - 1][0] = l[l.length - 1][0].replace(/\s+$/, ""); }
                    return l.filter((r) => r[0]); })
      .filter((l) => l.length);
    const push = (lines, ch, bold) => {
      const line = lines[lines.length - 1];
      if (line.length && line[line.length - 1][1] === bold) line[line.length - 1][0] += ch;
      else line.push([ch, bold]);
    };
    const bgOf = (el, stop) => {
      let e = el, bg = "rgba(0, 0, 0, 0)";
      while (e && e !== stop && bg === "rgba(0, 0, 0, 0)") { bg = getComputedStyle(e).backgroundColor; e = e.parentElement; }
      return bg;
    };
    const res = {};

    // HTML labels: group characters into lines by their rendered top. Pictures inside a label (<img>, inline <svg>,
    // Font Awesome <i>) are captured separately; an inline one leaves U+FFFC in its line, with its box in pics[].
    const ctx = document.createElement("canvas").getContext("2d");
    const nbspWidth = (cs) => { ctx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`; return ctx.measureText("\u00a0").width; };
    const isPic = (el) => /^(img|svg)$/i.test(el.tagName) || PIC_CLASSES.some((c) => el.classList.contains(c));
    for (const fo of document.querySelectorAll("foreignObject")) {
      const key = keyOf(fo);
      if (!key) continue;
      const textEl = fo.querySelector("p") || fo.querySelector("span") || fo;
      const cs = getComputedStyle(textEl), lh = parseFloat(cs.lineHeight);
      const lines = [], hard = [], pics = [], mids = [];   // hard[i]: line i starts the label or follows an explicit <br>
      // rotated labels (swimlane titles): compare character positions in the label's own frame
      const M = fo.getScreenCTM(), angle = Math.atan2(M.b, M.a) * 180 / Math.PI, rotated = Math.abs(angle) > 0.5;
      const localTop = (r) => rotated ? new DOMPoint(r.left + r.width / 2, r.top + r.height / 2).matrixTransform(M.inverse()).y : r.top;
      let lastTop = null, lastMid = null, band = null;   // band: vertical extent of the last picture
      let span = null;   // vertical extent of the current line's glyphs (mixed font sizes share a baseline, not a top)
      const walker = document.createTreeWalker(fo, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
      const range = document.createRange();
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        if (n.nodeType === 1) {
          if (n.tagName.toLowerCase() === "br") lastTop = "br";
          else if (isPic(n)) {
            const r = n.getBoundingClientRect();
            if (r.width && r.height) {
              pics.push({ box: toUser(r), inline: true });
              const mid = r.top + r.height / 2;
              if (lastTop === null || lastTop === "br" || (lastTop !== "pic" && Math.abs(mid - lastMid) > lh * 0.6)) {
                lines.push([]); hard.push(lastTop === null || lastTop === "br"); mids.push(mid);
              }
              lastTop = "pic"; lastMid = mid; band = [r.top, r.bottom];
              push(lines, "\uFFFC", styleOf(n.parentElement, fo));
            }
            let last = n;   // skip the picture's insides (an inline svg's <title> etc. are not label text)
            while (last.lastChild) last = last.lastChild;
            walker.currentNode = last;
          }
          continue;
        }
        const bold = styleOf(n.parentElement, fo);
        for (let i = 0; i < n.data.length; i++) {
          range.setStart(n, i); range.setEnd(n, i + 1);
          const r = range.getClientRects()[0];
          if (!r) continue;
          const top = Math.round(localTop(r)), mid = r.top + r.height / 2;
          const newLine = lastTop === null || lastTop === "br" ||
            (lastTop === "pic" ? mid < band[0] || mid > band[1]
              : rotated ? Math.abs(top - lastTop) > 3 : r.top >= span[1] - 1 || r.bottom <= span[0] + 1);
          if (newLine) {
            lines.push([]); hard.push(lastTop === null || lastTop === "br"); mids.push(r.top + r.height / 2);
            span = [r.top, r.bottom];
          }
          span = span ? [Math.min(span[0], r.top), Math.max(span[1], r.bottom)] : [r.top, r.bottom];
          lastTop = top; lastMid = r.top + r.height / 2;
          push(lines, n.data[i], bold);
        }
      }
      // a line holding only pictures: they sit on their own line (an <img> above the text, a lone formula), so
      // they are not part of the text
      let pi = 0;
      for (const l of lines) {
        const t = l.map((r) => r[0]).join("");
        const n = (t.match(/\uFFFC/g) || []).length;
        if (n && !t.replace(/[\uFFFC\s]/g, "")) { for (let k = 0; k < n; k++) pics[pi + k].inline = false; l.length = 0; }
        pi += n;
      }
      const keep = lines.map((l) => l.some((r) => r[0].trim()));   // tidy() drops blank lines; keep hard[] aligned
      let box = toUser(fo.getBoundingClientRect());
      const m = mids.filter((_, i) => keep[i]);
      if (pics.some((p) => !p.inline) && m.length) {   // a picture on its own line: text block = the text lines only
        const mid = (m[0] + m[m.length - 1]) / 2, h = m.length * lh;
        const fr = fo.getBoundingClientRect();
        box = toUser({ left: fr.left, width: fr.width, top: mid - h / 2, height: h });
      }
      res[key] = { lines: tidy(lines), hard: hard.filter((_, i) => keep[i]), color: cs.color, size: parseFloat(cs.fontSize), lineHeight: lh,
                   font: cs.fontFamily, bg: bgOf(textEl, fo), align: cs.textAlign, box, pics: pics.filter((p) => p.inline).map((p) => p.box),
                   nbsp: pics.length ? nbspWidth(cs) : 0 };
      if (rotated) {   // unrotated size of the label (user units) and its angle; box stays the on-screen bbox
        const sc = Math.hypot(M.a, M.b) * vb.width / rr.width;
        Object.assign(res[key], { angle, local: [fo.width.baseVal.value * sc, fo.height.baseVal.value * sc] });
      }
    }

    // plain SVG text labels (htmlLabels: false) and the diagram title: lines by glyph baseline
    for (const t of document.querySelectorAll("svg text")) {
      if (t.closest("foreignObject")) continue;
      const key = keyOf(t);
      if (!key || res[key]) continue;
      const lines = [];
      let lastY = null, idx = 0;
      const walker = document.createTreeWalker(t, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        const bold = styleOf(n.parentElement, t, "fill");
        for (let i = 0; i < n.data.length; i++, idx++) {
          let y;
          try { y = t.getStartPositionOfChar(idx).y; } catch (e) { continue; }
          if (lastY === null || Math.abs(y - lastY) > 2) lines.push([]);
          lastY = y;
          push(lines, n.data[i], bold);
        }
      }
      const cs = getComputedStyle(t);
      const bgRect = t.parentElement && t.parentElement.querySelector("rect.background");
      const bg = bgRect ? getComputedStyle(bgRect).fill : "none";
      const box = toUser(t.getBoundingClientRect());
      const n = Math.max(lines.length, 1);
      // SVG-text labels: Mermaid pre-wraps into rows, explicit breaks are indistinguishable -> keep all
      res[key] = { lines: tidy(lines), hard: lines.map(() => true), color: cs.fill, size: parseFloat(cs.fontSize),
                   lineHeight: n > 1 ? box[3] / n : box[3], font: cs.fontFamily,
                   bg: bg === "none" ? "rgba(0, 0, 0, 0)" : bg,
                   align: { middle: "center", end: "right" }[cs.textAnchor] || "left", box };
    }

    const paints = [...document.querySelectorAll("svg rect, svg polygon, svg path, svg circle, svg ellipse, svg line, svg polyline")]
      .map((el) => { const c = getComputedStyle(el);
        return { fill: c.fill, stroke: c.stroke, sw: parseFloat(c.strokeWidth), dash: c.strokeDasharray, fillOpacity: c.fillOpacity, strokeOpacity: c.strokeOpacity, filter: c.filter }; });
    return { labels: res, paints };
  }, PIC_CLASSES);
  // node pictures (img@{...}) and icons: screenshot each with everything else hidden
  const sel = "g.node image, g.image-shape image, g.icon-shape svg, g.node svg, g.rough-node image, g.rough-node svg, " +
              ["img", "svg", ...PIC_CLASSES.map((c) => "." + c)].map((t) => "foreignObject " + t).join(", ");
  out.images = [];
  if ((await page.$$(sel)).length) {
    const cap = await browser.newPage();   // separate page so the 4x scale cannot touch the text measurements above
    await cap.setViewport({ width: 800, height: 600, deviceScaleFactor: 4 });
    await show(cap);
    const targets = await cap.$$(sel);
    for (const t of targets) {
      const info = await t.evaluate((el, nested) => {
        const root = document.querySelector("svg"), vb = root.viewBox.baseVal, rr = root.getBoundingClientRect();
        if (el.parentElement && el.parentElement.closest(nested)) return null;   // inside another picture
        const r = el.getBoundingClientRect();
        const holder = el.closest("g[data-id]") || el.closest("g.node, g.icon-shape, g.image-shape, g.rough-node");
        if (!r.width || !r.height || !holder) return null;
        root.style.visibility = "hidden";
        el.style.visibility = "visible";
        return { key: holder.getAttribute("data-id") || holder.id, clip: { x: r.left, y: r.top, width: r.width, height: r.height },
                 box: [vb.x + (r.left - rr.left) * vb.width / rr.width, vb.y + (r.top - rr.top) * vb.height / rr.height,
                       r.width * vb.width / rr.width, r.height * vb.height / rr.height] };
      }, ["svg", "img", ...PIC_CLASSES.map((c) => "." + c)].map((t) => "foreignObject " + t).join(", "));
      if (!info) continue;
      const png = await cap.screenshot({ clip: info.clip, omitBackground: true, encoding: "base64", captureBeyondViewport: true });
      await t.evaluate((el) => { document.querySelector("svg").style.visibility = ""; el.style.visibility = ""; });
      out.images.push({ key: info.key, box: info.box, png });
    }
  }
  if (process.argv[4]) {
    await page.addScriptTag({ path: path.join(__dirname, "vendor", "node_modules", "mermaid", "dist", "mermaid.min.js") });
    out.edges = await page.evaluate(async (src) => {
      mermaid.initialize({ startOnLoad: false });
      await mermaid.parse(src);   // registers the diagram types
      const d = await mermaid.mermaidAPI.getDiagramFromText(src);
      return d.db.getEdges ? Object.fromEntries(d.db.getEdges().map((e) => [e.id, [e.start, e.end]])) : {};
    }, fs.readFileSync(process.argv[4], "utf8"));
  }
  await browser.close();
  process.stdout.write(JSON.stringify(out));
})();
