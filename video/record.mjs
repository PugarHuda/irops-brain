// Screen recording of the live app for the demo video: one continuous Playwright session, 1440x900.
//   node record.mjs            (needs public/timing.json from tts.py)
// Every scene runs its real actions against https://irops-brain.vercel.app. Long waits (the agents' run, an LLM
// answer) are marked as cuts, so the video shows the click and the result, not the spinner.
// Writes public/screen.webm and public/clips.json: per scene, the [from, to] seconds of screen.webm to show.
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";

const APP = process.env.APP_URL || "https://irops-brain.vercel.app/";
const OUT = path.resolve("public");
const timing = Object.fromEntries(JSON.parse(fs.readFileSync(path.join(OUT, "timing.json"), "utf8")).map((t) => [t.id, t.duration]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// a visible cursor: headless recordings have none
const CURSOR = `addEventListener("DOMContentLoaded", () => {
  const c = document.createElement("div");
  c.style.cssText = "position:fixed;z-index:99999;width:22px;height:22px;margin:-3px 0 0 -3px;pointer-events:none;" +
    "background:url(\\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M3 2l7 19 3-8 8-3z' fill='black' stroke='white' stroke-width='1.5'/%3E%3C/svg%3E\\") no-repeat;transition:transform .08s";
  document.body.append(c);
  addEventListener("mousemove", (e) => { c.style.left = e.clientX + "px"; c.style.top = e.clientY + "px"; }, true);
  addEventListener("mousedown", () => (c.style.transform = "scale(.8)"), true);
  addEventListener("mouseup", () => (c.style.transform = ""), true);
});`;

// warm the free-tier backend first so the recording does not start on a cold server
for (let i = 0; i < 20; i++) {
  try { if ((await fetch(new URL("api/scenarios", APP))).ok) break; } catch {}
  await sleep(5000);
}
await fetch(new URL("api/reset?tenant=irops_WN", APP), { method: "POST" });
await fetch(new URL("api/reset?tenant=irops_AS", APP), { method: "POST" });

const browser = await chromium.launch({ channel: "chrome" });  // the installed Chrome
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1,
  recordVideo: { dir: path.join(OUT, "raw"), size: { width: 1440, height: 900 } } });
await ctx.addInitScript(CURSOR);
const page = await ctx.newPage();
const t0 = Date.now();
const now = () => (Date.now() - t0) / 1000;
const clips = {};

async function scene(id, fn) {
  const segs = [[now(), null]];
  let hidden = 0;
  const cut = async (wait) => {           // run `wait` off camera
    const a = now(); segs.at(-1)[1] = a;
    await wait();
    segs.push([now(), null]); hidden += now() - a;
  };
  const start = now();
  await fn(cut);
  const need = timing[id] + 0.6;
  while (now() - start - hidden < need) await sleep(100);
  segs.at(-1)[1] = now();
  clips[id] = segs.map(([a, b]) => [+a.toFixed(2), +b.toFixed(2)]);
  console.log(id, clips[id]);
}

async function glide(sel, { offset = 24, ms = 1400 } = {}) {   // eased scroll so the viewer can follow
  await page.evaluate(async ([sel, offset, ms]) => {
    document.documentElement.style.scrollBehavior = "auto";
    const el = typeof sel === "number" ? null : document.querySelector(sel);
    const from = scrollY, to = el ? el.getBoundingClientRect().top + scrollY - offset : sel;
    const s = performance.now();
    await new Promise((done) => {
      const step = (t) => {
        const k = Math.min(1, (t - s) / ms), e = k < .5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
        scrollTo(0, from + (to - from) * e);
        k < 1 ? requestAnimationFrame(step) : done();
      };
      requestAnimationFrame(step);
    });
  }, [sel, offset, ms]);
}

async function point(locator, click = true) {
  const b = await locator.boundingBox();
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 25 });
  await sleep(250);
  if (click) await locator.click();
}

await page.goto(APP, { waitUntil: "networkidle" });
await page.mouse.move(1100, 500);
await sleep(800);

await scene("intro", async () => {});       // title card is drawn by Remotion; the hero sits behind it

await scene("story", async () => {
  await sleep(2500);
  await point(page.locator("#scenario"), false);
  await sleep(1500);
  await glide(".story", { ms: 2200 });
  await sleep(4000);
  await glide(0, { ms: 1800 });
});

await scene("run", async (cut) => {
  await point(page.locator("#run"));
  await sleep(1200);
  await cut(() => page.waitForFunction(() => document.getElementById("run").textContent === "Run the agents again", null, { timeout: 120000 }));
  await sleep(1500);
  await glide(".story", { ms: 1600 });
  await sleep(3500);
  await glide("#impact", { offset: 300, ms: 1600 });
});

await scene("evidence", async () => {
  await glide(".evidence", { ms: 1600 });
  await sleep(1200);
  // follow one chain on the canvas: late Denver departure -> same aircraft's next leg -> the one after that
  const box = await page.locator("#cy").boundingBox();
  const node = async (label) => page.evaluate((label) => {
    const n = cy.nodes().filter((x) => (x.data("label") || "").startsWith(label))[0];
    return n ? n.renderedPosition() : null;
  }, label);
  for (const label of ["WN2100 DEN", "WN4818 MSP", "WN4818 MDW"]) {
    const p = await node(label);
    if (p) await page.mouse.move(box.x + p.x, box.y + p.y, { steps: 35 });
    await sleep(1600);
  }
});

await scene("proposals", async () => {
  await glide(".board", { ms: 1200 });
  const strips = page.locator("#strips");
  await point(strips.locator(".strip").first(), false);
  for (let i = 0; i < 6; i++) { await strips.evaluate((el) => el.scrollBy({ top: 120, behavior: "smooth" })); await sleep(900); }
  await strips.evaluate((el) => el.scrollTo({ top: 0, behavior: "smooth" }));
});

await scene("approve", async (cut) => {
  const reserve = page.locator("#strips .strip", { hasText: "Assign reserve" }).first();
  await point(reserve.locator("button.approve"));
  await page.waitForSelector("#strips .stamp.approved", { timeout: 60000 });
  await sleep(2200);
  const cancel = page.locator("#strips .strip:not(.done)", { hasText: "Cancel round trip" }).first();
  await cancel.scrollIntoViewIfNeeded();
  await point(cancel.getByRole("button", { name: "Reject" }));
  await sleep(1500);
  await glide(0, { ms: 1200 });
  await point(page.locator("#run"));
  await cut(() => page.waitForFunction(() => document.getElementById("run").textContent === "Run the agents again" &&
    [...document.querySelectorAll(".stamp")].some((s) => s.textContent === "WITHHELD"), null, { timeout: 120000 }));
  const held = page.locator("#strips .strip", { hasText: "Withheld:" }).first();
  await glide(".board", { ms: 1200 });
  await held.scrollIntoViewIfNeeded();
  await point(held, false);
});

await scene("trace", async () => {
  await glide(".trace", { ms: 1500 });
  await sleep(800);
  const d = page.locator("#lanes .lane[data-agent=ops] details").nth(2);
  await point(d.locator("summary"));
  await sleep(2500);
  await page.mouse.move(900, 600, { steps: 30 });
});

await scene("ask", async (cut) => {
  await glide(".ask", { offset: 200, ms: 1500 });
  await point(page.locator("#q"));
  await page.locator("#q").pressSequentially("What breaks if WN2262 2022-12-22 DEN is 2h late?", { delay: 35 });
  await point(page.locator("#ask-form button"));
  await cut(() => page.waitForFunction(() => document.querySelector("#answer .text")?.textContent, null, { timeout: 90000 }));
  await sleep(500);
});

await scene("proof", async () => {
  await glide(".proof", { ms: 1600 });
  await sleep(4500);
  await glide("#decisions", { offset: 260, ms: 1600 });
});

await scene("live", async () => {
  await glide(".live", { ms: 1500 });
  await sleep(3000);
  await glide(0, { ms: 1200 });
  await point(page.locator("#scenario"), false);
  await page.selectOption("#scenario", "sea-ice");
  await sleep(1800);
  await glide(".mcp", { offset: 120, ms: 2000 });
  await point(page.locator("#mcp-copy"), false);
});

await scene("outro", async () => {});

await page.close();
const raw = await page.video().path();
await ctx.close();
await browser.close();
fs.renameSync(raw, path.join(OUT, "screen.webm"));
fs.writeFileSync(path.join(OUT, "clips.json"), JSON.stringify(clips, null, 1));
await fetch(new URL("api/reset?tenant=irops_WN", APP), { method: "POST" });
console.log("done", now().toFixed(1), "s");
