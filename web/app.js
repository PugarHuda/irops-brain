// IROPS Brain UI. All dynamic text goes through textContent (el()), never innerHTML.
const EPOCH = Date.UTC(2022, 11, 21);
const $ = (id) => document.getElementById(id);
const AGENTS = { ops: "Ops control", crew: "Crew scheduling", pax: "Passenger recovery" };
const KIND = {
  cancel_round_trip: "Cancel round trip", publish_delays: "Publish delays", assign_reserve: "Assign reserve",
  rebook: "Rebook", refund: "Refund",
};
const RULE_URL = {
  "14CFR117.13": "https://www.ecfr.gov/current/title-14/chapter-I/subchapter-G/part-117/section-117.13",
  "14CFR117.21": "https://www.ecfr.gov/current/title-14/chapter-I/subchapter-G/part-117/section-117.21",
  "14CFR117.25": "https://www.ecfr.gov/current/title-14/chapter-I/subchapter-G/part-117/section-117.25",
  "DOT-REFUND-2024": "https://www.law.cornell.edu/cfr/text/14/260.6",
};
let tz = "America/Denver";
let tenant = "irops_WN";   // one FalkorDB graph per airline; every call names the graph it acts on
let scenarios = [];
let roots = [], baseline = null;   // the run's late departures and its projection, to show what each approval changed
const CARRIER = (t) => t.split("_").pop();

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
  if (!r.ok) {  // FastAPI sends {detail}; a proxy or a crash may send nothing useful, so say at least the status
    const d = (await r.json().catch(() => ({}))).detail;
    throw new Error(typeof d === "string" ? d : r.status >= 502 ? `the backend did not answer (HTTP ${r.status}); it may be waking up, try again in a minute`
      : `HTTP ${r.status}${r.statusText ? " " + r.statusText : ""}`);
  }
  return r.json();
}

// ---------- graph canvas ----------
const cy = cytoscape({
  container: $("cy"), minZoom: 0.2, maxZoom: 2.5,
  style: [
    { selector: "node", style: { label: "data(label)", "text-wrap": "wrap", "font-family": "Archivo, Helvetica Neue, Arial, sans-serif", "font-weight": 600,
      "font-size": 12, "text-valign": "center", "text-halign": "center", color: "#000", shape: "round-rectangle",
      width: 122, height: 52, "background-color": "#FFFFFF", "border-width": 1.5, "border-color": "#000" } },
    { selector: "node.root", style: { "background-color": "#000", color: "#F3F3F5" } },
    { selector: "node.late", style: { "border-color": "#D2246E", "border-width": "data(bw)", "background-color": "#FDE3EC" } },
    { selector: "node.cancelled", style: { "background-color": "#E9E9EE", "border-style": "dashed", color: "#55555F" } },
    { selector: "node.Airport", style: { shape: "ellipse", width: 54, height: 54, "background-color": "#F1DC5B", "border-color": "#000", "font-size": 15 } },
    { selector: "node.Crew", style: { shape: "tag", "background-color": "#3346C8", color: "white", width: 80, height: 34 } },
    { selector: "edge", style: { width: 2.5, "curve-style": "bezier", "target-arrow-shape": "triangle",
      "line-color": "#000", "target-arrow-color": "#000", label: "data(label)", "font-size": 11,
      "font-family": "Archivo, Helvetica Neue, Arial, sans-serif", "text-background-color": "#fff", "text-background-opacity": 0.9,
      "text-background-padding": 2, color: "#55555F" } },
    { selector: "edge.CREW_NEXT, edge.ASSIGNED", style: { "line-color": "#3346C8", "target-arrow-color": "#3346C8", "line-style": "dashed" } },
    { selector: "edge.PAX_CONNECT, edge.CONN", style: { "line-color": "#D2246E", "target-arrow-color": "#D2246E", "line-style": "dotted" } },
    { selector: "edge.ROUTE, edge.ARRIVES_AT", style: { "line-color": "#F1DC5B", "target-arrow-color": "#C9B43A", width: 3 } },
  ],
});

const ordinal = (n) => n + (["th", "st", "nd", "rd"][(n % 100 - 20) % 10] || ["th", "st", "nd", "rd"][n % 100] || "th");

function edgeLabel(e) {
  if (e.type === "NEXT_LEG") return `aircraft ${e.tail || ""} · slack ${e.slack}m`;
  if (e.type === "CREW_NEXT") return `crew ${e.crew} · slack ${e.slack}m`;
  if (e.type === "ROUTE") return e.flow !== undefined ? `${e.flow} pax can still fly in` : `${e.block} min`;
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
  scenarios = s.scenarios;
  for (const sc of s.scenarios) $("scenario").append(el("option", { value: sc.id }, `${sc.title} · ${CARRIER(sc.tenant)}`));
  $("scenario").addEventListener("change", pickScenario);
  pickScenario();
  $("run").addEventListener("click", run);
  $("reset").addEventListener("click", resetOps);
  $("live-refresh").addEventListener("click", loadLive);
  $("mcp-cmd").textContent = `claude mcp add --transport http irops-brain ${location.origin}/mcp/`;
  $("mcp-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("mcp-cmd").textContent); $("mcp-copy").textContent = "Copied"; }
    catch { $("mcp-copy").textContent = "Select the command and copy it"; }
  });
  loadMcpTools();
  loadProof();
}

async function loadLive() {
  const list = $("live-list"), btn = $("live-refresh");
  btn.disabled = true;
  list.replaceChildren(el("li", { class: "empty" }, "Reading the FAA and weather feeds…"));
  try {
    const r = await api(`/api/live?tenant=${encodeURIComponent(tenant)}`);
    const when = r.feed?.faa_time || "";
    if (r.feed?.busy) setTimeout(loadLive, 15000);  // an agent run is writing; fetch fresh feeds once it is done
    $("live-caption").textContent = !r.weather_categories
      ? "The live layer has not been fetched into this graph yet." + (r.feed?.busy ? " Retrying once the agent run finishes." : "")
      : r.feed?.busy ? `An agent run is writing to the graph, so this is the stored layer (FAA status as of ${when}). Refreshing shortly.`
      : r.feed?.error
      ? `The live feeds could not be reached (${r.feed.error}). Showing the last layer stored in the graph.`
      : `FAA status as of ${when}. METAR for ${r.weather_categories.reduce((n, c) => n + c.airports, 0)} airports in ${CARRIER(tenant)}'s network: `
        + r.weather_categories.map((c) => `${c.airports} ${c.cat || "without a flight category"}`).join(", ") + ". "
        + `TAF forecasts flag ${r.rows.filter((x) => x.live.some((l) => l.source === "TAF")).length} of them for the next 12 hours.`
        + (r.feed.aircraft_airborne != null ? ` OpenSky ADS-B sees ${fmt(r.feed.aircraft_airborne)} ${CARRIER(tenant)} aircraft airborne and `
          + `${fmt(r.feed.aircraft_on_ground)} on the ground right now.` : "")
        + (r.feed.feed_errors ? ` Some feeds did not answer this round (${r.feed.feed_errors}).` : "");
    if (!r.rows.length) { list.replaceChildren(el("li", { class: "empty" }, "Nothing in this network is constrained right now.")); return; }
    list.replaceChildren(...r.rows.map((x) => el("li", {},
      el("b", {}, x.airport), ` ${x.name || ""} · ${fmt(x.routes)} routes, ${fmt(x.daily_flights)} flights a day touch it`
        + (x.network_rank ? `, ${fmt(x.connecting_pax_per_day)} passengers a day connect here, ${ordinal(x.network_rank)} most central of ${x.airports_ranked} (PageRank)` : "")
        + (x.aircraft_on_ground_now ? `, ${x.aircraft_on_ground_now} of the airline's aircraft on the ground there now (ADS-B)` : ""),
      x.spokes_mostly_served_from_here.length ? ` · mostly serves ${x.spokes_mostly_served_from_here.join(", ")}` : "",
      el("ul", {}, x.live.map((l) => el("li", {}, el("span", { class: `src ${l.source}` }, l.source), ` ${l.kind}: `,
        el("code", {}, l.reason), l.detail ? ` (${l.detail})` : ""))))));
  } catch (e) {
    list.replaceChildren(el("li", { class: "empty" }, `Live conditions unavailable: ${e.message}`));
  } finally { btn.disabled = false; }
}

function pickScenario() {
  const sc = scenarios.find((x) => x.id === $("scenario").value);
  if (!sc) return;
  const changed = sc.tenant !== tenant || !$("live-list").children.length;
  tenant = sc.tenant;
  if (changed) loadLive();
  $("q").placeholder = CARRIER(tenant) === "AS" ? "What breaks if AS292 2022-12-22 SEA is 3h late?"
    : "What breaks if WN2262 2022-12-22 DEN is 2h late?";
  const ap = CARRIER(tenant) === "AS" ? "SEA" : "DEN";
  $("examples").replaceChildren(...[`Which crews are over their duty limit at ${ap}?`, `Who can cover a crew at ${ap}?`,
    `Weather at ${ap} right now`, `How many passengers can we still move from ${ap} to ${ap === "SEA" ? "ANC" : "BOS"}?`,
    "Which flights are the most load-bearing?"].map((x) =>
    el("button", { type: "button", onclick: () => { $("q").value = x; $("ask-form").requestSubmit(); } }, x)));
}

async function resetOps() {
  const btn = $("reset");
  btn.disabled = true;
  try {
    await api(`/api/reset?tenant=${encodeURIComponent(tenant)}`, {});
    document.querySelectorAll("#strips .strip:not(.done)").forEach((li) => markDone(li, "superseded"));
    if (baseline) renderImpact(baseline);
    $("board-hint").textContent = `Every approved change in ${CARRIER(tenant)}'s graph was undone and rejected actions may be proposed again. Run the agents for fresh proposals.`;
  } catch (e) {
    $("board-hint").textContent = `Undo failed: ${e.message}`;
  } finally { btn.disabled = false; }
}

async function run() {
  const btn = $("run");
  btn.disabled = true; btn.textContent = "Agents working…";
  try {
    const r = await api("/api/run", { scenario: $("scenario").value });
    tz = r.scenario.tz;
    tenant = r.tenant;
    $("board-hint").textContent = `Nothing changes the operation until you approve it. Graph: ${r.tenant}.`;
    $("clock-time").textContent = local(r.scenario.clock);
    $("clock-zone").textContent = `${local(r.scenario.clock, tz, { weekday: "short", day: "numeric", month: "short", timeZoneName: "short" })} · session ${r.session}`;
    $("story-title").textContent = r.scenario.title;
    $("story").textContent = `${r.scenario.story} Known so far: ${r.known.delayed} departures late, ${r.known.cancelled} cancelled.`;
    const b = $("briefing");
    b.hidden = false;
    b.textContent = r.briefing.text;
    roots = r.steps.find((s) => s.tool === "propagate_delay")?.args.roots || [];
    baseline = r.summary;
    renderImpact(r.summary);
    renderStrips(r.proposals.map((p) => ({ ...p, tenant: r.tenant })));
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
    $("briefing").textContent = /Another visitor/.test(e.message) ? e.message
      : `The run failed: ${e.message}. Press Run the agents to try again: every run starts again from a clean replay.`;
  } finally {
    btn.disabled = false; btn.textContent = "Run the agents again";
  }
}

function renderImpact(s, base) {
  const items = [
    ["More flights pulled late", s.downstream_flights, false],
    ["Passengers on them", s.pax_delayed, false],
    ["Passengers who miss a connection", s.pax_misconnecting, true],
    ["Crews pushed past their legal duty limit", s.crews_illegal, true],
    ["Passengers owed a refund if they ask (3h+ late or cancelled)", s.pax_refund_eligible, true],
  ];
  const dl = $("impact");
  const keys = ["downstream_flights", "pax_delayed", "pax_misconnecting", "crews_illegal", "pax_refund_eligible"];
  dl.replaceChildren(...items.map(([t, v, bad], i) => {
    const d = base ? (v || 0) - (base[keys[i]] || 0) : 0;
    return el("div", {}, el("dt", {}, t), el("dd", { class: bad ? "bad" : "" }, fmt(v)),
      d ? el("span", { class: `delta ${d < 0 ? "down" : "up"}` }, `${d > 0 ? "+" : "−"}${fmt(Math.abs(d))} after your approvals`) : null);
  }));
  dl.hidden = false;
}

// re-project from the run's late departures on the graph as it now stands
async function refreshImpact() {
  if (!roots.length) return;
  try {
    const r = await api("/api/impact", { roots, tenant });
    renderImpact(r.summary, baseline);
  } catch { /* the approval itself is already verified; the figures refresh on the next one */ }
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
  if (p.action && p.status === "proposed") {
    actions.append(el("button", { class: "approve", onclick: () => decide(p, li, "approve") }, "Approve"),
      el("button", { onclick: () => decide(p, li, "reject") }, "Reject"));
  }
  if (evidence) actions.append(el("button", { class: "ev", onclick: () => showPaths([evidence], p.summary) }, "Show evidence"));
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
  if (note) li.querySelector(".strip-main").append(el("div", { class: status === "approved" ? "verify" : "verify bad" }, note));
}

async function decide(p, li, verb) {
  const buttons = li.querySelectorAll(".actions button:not(.ev)");
  buttons.forEach((b) => (b.disabled = true));
  let r;
  try {
    r = await api(`/api/decisions/${p.id}/${verb}?tenant=${encodeURIComponent(p.tenant || tenant)}`, {});
  } catch (e) {
    buttons.forEach((b) => (b.disabled = false));
    li.querySelector(".strip-main").append(el("div", { class: "verify bad" }, `Not sent: ${e.message}. Try again.`));
    return;
  }
  if (!r.ok) return markDone(li, verb === "reject" ? "superseded" : "failed", r.reason || "The graph refused this change.");
  if (verb === "reject") return markDone(li, "rejected");
  const v = r.verification;
  markDone(li, "approved", `Written to the graph, then read back: ${v.rows.length} row(s) show the intended state.`);
  refreshImpact();
  if (v.cypher?.length) li.querySelector(".strip-main").append(el("details", {}, el("summary", {}, "Read-back query"), v.cypher.map((q) => el("pre", {}, q))));
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
        s.extra && s.extra.max_passengers !== undefined
          ? el("div", { class: "result" }, `At most ${s.extra.max_passengers} passengers (max flow), limited by ${s.extra.bottleneck}.`) : null,
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
    const r = await api("/api/ask", { question: q, tenant });
    const paths = r.calls.flatMap((c) => c.paths || []);
    box.replaceChildren(el("p", { class: "text" }, r.answer), ...(r.note ? [el("p", { class: "by" }, r.note)] : []),
      el("p", { class: "by" }, `${r.by === "router" ? "Answered by the intent router, using" : `${r.by}, using`} ${r.calls.map((c) => c.tool).join(", ") || "no tool"}`),
      ...r.calls.map((c) => el("details", {}, el("summary", {}, `${c.tool}: ${c.outcome}`), c.cypher.map((x) => el("pre", {}, x)))));
    if (paths.length) showPaths(paths, q);
  } catch (e) {
    box.replaceChildren(el("p", { class: "text" }, `The question could not be answered: ${e.message}`));
  }
});

// ---------- proof ----------
async function loadProof() {
  const { results, decisions } = await api("/api/backtest");
  if (!results.length) return;
  const head = el("tr", {}, ["Airline", "Traversal depth", "Real knock-on delays", "Caught", "Recall", "Precision", "Mean error"].map((h) => el("th", {}, h)));
  const best = {};
  for (const r of results) best[r.airline] = Math.max(best[r.airline] || 0, r.caught);
  $("backtest").replaceChildren(el("thead", {}, head), el("tbody", {}, results.map((r) =>
    el("tr", { class: r.caught === best[r.airline] ? "best" : "" }, el("td", {}, r.airline === "AS" ? "Alaska" : "Southwest"),
      el("td", {}, r.hops === 1 ? "1 hop (previous leg only)" : `${r.hops} hops`),
      el("td", {}, fmt(r.real_knock_on_flights)), el("td", {}, fmt(r.caught)), el("td", {}, `${Math.round(r.recall * 100)}%`),
      el("td", {}, `${Math.round(r.precision * 100)}%`), el("td", {}, `${r.mae_minutes_on_caught} min`)))));
  const pct = (v) => (v == null ? "—" : `${Math.round(v * 100)}%`);
  const sc = Object.fromEntries(scenarios.map((s) => [s.id, s.title]));
  const dh = el("tr", {}, ["Scenario", "Cancels proposed", "Airline also cancelled", "Cancelled or 3h+ late",
    "Projected 3h+ late", "Really 3h+ late or cancelled", "Base rate downstream"].map((h) => el("th", {}, h)));
  $("decisions").replaceChildren(el("thead", {}, dh), el("tbody", {}, (decisions || []).map((d) => el("tr", {},
    el("td", {}, sc[d.scenario] || d.scenario), el("td", {}, fmt(d.cancels_proposed)), el("td", {}, fmt(d.airline_also_cancelled)),
    el("td", {}, fmt(d.cancels_cancelled_or_3h_late)), el("td", {}, fmt(d.projected_3h_late)),
    el("td", {}, `${fmt(d.really_3h_late_or_cancelled)} (${pct(d.precision)})`),
    el("td", {}, `${pct(d.base_rate)} of ${fmt(d.reachable_flights)}`)))));
}

async function loadCompare(root) {
  try {
    const c = await api(`/api/compare?flight=${encodeURIComponent(root.flight)}&delay=${root.delay}&tenant=${encodeURIComponent(tenant)}`);
    $("compare").replaceChildren(
      el("div", {}, el("h3", {}, "Record lookup"), el("p", {}, `${c.flight.fno} ${c.flight.origin}→${c.flight.dest} is ${c.delay} min late. ${c.flat.why} Connections and crews: unknown.`)),
      el("div", { class: "graph" }, el("h3", {}, "Graph traversal"),
        el("p", {}, `${fmt(c.graph.downstream_flights)} more flights late, ${fmt(c.graph.pax_misconnecting)} passengers miss connections, ${fmt(c.graph.crews_illegal)} crews over their duty limit — from the same one late flight.`)));
  } catch { /* comparison is optional */ }
}

// the MCP endpoint is live: list its tools with a real JSON-RPC call
async function loadMcpTools() {
  try {
    const r = await fetch("/mcp/", { method: "POST", headers: { "content-type": "application/json",
      accept: "application/json, text/event-stream" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list", params: {} }) });
    const j = await r.json();
    const names = (j.result?.tools || []).map((t) => t.name);
    $("mcp-tools").textContent = names.length ? `Live now: ${names.join(", ")}.` : "";
  } catch { /* the panel still shows how to connect */ }
}

init();
