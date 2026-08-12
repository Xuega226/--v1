import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { Presentation, PresentationFile } from "@oai/artifact-tool";

const [, , inputPath, outputDir, layoutDir] = process.argv;
if (!inputPath || !outputDir || !layoutDir) {
  throw new Error("usage: node generate_deck.mjs <input.json> <output-dir> <layout-dir>");
}

const spec = JSON.parse(await fs.readFile(inputPath, "utf8"));
const presentation = Presentation.create({ slideSize: { width: 1280, height: 720 } });
const modules = new Map();
const sourceLedger = [];
let imageCount = 0;
let chartCount = 0;

const THEMES = {
  codex_grid: {
    name: "Codex Grid",
    canvas: "#FFFFFF",
    ink: "#000000",
    muted: "#62666D",
    panel: "#EDEDED",
    rule: "#B8BCC4",
    accent: "#3D8DFF",
    accent2: "#6DCBF4",
    series: ["#3D8DFF", "#6DCBF4", "#A9DFF7", "#6E7785"],
    font: "Helvetica Neue",
  },
  unnameko_green: {
    name: "未名子柔绿",
    canvas: "#F5FAF6",
    ink: "#25352B",
    muted: "#607166",
    panel: "#E4F0E7",
    rule: "#AEC7B5",
    accent: "#5E9270",
    accent2: "#A9CFB3",
    series: ["#5E9270", "#8FBC9B", "#BDD8C4", "#D5A968"],
    font: "Microsoft YaHei",
  },
  night_code: {
    name: "夜色代码",
    canvas: "#111827",
    ink: "#F8FAFC",
    muted: "#A8B3C4",
    panel: "#1F2937",
    rule: "#475569",
    accent: "#63E6BE",
    accent2: "#60A5FA",
    series: ["#63E6BE", "#60A5FA", "#FBBF24", "#F472B6"],
    font: "Microsoft YaHei",
  },
};

const brandKey = Object.hasOwn(THEMES, spec.brand_template) ? spec.brand_template : "codex_grid";
const theme = THEMES[brandKey];
const layoutStrategy = ["auto_grid", "text_brief", "report_flow"].includes(spec.layout_strategy)
  ? spec.layout_strategy
  : (["auto_grid", "text_brief", "report_flow"].includes(spec.template) ? spec.template : "auto_grid");

async function loadLayout(number) {
  if (!modules.has(number)) {
    const file = path.join(layoutDir, `slide-${String(number).padStart(2, "0")}.mjs`);
    modules.set(number, await import(pathToFileURL(file).href));
  }
  return modules.get(number);
}

function clean(value, limit = 180) {
  return String(value ?? "").replace(/\s+/g, " ").trim().slice(0, limit);
}

function bulletsOf(slide) {
  const raw = Array.isArray(slide?.bullets) ? slide.bullets : [];
  return raw.map((item) => clean(item, 150)).filter(Boolean).slice(0, 6);
}

function bodyPair(bullets, start) {
  return {
    titleHere: bullets[start] || "要点",
    loremIpsumDolorSitAmetConsecteturAdipiscing: bullets[start + 1] || "",
  };
}

function addText(slide, text, position, options = {}) {
  const shape = slide.shapes.add({
    geometry: "textbox",
    name: options.name,
    position,
    fill: options.fill || "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  shape.text = text;
  shape.text.style = {
    fontSize: options.fontSize ?? 24,
    bold: Boolean(options.bold),
    color: options.color || theme.ink,
    alignment: options.alignment || "left",
    verticalAlignment: options.verticalAlignment || "top",
    typeface: options.typeface || theme.font,
  };
  return shape;
}

function addPageChrome(slide, title, index) {
  slide.background.fill = theme.canvas;
  addText(slide, clean(title || `第 ${index} 页`, 55),
    { left: 54, top: 36, width: 1110, height: 72 },
    { fontSize: 46, bold: true, name: "slide-title" });
  slide.shapes.add({
    geometry: "rect",
    position: { left: 54, top: 125, width: 1172, height: 2 },
    fill: theme.rule,
    line: { style: "solid", fill: "none", width: 0 },
  });
  addText(slide, String(index), { left: 1175, top: 665, width: 50, height: 22 },
    { fontSize: 13, color: theme.muted, alignment: "right", name: "page-number" });
}

function addNotes(slide, entries) {
  const sources = entries.filter((entry) => entry?.url);
  if (!sources.length) return;
  const lines = ["[Sources]"];
  for (const entry of sources) {
    const detail = [entry.label, entry.artist, entry.license, entry.retrieved_at]
      .map((item) => clean(item, 180)).filter(Boolean).join("; ");
    lines.push(`- ${entry.url}${detail ? ` — ${detail}` : ""}`);
    sourceLedger.push({ url: entry.url, detail });
  }
  lines.push("[/Sources]");
  slide.speakerNotes.textFrame.setText(lines.join("\n"));
  slide.speakerNotes.setVisible(true);
}

async function addCodexCover() {
  const module = await loadLayout(1);
  return module.buildSlide01(presentation, {
    title: clean(spec.kicker || "PRESENTATION", 40),
    title2: clean(spec.title || "未命名演示", 54),
    title3: clean(spec.subtitle || spec.purpose || "", 100),
  });
}

function addBrandCover() {
  const slide = presentation.slides.add();
  slide.background.fill = theme.canvas;
  slide.shapes.add({
    geometry: "rect",
    position: { left: 0, top: 0, width: 20, height: 720 },
    fill: theme.accent,
    line: { style: "solid", fill: "none", width: 0 },
  });
  addText(slide, clean(spec.kicker || theme.name.toUpperCase(), 40),
    { left: 64, top: 52, width: 600, height: 34 },
    { fontSize: 18, bold: true, color: theme.accent });
  addText(slide, clean(spec.title || "未命名演示", 48),
    { left: 64, top: 224, width: 1080, height: 180 },
    { fontSize: 72, bold: true, verticalAlignment: "bottom", name: "deck-title" });
  addText(slide, clean(spec.subtitle || spec.purpose || "", 100),
    { left: 66, top: 454, width: 820, height: 90 },
    { fontSize: 30, color: theme.muted });
  return slide;
}

async function addCover() {
  return brandKey === "codex_grid" ? addCodexCover() : addBrandCover();
}

async function addCodexClosing(index) {
  const module = await loadLayout(26);
  return module.buildSlide26(presentation, {
    title: clean(spec.kicker || "NEXT STEP", 32),
    title2: clean(spec.closing || "下一步：确认重点并开始行动", 36),
    title3: {
      loremIpsumDetails: clean(spec.author || "未名子", 36),
      loremIpsumDetails2: clean(spec.audience || "给主人", 36),
      loremIpsumDetails3: clean(spec.date || "", 36),
    },
    footer1: String(index),
  });
}

function addBrandClosing(index) {
  const slide = presentation.slides.add();
  slide.background.fill = theme.canvas;
  slide.shapes.add({
    geometry: "rect",
    position: { left: 64, top: 112, width: 170, height: 8 },
    fill: theme.accent,
    line: { style: "solid", fill: "none", width: 0 },
  });
  addText(slide, clean(spec.closing || "下一步：确认重点并开始行动", 46),
    { left: 64, top: 212, width: 1040, height: 180 },
    { fontSize: 64, bold: true, verticalAlignment: "bottom" });
  addText(slide, `${clean(spec.author || "未名子", 36)}  ·  ${clean(spec.date || "", 36)}`,
    { left: 66, top: 485, width: 600, height: 44 },
    { fontSize: 22, color: theme.muted });
  addText(slide, String(index), { left: 1175, top: 665, width: 50, height: 22 },
    { fontSize: 13, color: theme.muted, alignment: "right" });
  return slide;
}

async function addClosing(index) {
  return brandKey === "codex_grid" ? addCodexClosing(index) : addBrandClosing(index);
}

async function addCodexTextSlide(slideSpec, index, bullets) {
  const requested = clean(slideSpec.layout, 24);
  if (requested === "timeline" || (layoutStrategy === "report_flow" && bullets.length === 3)) {
    const module = await loadLayout(17);
    const points = [...bullets, "", "", ""].slice(0, 3);
    return module.buildSlide17(presentation, {
      title: clean(slideSpec.title || "时间线", 55), footer1: String(index),
      label1: "01", label2: "02", label3: "03",
      body1: { titleHere: points[0], loremIpsumDolorSitAmetConsecteturAdipiscing: "" },
      body2: { titleHere: points[1], loremIpsumDolorSitAmetConsecteturAdipiscing: "" },
      body3: { titleHere: points[2], loremIpsumDolorSitAmetConsecteturAdipiscing: "" },
    });
  }
  if (layoutStrategy === "text_brief" || requested === "dense") {
    const module = await loadLayout(4);
    const left = bullets.slice(0, 3);
    const right = bullets.slice(3);
    return module.buildSlide04(presentation, {
      title: clean(slideSpec.title || `第 ${index} 页`, 55), footer1: String(index),
      body1: {
        titleHere: clean(slideSpec.left_title || "核心内容", 34),
        loremIpsumDolorSitAmetConsecteturAdipiscing: left[0] || "",
        loremIpsumDolorSitAmetConsecteturAdipiscing2: left[1] || "",
        loremIpsumDolorSitAmetConsecteturAdipiscing3: left[2] || "",
      },
      body2: {
        loremIpsumDolorSitAmetConsecteturAdipiscing: right[0] || "",
        loremIpsumDolorSitAmetConsecteturAdipiscing2: right[1] || "",
        loremIpsumDolorSitAmetConsecteturAdipiscing3: right[2] || "",
      },
    });
  }
  if (requested === "three_column" || bullets.length >= 5) {
    const module = await loadLayout(6);
    return module.buildSlide06(presentation, {
      title: clean(slideSpec.title || `第 ${index} 页`, 55), footer1: String(index),
      body1: bodyPair(bullets, 0), body2: bodyPair(bullets, 2), body3: bodyPair(bullets, 4),
    });
  }
  const module = await loadLayout(5);
  const split = Math.max(1, Math.ceil(bullets.length / 2));
  const left = bullets.slice(0, split);
  const right = bullets.slice(split);
  return module.buildSlide05(presentation, {
    title: clean(slideSpec.title || `第 ${index} 页`, 55), footer1: String(index),
    body1: { titleHere: "重点", loremIpsumDolorSitAmetConsecteturAdipiscing: left.join("\n\n") },
    body2: { titleHere: "说明", loremIpsumDolorSitAmetConsecteturAdipiscing: (right.length ? right : left).join("\n\n") },
  });
}

function addBrandTimeline(slideSpec, index, bullets) {
  const slide = presentation.slides.add();
  addPageChrome(slide, slideSpec.title, index);
  const points = [...bullets, "", ""].slice(0, 3);
  slide.shapes.add({
    geometry: "rect",
    position: { left: 80, top: 342, width: 1080, height: 2 },
    fill: theme.rule,
    line: { style: "solid", fill: "none", width: 0 },
  });
  points.forEach((point, offset) => {
    const left = 86 + offset * 400;
    slide.shapes.add({
      geometry: "ellipse",
      position: { left, top: 330, width: 26, height: 26 },
      fill: offset === 0 ? theme.accent : theme.accent2,
      line: { style: "solid", fill: theme.canvas, width: 2 },
    });
    addText(slide, `0${offset + 1}`, { left, top: 274, width: 70, height: 30 },
      { fontSize: 18, bold: true, color: theme.muted });
    addText(slide, point, { left, top: 390, width: 310, height: 126 },
      { fontSize: 29, bold: true });
  });
  return slide;
}

function addBrandTextSlide(slideSpec, index, bullets) {
  if (slideSpec.layout === "timeline" || (layoutStrategy === "report_flow" && bullets.length === 3)) {
    return addBrandTimeline(slideSpec, index, bullets);
  }
  const slide = presentation.slides.add();
  addPageChrome(slide, slideSpec.title, index);
  const split = layoutStrategy === "text_brief" ? Math.ceil(bullets.length / 2) : Math.min(3, Math.ceil(bullets.length / 2));
  const columns = [bullets.slice(0, split), bullets.slice(split)];
  columns.forEach((items, column) => {
    if (!items.length) return;
    const left = 62 + column * 600;
    slide.shapes.add({
      geometry: "rect",
      position: { left, top: 184, width: 82, height: 7 },
      fill: column === 0 ? theme.accent : theme.accent2,
      line: { style: "solid", fill: "none", width: 0 },
    });
    addText(slide, items.map((item) => `• ${item}`).join("\n\n"),
      { left, top: 218, width: 520, height: 365 },
      { fontSize: 27, color: theme.ink, name: `body-column-${column + 1}` });
  });
  return slide;
}

async function addImageSlide(slideSpec, index, bullets) {
  const imageAsset = slideSpec.image;
  const imagePath = String(imageAsset?.path || "");
  if (!imagePath) return brandKey === "codex_grid"
    ? addCodexTextSlide(slideSpec, index, bullets)
    : addBrandTextSlide(slideSpec, index, bullets);
  const slide = presentation.slides.add();
  addPageChrome(slide, slideSpec.title, index);
  addText(slide, bullets.map((item) => `• ${item}`).join("\n\n"),
    { left: 58, top: 184, width: 420, height: 388 },
    { fontSize: 25, color: theme.ink, name: "image-slide-copy" });
  const bytes = await fs.readFile(imagePath);
  const blob = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  slide.images.add({
    blob,
    contentType: clean(imageAsset.mime || "image/jpeg", 40),
    alt: clean(imageAsset.alt || slideSpec.image_query || slideSpec.title, 180),
    fit: "cover",
    geometry: "rect",
    position: { left: 520, top: 166, width: 706, height: 438 },
  });
  const credit = [imageAsset.artist, imageAsset.license].map((item) => clean(item, 80)).filter(Boolean).join(" · ");
  if (credit) addText(slide, credit, { left: 520, top: 613, width: 706, height: 22 },
    { fontSize: 11, color: theme.muted, alignment: "right", name: "image-credit" });
  addNotes(slide, [{
    url: imageAsset.source_url || imageAsset.image_url,
    label: imageAsset.title || imageAsset.query,
    artist: imageAsset.artist,
    license: imageAsset.license,
    retrieved_at: imageAsset.retrieved_at,
  }]);
  imageCount += 1;
  return slide;
}

function normalizeChartSeries(chart) {
  return chart.series.map((series, index) => {
    const color = theme.series[index % theme.series.length];
    const base = { name: clean(series.name || `系列 ${index + 1}`, 60), values: series.values.map(Number) };
    if (chart.type === "line") return { ...base, line: { style: "solid", fill: color, width: 3 }, marker: { symbol: "circle", size: 7 } };
    if (["pie", "doughnut"].includes(chart.type)) {
      return {
        ...base,
        fill: color,
        points: series.values.map((_, pointIndex) => ({
          idx: pointIndex,
          fill: theme.series[pointIndex % theme.series.length],
          line: { style: "solid", fill: theme.canvas, width: 1 },
        })),
      };
    }
    return { ...base, fill: color };
  });
}

function addChartSlide(slideSpec, index, bullets) {
  const chart = slideSpec.chart;
  const slide = presentation.slides.add();
  addPageChrome(slide, slideSpec.title, index);
  const copyWidth = bullets.length ? 300 : 0;
  if (bullets.length) {
    addText(slide, bullets.slice(0, 4).map((item) => `• ${item}`).join("\n\n"),
      { left: 58, top: 190, width: copyWidth, height: 355 },
      { fontSize: 22, color: theme.ink, name: "chart-interpretation" });
  }
  const chartLeft = bullets.length ? 390 : 70;
  const chartWidth = bullets.length ? 820 : 1140;
  const isPie = ["pie", "doughnut"].includes(chart.type);
  slide.charts.add(chart.type, {
    position: { left: chartLeft, top: 160, width: chartWidth, height: 455 },
    title: clean(chart.title || "", 100),
    titlePlacement: chart.title ? "aboveChart" : "none",
    titleTextStyle: { fontSize: 20, bold: true, fill: theme.ink },
    categories: chart.categories.map((item) => clean(item, 50)),
    series: normalizeChartSeries(chart),
    hasLegend: chart.series.length > 1 || isPie,
    legend: { position: isPie ? "right" : "bottom", overlay: false, textStyle: { fill: theme.muted, fontSize: 14 } },
    barOptions: chart.type === "bar" ? { direction: "column", grouping: "clustered", gapWidth: 55 } : undefined,
    lineOptions: chart.type === "line" ? { smooth: false, grouping: "standard" } : undefined,
    doughnutOptions: chart.type === "doughnut" ? { holeSize: 58 } : undefined,
    dataLabels: isPie
      ? { showPercent: true, showCategoryName: true, position: "outEnd", textStyle: { fill: theme.ink, fontSize: 13 } }
      : { showValue: true, position: "outEnd", textStyle: { fill: theme.ink, fontSize: 13, bold: true } },
    xAxis: isPie ? undefined : { textStyle: { fill: theme.muted, fontSize: 13 }, line: { style: "solid", fill: theme.rule, width: 1 } },
    yAxis: isPie ? undefined : {
      numberFormatCode: chart.number_format || undefined,
      textStyle: { fill: theme.muted, fontSize: 13 },
      majorGridlines: { style: "solid", fill: theme.rule, width: 1 },
      line: { style: "solid", fill: theme.rule, width: 1 },
    },
    chartFill: theme.canvas,
    chartLine: { style: "solid", fill: theme.canvas, width: 0 },
    plotAreaFill: theme.canvas,
    plotAreaLine: { style: "solid", fill: theme.canvas, width: 0 },
  });
  if (chart.source_url) addNotes(slide, [{ url: chart.source_url, label: chart.title || slideSpec.title }]);
  chartCount += 1;
  return slide;
}

await addCover();
const contentSlides = Array.isArray(spec.slides) ? spec.slides.slice(0, 14) : [];
for (let offset = 0; offset < contentSlides.length; offset += 1) {
  const slideSpec = contentSlides[offset] || {};
  const index = offset + 2;
  const bullets = bulletsOf(slideSpec);
  if (slideSpec.chart?.categories?.length && slideSpec.chart?.series?.length) {
    addChartSlide(slideSpec, index, bullets);
  } else if (slideSpec.image?.path) {
    await addImageSlide(slideSpec, index, bullets);
  } else if (brandKey === "codex_grid") {
    await addCodexTextSlide(slideSpec, index, bullets);
  } else {
    addBrandTextSlide(slideSpec, index, bullets);
  }
}
if (spec.include_closing !== false) await addClosing(presentation.slides.items.length + 1);

await fs.mkdir(outputDir, { recursive: true });
const previews = [];
const layoutFiles = [];
const warnings = [];

function inspectLayout(value, slideNumber, trail = "root") {
  if (!value || typeof value !== "object") return;
  if (Array.isArray(value)) {
    value.forEach((item, index) => inspectLayout(item, slideNumber, `${trail}[${index}]`));
    return;
  }
  for (const [key, item] of Object.entries(value)) {
    if (/overflow|clipp|truncat/i.test(key) && item === true) warnings.push(`slide ${slideNumber}: ${trail}.${key}`);
    inspectLayout(item, slideNumber, `${trail}.${key}`);
  }
}

for (const [offset, slide] of presentation.slides.items.entries()) {
  const stem = `slide-${String(offset + 1).padStart(2, "0")}`;
  const pngPath = path.join(outputDir, `${stem}.png`);
  const png = await presentation.export({ slide, format: "png", scale: 1 });
  await fs.writeFile(pngPath, Buffer.from(await png.arrayBuffer()));
  previews.push(pngPath);
  const layoutPath = path.join(outputDir, `${stem}.layout.json`);
  const layoutBlob = await slide.export({ format: "layout" });
  const layoutText = await layoutBlob.text();
  await fs.writeFile(layoutPath, layoutText, "utf8");
  layoutFiles.push(layoutPath);
  inspectLayout(JSON.parse(layoutText), offset + 1);
}

const montagePath = path.join(outputDir, "deck-montage.png");
const montage = await presentation.export({ format: "png", montage: true, scale: 1 });
await fs.writeFile(montagePath, Buffer.from(await montage.arrayBuffer()));

const pptxPath = path.join(outputDir, "deck.pptx");
const pptx = await PresentationFile.exportPptx(presentation);
await pptx.save(pptxPath);

const qa = {
  ok: warnings.length === 0,
  slide_count: presentation.slides.items.length,
  preview_files: previews,
  layout_files: layoutFiles,
  montage_path: montagePath,
  pptx_path: pptxPath,
  brand_template: brandKey,
  layout_strategy: layoutStrategy,
  image_count: imageCount,
  chart_count: chartCount,
  source_count: sourceLedger.length,
  sources: sourceLedger,
  warnings,
};
await fs.writeFile(path.join(outputDir, "qa.json"), JSON.stringify(qa, null, 2), "utf8");
process.stdout.write(JSON.stringify(qa));
