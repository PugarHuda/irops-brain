// IROPS Brain UI. All dynamic text goes through textContent (el()), never innerHTML.
const EPOCH = Date.UTC(2022, 11, 21);
const $ = (id) => document.getElementById(id);
const AGENTS = { ops: "Ops control", crew: "Crew scheduling", pax: "Passenger recovery" };
const KIND = {
  cancel_round_trip: "Cancel round trip", publish_delays: "Publish delays", assign_reserve: "Assign reserve",
  rebook: "Rebook", refund: "Offer refund",
};
const RULE_URL = {
  "14CFR117.13": "https://www.ecfr.gov/current/title-14/chapter-I/subchapter-G/part-117/section-117.13",
  "14CFR117.21": "https://www.ecfr.gov/current/title-14/chapter-I/subchapter-G/part-117/section-117.21",
  "14CFR117.25": "https://www.ecfr.gov/current/title-14/chapter-I/subchapter-G/part-117/section-117.25",
  "DOT-REFUND-2024": "https://www.transportation.gov/briefing-room/biden-harris-administration-announces-final-rule-requiring-automatic-refunds-airline",
};
let tz = "America/Denver";

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) n.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) n.append(k instanceof Node ? k : String(k));
  return n;
}
const local = (m, zone = tz, opts = { hour: "2-digit", minute: "2-digit", hour12: false }) =>
  new Intl.DateTimeFormat("en-US", { timeZone: zone, ...opts }).format(new Date(EPOCH + m * 60000));
const fmt = (n) => Number(n || 0).toLocaleString("en-US");

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

// ---------- graph canvas ----------
const cy = cytoscape({
  container: $("cy"), minZoom: 0.2, maxZoom: 2.5,
  style: [
    { selector: "node", style: { label: "data(label)", "text-wrap": "wrap", "font-family": "Barlow Condensed",
      "font-size": 13, "text-valign": "center", "text-halign": "center", color: "#14233B", shape: "round-rectangle",
      width: 108, height: 52, "background-color": "#F7F3E3", "border-width": 1.5, "border-color": "#14233B" } },
    { selector: "node.root", style: { "background-color": "#14233B", color: "#EEF2EA" } },
    { selector: "node.late", style: { "border-color": "#A3195B", "border-width": "data(bw)" } },
    { selector: "node.cancelled", style: { "background-color": "#E2E8DC", "border-style": "dashed", color: "#46546B" } },
    { selector: "node.Airport", style: { shape: "ellipse", width: 54, height: 54, "background-color": "#CDBF98", "font-size": 15 } },
    { selector: "node.Crew", style: { shape: "tag", "background-color": "#2C5C8F", color: "white", width: 80, height: 34 } },
    { selector: "edge", style: { width: 2.5, "curve-style": "bezier", "target-arrow-shape": "triangle",
      "line-color": "#14233B", "target-arrow-color": "#14233B", label: "data(label)", "font-size": 11,
      "font-family": "Atkinson Hyperlegible", "text-background-color": "#fff", "text-background-opacity": 0.9,
      "text-background-padding": 2, color: "#46546B" } },
    { selector: "edge.CREW_NEXT, edge.ASSIGNED", style: { "line-color": "#2C5C8F", "target-arrow-color": "#2C5C8F", "line-style": "dashed" } },
    { selector: "edge.PAX_CONNECT, edge.CONN", style: { "line-color": "#A3195B", "target-arrow-color": "#A3195B", "line-style": "dotted" } },
    { selector: "edge.ROUTE, edge.ARRIVES_AT", style: { "line-color": "#CDBF98", "target-arrow-color": "#B2A27A", width: 3 } },
  ],
});

function edgeLabel(e) {
  if (e.type === "NEXT_LEG") return `aircraft ${e.tail || ""} · slack ${e.slack}m`;
  if (e.type === "CREW_NEXT") return `crew ${e.crew} · slack ${e.slack}m`;
  if (e.type === "ROUTE") return `${e.block} min`;
  if (e.type === "CONN") return "connect";
  if (e.type === "ARRIVES_AT") return "arrives";
  if (e.type === "ASSIGNED") return "flies";
  return e.type.toLowerCase();
}

function showPaths(paths, caption, delays = {}) {
  cy.elements().remove();
  const seen = new Set();
  const add = [];
  for (const p of paths || []) {
    p.nodes.forEach((n, i) => {
      if (seen.has(n.key)) return;
      seen.add(n.key);
      const d = delays[n.key] ?? n.delay ?? 0;
      let label = n.key, cls = n.label;
      if (n.label === "Flight") {
        label = `${n.fno}  ${n.origin}→${n.dest}\n${n.dep !== undefined ? local(n.dep, tz) : ""}${d ? `  +${d}m` : ""}`;
        if (n.status === "cancelled") cls += " cancelled";
        else if (i === 0 && n.status === "delayed") cls += " root";
        else if (d > 0) cls += " late";
      } else if (n.label === "Airport") label = n.code || n.key;
      add.push({ group: "nodes", data: { id: n.key, label, bw: Math.min(7, 1.5 + d / 60) }, classes: cls });
    });
    for (const e of p.edges) {
      const id = `${e.src}>${e.dst}>${e.type}`;
      if (seen.has(id) || !e.src || !e.dst) continue;
      seen.add(id);
      add.push({ group: "edges", data: { id, source: e.src, target: e.dst, label: edgeLabel(e) }, classes: e.type });
    }
  }
  cy.add(add);
  // chains read left to right, like a timeline
  cy.layout({ name: "breadthfirst", directed: true, spacingFactor: 1.05, padding: 24,
    roots: cy.nodes(".root").length ? cy.nodes(".root") : undefined,
    transform: (n, pos) => ({ x: pos.y * 2.8, y: pos.x * 0.55 }) }).run();
  if (cy.zoom() < 0.6) { cy.zoom(0.6); cy.pan({ x: 24, y: cy.height() / 2 - cy.nodes()[0].position("y") * 0.6 }); }
  $("ev-caption").textContent = caption || (paths?.length ? "" : "This answer has no graph path.");
}

// ---------- scenario + run ----------
async function init() {
  const s = await api("/api/scenarios");
  for (const sc of s.scenarios) $("scenario").append(el("option", { value: sc.id }, sc.title));
  $("run").addEventListener("click", run);
  loadProof();
}

async function run() {
  const btn = $("run");
  btn.disabled = true; btn.textContent = "Agents working…";
  try {
    const r = await api("/api/run", { scenario: $("scenario").value });
    tz = r.scenario.tz;
    $("clock-time").textContent = local(r.scenario.clock);
    $("clock-zone").textContent = `${local(r.scenario.clock, tz, { weekday: "short", day: "numeric", month: "short", timeZoneName: "short" })} · session ${r.session}`;
    $("story-title").textContent = r.scenario.title;
    $("story").textContent = `${r.scenario.story} Known so far: ${r.known.delayed} departures late, ${r.known.cancelled} cancelled.`;
    const b = $("briefing");
    b.hidden = false;
    b.textContent = r.briefing.text;
    renderImpact(r.summary);
    renderStrips(r.proposals);
    renderTrace(r.steps);
    const prop = r.steps.find((s) => s.tool === "propagate_delay");
    if (prop) {
      const delays = Object.fromEntries(prop.rows.map((x) => [x.id, x.delay]));
      const worst = prop.paths.map((p, i) => [p, prop.rows[i]?.delay || 0]).sort((a, b) => b[1] - a[1]).slice(0, 8).map((x) => x[0]);
      showPaths(worst, `The 8 worst of ${prop.extra.summary.downstream_flights} knock-on delays, traced back to the late departure that causes each. Slack is consumed at every hop.`, delays);
      loadCompare([...prop.args.roots].sort((a, b) => b.delay - a.delay)[0]);
    }
  } catch (e) {
    $("briefing").hidden = false;
    $("briefing").textContent = `The run failed: ${e.message}. Check that FalkorDB is running and the graph is loaded (uv run python -m irops.ingest).`;
  } finally {
    btn.disabled = false; btn.textContent = "Run the agents again";
  }
}

function renderImpact(s) {
  const items = [
    ["More flights pulled late", s.downstream_flights, false],
    ["Passengers on them", s.pax_delayed, false],
    ["Passengers who miss a connection", s.pax_misconnecting, true],
    ["Crews pushed past their legal duty limit", s.crews_illegal, true],
  ];
  const dl = $("impact");
  dl.replaceChildren(...items.map(([t, v, bad]) => el("div", {}, el("dt", {}, t), el("dd", { class: bad ? "bad" : "" }, fmt(v)))));
  dl.hidden = false;
}

function renderStrips(list) {
  const ol = $("strips");
  if (!list.length) { ol.replaceChildren(el("li", { class: "empty" }, "The agents found nothing to change.")); return; }
  ol.replaceChildren(...list.map(strip));
}

function strip(p) {
  const li = el("li", { class: "strip", "data-agent": p.agent });
  const evidence = p.evidence ? JSON.parse(p.evidence) : null;
  const actions = el("div", { class: "actions" });
  if (p.action) {
    actions.append(el("button", { class: "approve", onclick: () => decide(p, li, "approve") }, "Approve"),
      el("button", { onclick: () => decide(p, li, "reject") }, "Reject"));
  }
  if (evidence) actions.append(el("button", { onclick: () => showPaths([evidence], p.summary) }, "Show evidence"));
  li.append(el("div", { class: "strip-body" },
    el("div", { class: "strip-kind" }, KIND[p.kind] || p.kind, el("small", {}, AGENTS[p.agent])),
    el("div", { class: "strip-main" }, el("p", {}, p.summary),
      p.rules.length ? el("div", { class: "rules" }, p.rules.map((r) => el("a", { href: RULE_URL[r], target: "_blank", rel: "noopener" }, r))) : null,
      actions)));
  if (p.status !== "proposed") markDone(li, p.status);
  return li;
}

function markDone(li, status, note) {
  li.classList.add("done");
  li.append(el("span", { class: `stamp ${status}` }, status.toUpperCase()));
  if (note) li.querySelector(".strip-main").append(el("div", { class: "verify" }, note));
}

async function decide(p, li, verb) {
  li.querySelectorAll("button").forEach((b) => (b.disabled = true));
  const r = await api(`/api/decisions/${p.id}/${verb}`, {});
  if (verb === "reject") return markDone(li, "rejected");
  if (!r.ok) return markDone(li, "failed", r.reason || "The graph refused this change.");
  const v = r.verification;
  markDone(li, "approved", `Written to the graph and re-checked: ${v.outcome}, ${v.rows.length} row(s) read back.`);
}

function renderTrace(steps) {
  const lanes = $("lanes");
  lanes.replaceChildren(...Object.entries(AGENTS).map(([id, name]) => {
    const mine = steps.filter((s) => s.agent === id);
    return el("div", { class: "lane", "data-agent": id }, el("h3", {}, name),
      el("ol", {}, mine.map((s) => el("li", {},
        el("b", {}, s.tool), el("span", { class: `outcome ${s.outcome}` }, s.outcome.replace(/_/g, " ")),
        s.paths.length ? el("button", { class: "show", onclick: () => showPaths(s.paths, s.note) }, "paths") : null,
        el("div", {}, s.note),
        el("details", {}, el("summary", {}, `${s.cypher.length} Cypher quer${s.cypher.length === 1 ? "y" : "ies"}`),
          s.cypher.map((q) => el("pre", {}, q)))))));
  }));
}

// ---------- ask ----------
$("ask-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("q").value.trim() || $("q").placeholder;
  const box = $("answer");
  box.replaceChildren(el("p", { class: "by" }, "Asking the graph…"));
  try {
    const r = await api("/api/ask", { question: q });
    const paths = r.calls.flatMap((c) => c.paths || []);
    box.replaceChildren(el("p", { class: "text" }, r.answer),
      el("p", { class: "by" }, `${r.by === "claude" ? "Claude, using" : "Answered by"} ${r.calls.map((c) => c.tool).join(", ") || "no tool"}`),
      ...r.calls.map((c) => el("details", {}, el("summary", {}, `${c.tool}: ${c.outcome}`), c.cypher.map((x) => el("pre", {}, x)))));
    if (paths.length) showPaths(paths, q);
  } catch (e) {
    box.replaceChildren(el("p", { class: "text" }, `The question could not be answered: ${e.message}`));
  }
});

// ---------- proof ----------
async function loadProof() {
  const { results } = await api("/api/backtest");
  if (!results.length) return;
  const head = el("tr", {}, ["Traversal depth", "Real knock-on delays", "Caught", "Recall", "Precision", "Mean error"].map((h) => el("th", {}, h)));
  const best = Math.max(...results.map((r) => r.caught));
  $("backtest").replaceChildren(el("thead", {}, head), el("tbody", {}, results.map((r) =>
    el("tr", { class: r.caught === best ? "best" : "" }, el("td", {}, r.hops === 1 ? "1 hop (previous leg only)" : `${r.hops} hops`),
      el("td", {}, fmt(r.real_knock_on_flights)), el("td", {}, fmt(r.caught)), el("td", {}, `${Math.round(r.recall * 100)}%`),
      el("td", {}, `${Math.round(r.precision * 100)}%`), el("td", {}, `${r.mae_minutes_on_caught} min`)))));
}

async function loadCompare(root) {
  try {
    const c = await api(`/api/compare?flight=${encodeURIComponent(root.flight)}&delay=${root.delay}`);
    $("compare").replaceChildren(
      el("div", {}, el("h3", {}, "Record lookup"), el("p", {}, `${c.flight.fno} ${c.flight.origin}→${c.flight.dest} is ${c.delay} min late. ${c.flat.why} Connections and crews: unknown.`)),
      el("div", { class: "graph" }, el("h3", {}, "Graph traversal"),
        el("p", {}, `${fmt(c.graph.downstream_flights)} more flights late, ${fmt(c.graph.pax_misconnecting)} passengers miss connections, ${fmt(c.graph.crews_illegal)} crews over their duty limit — from the same one late flight.`)));
  } catch { /* comparison is optional */ }
}

init();
