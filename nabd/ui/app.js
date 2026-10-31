"use strict";
/* Nabd War Room. Plain JS, no build step: it has to work on a venue laptop. */

// Optional children are written as `cond ? node : null`; the DOM would render those nulls as "null".
const nativeReplace = Element.prototype.replaceChildren;
Element.prototype.replaceChildren = function (...kids) {
  return nativeReplace.apply(this, kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
};

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

function h(tag, attrs = {}, ...children) {
  const el = document.createElementNS(tag === "svg" || attrs._svg ? "http://www.w3.org/2000/svg" : "http://www.w3.org/1999/xhtml", tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "_svg" || v === undefined || v === null || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
  return el;
}
const s = (tag, attrs = {}, ...children) => h(tag, { ...attrs, _svg: true }, ...children);

const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
const fmtDur = (sec) => {
  if (sec === null || sec === undefined) return "n/a";
  sec = Math.max(0, Math.round(sec));
  return sec >= 60 ? `${Math.floor(sec / 60)}m ${String(sec % 60).padStart(2, "0")}s` : `${sec}s`;
};
const pct = (x, d = 1) => (x === null || x === undefined ? "–" : `${(x * 100).toFixed(d)}%`);
const num = (x, d = 0) => (x === null || x === undefined ? "–" : Number(x).toFixed(d));
const money = (x) => `$${(x || 0).toFixed(x >= 1 ? 2 : 4)}`;
const kfmt = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n || 0));

async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const resp = await fetch(path, opts);
  if (!resp.ok) throw new Error(`${path}: ${resp.status}`);
  return resp.json();
}

const S = {
  settings: {}, faults: {}, scenarios: {}, stages: [], lastSeq: 0, replaying: true, hadError: false,
  tele: null, hist: {}, memHist: [], incident: null, lastResult: null,
  hyp: {}, act: {}, toolsCard: null, verifyCard: null,
  roots: new Set(), symptoms: new Set(), fixed: new Set(), pending: new Set(),
  currentApproval: null, chaosActive: {}, pmLang: "en", pmCurrent: null,
  bench: null,
};

const SERVICES = ["gateway", "auth", "inventory", "orders", "payments", "psp", "fulfillment", "catalog-sync"];
const STAGE_LABEL = { detect: "Detect", triage: "Triage", investigate: "Investigate", hypothesize: "Hypothesize",
  skeptic: "Skeptic", act: "Act", verify: "Verify", report: "Report" };
const ROUTE_LABEL = { core: "Core engine", claude: "LLM", memory: "Memory", baseline: "Runbook baseline" };
const llm = () => S.settings.llm_name || "LLM";
const routeLabel = (r) => (r === "claude" ? llm() : ROUTE_LABEL[r] || r || "–");
const costText = (x) => (S.settings.price_known === false ? "n/a" : money(x));

/* ================================================================ topology */

const NODES = {
  traffic: { x: 300, y: 34, label: "Shoppers", kind: "users" },
  gateway: { x: 300, y: 112, label: "Gateway" },
  auth: { x: 66, y: 214, label: "Auth" },
  payments: { x: 194, y: 214, label: "Payments" },
  inventory: { x: 322, y: 214, label: "Inventory" },
  orders: { x: 450, y: 214, label: "Orders" },
  events: { x: 588, y: 214, label: "Order events", kind: "queue" },
  fulfillment: { x: 588, y: 312, label: "Fulfillment" },
  carrier: { x: 588, y: 404, label: "Freight carrier", kind: "external" },
  "catalog-sync": { x: 194, y: 404, label: "Catalog sync" },
  supplier: { x: 62, y: 404, label: "Supplier catalog", kind: "external" },
  psp: { x: 104, y: 336, label: "PSP (mada)", kind: "external" },
  redis: { x: 262, y: 336, label: "Redis" },
  postgres: { x: 420, y: 396, label: "Postgres" },
};
const LINK = { x: 420, y: 304 };
const BW = 112, BH = 46;
const EDGES = [
  ["traffic", "gateway"], ["gateway", "auth"], ["gateway", "payments"], ["gateway", "inventory"], ["gateway", "orders"],
  ["payments", "psp"], ["payments", "redis"], ["inventory", "redis"], ["inventory", "postgres"],
  ["orders", "orders-db-link"], ["orders-db-link", "postgres"],
  ["orders", "events"], ["events", "fulfillment"], ["fulfillment", "carrier"], ["fulfillment", "postgres"],
  ["catalog-sync", "supplier"], ["catalog-sync", "postgres"],
];
const topoEls = { nodes: {}, edges: [], metrics: {}, tags: null, standby: null };

function anchor(name, end) {
  if (name === "orders-db-link") return { x: LINK.x, y: LINK.y + (end === "top" ? -10 : 10) };
  const n = NODES[name];
  if (end === "left" || end === "right") return { x: n.x + (end === "left" ? -BW / 2 : BW / 2), y: n.y };
  return { x: n.x, y: n.y + (end === "top" ? -BH / 2 : BH / 2) };
}

function edgePath(a, b) {
  const A = a === "orders-db-link" ? LINK : NODES[a], B = b === "orders-db-link" ? LINK : NODES[b];
  if (Math.abs(A.x - B.x) > 1.5 * Math.abs(A.y - B.y)) {  // side by side: join the facing sides
    const p1 = anchor(a, A.x < B.x ? "right" : "left"), p2 = anchor(b, A.x < B.x ? "left" : "right");
    const mx = (p1.x + p2.x) / 2;
    return `M${p1.x} ${p1.y} C${mx} ${p1.y}, ${mx} ${p2.y}, ${p2.x} ${p2.y}`;
  }
  if (a === "fulfillment" && b === "postgres") {
    const p1 = anchor(a, "left"), p2 = anchor(b, "right");
    return `M${p1.x} ${p1.y} C${p1.x - 22} ${p1.y}, ${p2.x + 26} ${p2.y - 30}, ${p2.x} ${p2.y}`;
  }
  const p1 = anchor(a, "bottom"), p2 = anchor(b, "top");
  const my = (p1.y + p2.y) / 2;
  return `M${p1.x} ${p1.y} C${p1.x} ${my}, ${p2.x} ${my}, ${p2.x} ${p2.y}`;
}

function buildTopology() {
  const svg = $("#topo");
  svg.replaceChildren();
  const edgeLayer = s("g");
  for (const [a, b] of EDGES) {
    const path = s("path", { class: "edge flow", d: edgePath(a, b) });
    edgeLayer.appendChild(path);
    topoEls.edges.push({ a, b, el: path });
  }
  const ox = NODES.orders.x + 30, oy = NODES.orders.y + BH / 2;
  const standby = s("path", { class: "edge standby", d: `M${ox} ${oy} C ${ox + 6} 300, ${NODES.postgres.x + 40} 340, ${NODES.postgres.x + 34} ${NODES.postgres.y - BH / 2}` });
  edgeLayer.appendChild(standby);
  topoEls.standby = standby;
  edgeLayer.appendChild(s("text", { class: "edge-label", x: ox + 12, y: 300, text: "standby" }));
  edgeLayer.appendChild(s("text", { class: "edge-label", x: LINK.x - 58, y: LINK.y + 4, text: "primary" }));
  svg.appendChild(edgeLayer);

  const link = s("g", { class: "link-node" }, s("path", { d: `M${LINK.x} ${LINK.y - 10} L${LINK.x + 10} ${LINK.y} L${LINK.x} ${LINK.y + 10} L${LINK.x - 10} ${LINK.y} Z` }));
  const linkMetric = s("text", { class: "edge-label", x: LINK.x - 58, y: LINK.y + 16, text: "" });
  link.appendChild(linkMetric);
  svg.appendChild(link);
  topoEls.nodes["orders-db-link"] = link;
  topoEls.metrics["orders-db-link"] = linkMetric;

  for (const [name, n] of Object.entries(NODES)) {
    const g = s("g", { class: `node ${n.kind || ""}`, transform: `translate(${n.x - BW / 2} ${n.y - BH / 2})` });
    const rx = n.kind === "queue" ? BH / 2 : 9;
    g.appendChild(s("rect", { class: "ring", x: -3, y: -3, width: BW + 6, height: BH + 6, rx: rx + 2 }));
    g.appendChild(s("rect", { class: "box", width: BW, height: BH, rx }));
    g.appendChild(s("circle", { class: "sdot", cx: 14, cy: 15, r: 4 }));
    g.appendChild(s("text", { class: "label", x: 24, y: 19, text: n.label }));
    const metric = s("text", { class: "metric", x: 24, y: 35, text: "" });
    g.appendChild(metric);
    svg.appendChild(g);
    topoEls.nodes[name] = g;
    topoEls.metrics[name] = metric;
  }
  topoEls.tags = s("g");
  svg.appendChild(topoEls.tags);
}

function nodeMetric(name, t) {
  const sv = t.services[name];
  const inv = t.mizan?.invariants || {};
  if (name === "traffic") return `${num(t.services.gateway?.rps, 1)} req/s`;
  if (name === "events") {
    const c = inv.conserved || {}, p = inv.progress || {};
    if (c.ok === false) return `${c.violations} lost`;
    return p.pending === undefined ? "" : `${p.pending} waiting`;
  }
  if (name === "fulfillment") {
    const p = inv.progress || {};
    if (t.health[name] === "down") return "not running";
    if (p.stuck) return `stuck ${Math.round(p.stuck.age_s)} s`;
    return p.pending ? `oldest ${Math.round(p.oldest_wait_s || 0)} s` : "idle";
  }
  if (name === "carrier" || name === "supplier") return "external";
  if (name === "catalog-sync") {
    const p = inv.prices || {};
    return p.ok === false ? `${p.violations} prices off ${p.worst_pct}%` : "in sync";
  }
  if (name === "inventory" && inv.prices?.ok === false && t.health[name] !== "down") return `${inv.prices.violations} wrong prices`;
  if (name === "redis") {
    const r = t.infra.redis;
    return r.ok ? `${num(r.used_mb, 1)} / ${num(r.max_mb)} MB` : "unreachable";
  }
  if (name === "postgres") {
    const p = t.infra.postgres;
    return p.ok ? `${p.connections}/${p.max_connections - p.reserved_for_admin} conns${p.blocked_queries ? ` · ${p.blocked_queries} blocked` : ""}` : "no connections";
  }
  if (!sv) return "";
  if (t.health[name] === "down") return "not running";
  return `${pct(sv.err)} · ${sv.p95_ms ?? "–"} ms`;
}

function updateTopology(t) {
  for (const [name, el] of Object.entries(topoEls.nodes)) {
    const st = t.health[name] || "ok";
    const base = name === "orders-db-link" ? "link-node" : `node ${NODES[name]?.kind || ""}`;
    const marks = [S.roots.has(name) ? "root" : "", S.symptoms.has(name) ? "symptom" : ""].join(" ");
    el.setAttribute("class", `${base} ${st} ${marks}`.trim());
    if (name === "orders-db-link") {
      const l = t.infra.link;
      topoEls.metrics[name].textContent = l.primary_p95_ms ? `${l.primary_p95_ms} ms` : "";
    } else {
      topoEls.metrics[name].textContent = nodeMetric(name, t);
    }
  }
  for (const e of topoEls.edges) {
    const st = t.health[e.b] || "ok";
    e.el.setAttribute("class", `edge ${st === "ok" ? "flow" : st}`);
  }
  const standbyActive = t.infra.link.standby_p95_ms !== null && t.infra.link.standby_p95_ms !== undefined;
  topoEls.standby.setAttribute("class", `edge standby${standbyActive ? " active" : ""}`);
  for (const e of topoEls.edges) {
    if (standbyActive && (e.b === "orders-db-link" || e.a === "orders-db-link") && t.health["orders-db-link"] === "ok") {
      e.el.setAttribute("class", "edge");
    }
  }
}

function renderTags() {
  topoEls.tags.replaceChildren();
  const add = (name, kind, text) => {
    if (name !== "orders-db-link" && !NODES[name]) return;
    const pos = name === "orders-db-link" ? { x: LINK.x - 44, y: LINK.y - 30 } : { x: NODES[name].x - BW / 2, y: NODES[name].y - BH / 2 - 16 };
    const w = text.length * 6.3 + 10;
    topoEls.tags.appendChild(s("g", { class: `tag ${kind}`, transform: `translate(${pos.x} ${pos.y})` },
      s("rect", { width: w, height: 13, rx: 3 }), s("text", { x: 5, y: 9.5, text })));
  };
  for (const n of S.fixed) add(n, "fixed", "FIXED ✓");
  for (const n of S.roots) if (!S.fixed.has(n)) add(n, "root", "ROOT CAUSE");
  for (const n of S.symptoms) if (!S.roots.has(n) && !S.fixed.has(n)) add(n, "symptom", "symptom");
  if (S.tele) updateTopology(S.tele);
}

/* ================================================================ chaos panel */

function buildChaos() {
  const button = (id, f) => h("button", { class: `fault${f.held_out ? " held" : ""}${f.silent ? " silent" : ""}`, "data-fault": id,
    title: f.blurb, onclick: () => injectFault(id) },
    h("span", { class: "ft", text: f.title }));
  const entries = Object.entries(S.faults);
  const silent = entries.filter(([, f]) => f.silent);
  const loud = entries.filter(([, f]) => !f.silent && !f.held_out && !f.benign && !f.blind);
  const benign = entries.filter(([, f]) => f.benign);
  const held = entries.filter(([, f]) => f.held_out);
  const blind = entries.filter(([, f]) => f.blind);
  const section = (cls, title, hint, items) => h("div", { class: `fsec ${cls}` },
    h("div", { class: "fsec-head" }, h("b", { text: title }), h("span", { text: hint })),
    h("div", { class: "fsec-grid" }, items.map(([id, f]) => button(id, f))));
  $("#fault-grid").replaceChildren(
    silent.length ? section("silent", "Silent failures", "No alert will fire. Only Mizan sees these.", silent) : null,
    section("loud", "Loud failures", "Alerts fire; Nabd finds the real cause.", loud),
    benign.length ? section("benign", "Not a fault", "Load alone must not wake Nabd up: the right outcome is nothing.", benign) : null);
  $("#held-grid").replaceChildren(
    blind.length ? section("held", "Blind faults", "Written by a teammate in a file we never opened.", blind) : null,
    held.length ? section("held", "Held-out faults",
      S.settings.claude_available ? `No signature exists, so these go to ${llm()}.`
        : "No signature and no API key: Nabd should escalate, not guess.", held) : null);
  const sel = $("#scenario-select");
  sel.replaceChildren(...Object.entries(S.scenarios).map(([id, sc]) =>
    h("option", { value: id, text: `${sc.title} (${sc.faults.length} faults)` })));
}

function markChaos(active) {
  S.chaosActive = active || {};
  for (const el of $$(".fault")) {
    const a = S.chaosActive[el.dataset.fault];
    el.classList.toggle("active", Boolean(a && !a.hidden));
  }
}

function menu(buttonSel, menuSel) {
  const btn = $(buttonSel), box = $(menuSel);
  btn.onclick = (e) => {
    e.stopPropagation();
    const opening = box.hidden;
    closeMenus();
    box.hidden = !opening;
    btn.setAttribute("aria-expanded", String(opening));
  };
  box.onclick = (e) => e.stopPropagation();
}

function closeMenus() {
  for (const box of $$(".popover")) box.hidden = true;
  for (const btn of $$("[aria-controls]")) btn.setAttribute("aria-expanded", "false");
}

async function injectFault(id) {
  closeMenus();
  try { await api("/api/chaos/inject", { fault: id }); refreshState(); } catch (e) { sysRow(`Inject failed: ${e.message}`); }
}

/* ================================================================ feed */

function feed() { return $("#feed"); }
function append(el) {
  const f = feed();
  const near = f.scrollHeight - f.scrollTop - f.clientHeight < 160;
  $(".idle-card", f)?.remove();
  f.appendChild(el);
  while (f.children.length > 500) f.firstElementChild.remove();
  if (near || S.replaying) f.scrollTop = f.scrollHeight;
  return el;
}
function rel(ts) { return S.incident ? `+${fmtDur(ts - S.incident.opened)}` : fmtTime(ts); }
function row(ev, text, cls = "") {
  S.toolsCard = null;
  return append(h("div", { class: `row ${cls}` }, h("span", { class: "t", text: rel(ev.ts) }), h("span", { class: "txt", text })));
}
function sysRow(text) { return append(h("div", { class: "row sys" }, h("span", { class: "t", text: fmtTime(Date.now() / 1000) }), h("span", { class: "txt", text }))); }
function card(cls, kicker, title, ...body) {
  S.toolsCard = null;
  return append(h("div", { class: `card ${cls}` },
    h("div", { class: "head" }, kicker ? h("span", { class: "kicker", text: kicker }) : null, title ? h("span", { class: "title", text: title }) : null),
    ...body));
}

function idleCard() {
  const last = S.lastResult;
  return h("div", { class: "idle-card" },
    s("svg", { class: "pulse-big", viewBox: "0 0 160 48" },
      s("path", { d: "M0 24 H52 L60 24 L66 8 L74 40 L80 16 L86 24 H160", fill: "none", stroke: "var(--ok)", "stroke-width": 2.2, "stroke-linejoin": "round" })),
    h("h3", { text: "Nabd is watching Dukkan" }),
    h("p", { text: "Break the store from the panel on the right. Nabd finds the cause, fixes it safely, repairs the damage and proves the store recovered." }),
    last ? h("p", { class: "hint", text: `Last: ${last.id} ${last.status} in ${fmtDur(last.mttr_s)}` }) : null);
}

function renderStepper(stage) {
  const idx = S.stages.indexOf(stage);
  $("#stepper").replaceChildren(...S.stages.map((st, i) =>
    h("li", { class: idx < 0 ? "" : i < idx ? "done" : i === idx ? "active" : "" }, h("div", { class: "bar" }), h("span", { text: STAGE_LABEL[st] || st }))));
}

function renderIncidentHead() {
  const el = $("#incident-head");
  const inc = S.incident;
  if (!inc) {
    el.replaceChildren(h("span", { class: "meta", text: "No open incident." }),
      h("span", { class: "spacer" }),
      S.lastResult ? h("span", { class: "cost", text: `Last: ${S.lastResult.id} · ${S.lastResult.status} · ${fmtDur(S.lastResult.mttr_s)}` }) : null);
    return;
  }
  const b = inc.budget;
  const budget = b ? h("span", { class: `budget${b.spent ? " spent" : ""}`, title: "The escalation contract: past any limit Nabd stops and hands over to a human" },
    "Autonomy ", h("b", { text: `${b.actions}/${b.limits.actions}` }), " actions · ",
    h("b", { text: `${kfmt(Math.round(b.money_sar))}/${kfmt(b.limits.money_sar)}` }), " SAR · ",
    h("b", { text: `${b.limits.minutes}` }), " min max") : null;
  el.replaceChildren(
    h("span", { class: "inc-id", text: inc.id }),
    inc.kind === "predictive" ? h("span", { class: "chip memory", text: "predictive" }) : null,
    inc.route ? h("span", { class: `chip ${inc.route}`, text: routeLabel(inc.route) }) : null,
    h("span", { class: "meta", text: `${inc.alerts || 0} alerts` }),
    budget,
    h("span", { class: "spacer" }),
    h("span", { class: "cost" }, "LLM ", h("b", { text: String(inc.llmCalls || 0) }), " calls · ",
      h("b", { text: kfmt((inc.tokIn || 0) + (inc.tokOut || 0)) }), " tokens · ", h("b", { text: costText(inc.cost || 0) })));
}

function toolsCardFor(engine, role) {
  const key = `${engine}|${role || ""}`;
  if (S.toolsCard && S.toolsCard.key === key && S.toolsCard.el.isConnected && feed().lastElementChild === S.toolsCard.el) return S.toolsCard;
  // the investigation is one quiet line; the tool calls are there for whoever wants them
  const list = h("div", { class: "tools" });
  const label = h("span", { class: "kicker" });
  const fold = h("button", { class: "fold", text: "Show" });
  const el = append(h("div", { class: `card tools-card${engine === "claude" ? " claude" : ""}` }, h("div", { class: "head" }, label, fold), list));
  fold.onclick = () => { el.classList.toggle("open"); fold.textContent = el.classList.contains("open") ? "Hide" : "Show"; };
  S.toolsCard = { key, el, list, rows: [], label, who: engine === "claude" ? `${llm()} (${role || "investigator"})` : "Nabd" };
  return S.toolsCard;
}

function argText(args) {
  const vals = Object.values(args || {}).map((v) => (typeof v === "string" ? v : JSON.stringify(v)));
  const t = vals.join(", ");
  return t.length > 60 ? `${t.slice(0, 57)}…` : t;
}

function onToolCall(ev) {
  const tc = toolsCardFor(ev.engine, ev.role);
  const r = h("div", { class: `tool pending ${ev.engine}` },
    h("span", { class: "ic", text: "›" }), h("span", { class: "nm", text: `${ev.tool}(${argText(ev.args)})` }),
    h("span", { class: "br" }), h("span", { class: "ms" }));
  r.dataset.key = `${ev.tool}|${JSON.stringify(ev.args)}`;
  tc.list.appendChild(r);
  tc.rows.push(r);
  tc.label.textContent = `${tc.who} checked ${tc.rows.length} source${tc.rows.length > 1 ? "s" : ""}`;
  const f = feed();
  if (f.scrollHeight - f.scrollTop - f.clientHeight < 200) f.scrollTop = f.scrollHeight;
}

function onToolResult(ev) {
  const key = `${ev.tool}|${JSON.stringify(ev.args)}`;
  const rowEl = $$(".tool.pending", feed()).find((r) => r.dataset.key === key);
  if (!rowEl) return;
  rowEl.classList.remove("pending");
  $(".br", rowEl).textContent = ev.brief;
  $(".ms", rowEl).textContent = `${ev.ms} ms`;
}

function hypCard(hy) {
  const claude = hy.source === "claude";
  const ev = hy.evidence || [];
  const evidence = h("ul", { class: "evidence" }, ev.slice(0, 2).map((e) => h("li", { text: e })));
  const rest = ev.length > 2 ? h("ul", { class: "evidence more-ev" }, ev.slice(2, 8).map((e) => h("li", { text: e }))) : null;
  const expl = hy.explanation ? h("div", { class: "sub more-ev", text: hy.explanation }) : null;
  const action = hy.action && hy.action.name !== "none"
    ? h("div", { class: "proposal" }, hy.phase === "heal" ? "Repair: " : "Fix: ", h("b", { text: describeAction(hy.action) }))
    : h("div", { class: "proposal", text: "No automatic fix: a human takes this one" });
  const skep = h("div", { class: "skeptic", hidden: true });
  const more = `Why? Show all ${ev.length} clues`;
  const fold = rest || expl ? h("button", { class: "fold", text: more }) : null;
  const el = card(`hyp${claude ? " claude" : ""}${hy.phase === "heal" ? " heal" : ""}${hy.mizan ? " mizan" : ""}`, null, null,
    h("div", { class: "head" }, h("span", { class: "hid", text: hy.id }),
      hy.phase === "heal" ? h("span", { class: "phase", text: "repair the books" }) : hy.mizan ? h("span", { class: "phase", text: "found by Mizan" }) : null,
      h("span", { class: "title", text: hy.title })),
    h("div", { class: "conf" }, h("div", { class: "track" }, h("div", { class: "fill", style: `width:${Math.round(hy.confidence * 100)}%` })),
      `${Math.round(hy.confidence * 100)}% confident`, h("span", { class: "nchip", text: hy.node })),
    evidence, rest, expl, action, skep, fold);
  el.firstElementChild.remove();
  if (fold) fold.onclick = () => { el.classList.toggle("open"); fold.textContent = el.classList.contains("open") ? "Show less" : more; };
  S.hyp[hy.id] = { el, skep };
}

function describeAction(a) {
  const target = a.params?.service || a.params?.application_name || a.params?.prefix || "";
  return `${a.name.replaceAll("_", " ")}${target ? ` (${target})` : ""}`;
}

function onSkeptic(ev) {
  const c = S.hyp[ev.hypothesis_id];
  if (!c) return;
  c.skep.hidden = false;
  c.skep.replaceChildren(h("div", { title: ev.reasoning },
    h("span", { class: `verdict ${ev.verdict}`, text: `skeptic: ${ev.verdict}` }), " ", ev.challenge));
  if (ev.verdict === "refuted") c.el.style.opacity = "0.55";
}

function actionCard(a) {
  const status = h("span", { class: `astatus ${a.status}`, text: a.status.replaceAll("_", " ") });
  const result = h("div", { class: "sub", hidden: !a.result, text: a.result || "" });
  const checks = h("div", { class: "pills" });
  const title = h("span", { class: "title", text: a.description });
  const el = card("action", null, null,
    h("div", { class: "head" }, title, a.risk ? h("span", { class: `risk ${a.risk}`, text: `${a.risk} risk` }) : null, status),
    checks, result);
  el.firstElementChild.remove();
  S.act[a.id] = { el, status, result, checks, title };
  renderGate(a);
}

function renderGate(a) {
  const c = S.act[a.id];
  if (!c) return;
  // every gate is one pill; hover it for the exact reason
  const pill = (cls, label, detail) => h("span", { class: `pill ${cls}`, title: detail || "" }, label);
  const blockedBy = a.status === "blocked" ? a.blocked_by : null;
  c.title.textContent = a.description;
  c.checks.replaceChildren(
    a.corrected_from ? pill("fix", "↺ Corrected", `from “${a.corrected_from}”`) : null,
    a.validity ? pill("ok", "✓ Right fix for this cause", a.validity)
      : blockedBy === "Validity gate" ? pill("no", "✕ Wrong fix for this cause", a.result) : null,
    a.preflight ? (blockedBy === "Dry run" ? pill("no", "✕ Dry run failed", a.preflight) : pill("ok", "✓ Dry run", a.preflight)) : null,
    a.approval_reason ? pill("money", "⚖ Above the money limit", a.approval_reason) : null,
    blockedBy === "Safety gate" ? pill("no", "✕ Safety gate", a.result) : null,
    blockedBy === "Autonomy budget" ? pill("no", "✕ Budget spent", a.result) : null);
}

function updateAction(a) {
  if (!S.act[a.id]) actionCard(a);
  renderGate(a);
  const c = S.act[a.id];
  c.status.className = `astatus ${a.status}`;
  c.status.textContent = a.status.replaceAll("_", " ") + (a.approved_by && a.status === "done" ? ` · ${a.approved_by}` : "");
  if (a.result) { c.result.hidden = false; c.result.textContent = a.result; }
}

function onVerify(ev) {
  const labels = { journey: "Shopper checks out", gateway_slo: "Inside the SLO", alerts_clear: "Alerts clear",
    nodes_healthy: "Nodes healthy", books_balanced: "Books balance", early_exit: "Stopped early" };
  const pills = h("div", { class: "pills" }, Object.entries(ev.checks).map(([k, c]) =>
    h("span", { class: `pill ${c.ok ? "ok" : "wait"}`, title: c.detail }, `${c.ok ? "✓" : "…"} ${labels[k] || k}`)));
  const passes = h("div", { class: "passes" }, [0, 1, 2].map((i) => h("i", { class: i < ev.passes ? "on" : "" })));
  const content = [h("div", { class: "head" }, h("span", { class: "title", text: `Proving recovery: ${ev.passes} of 3 clean checks` }), passes), pills];
  if (S.verifyCard && S.verifyCard.isConnected && S.verifyCard === feed().lastElementChild) {
    S.verifyCard.replaceChildren(...content);
  } else {
    S.toolsCard = null;
    S.verifyCard = append(h("div", { class: "card" }, ...content));
  }
}

/* ================================================================ status */

function setStatus() {
  const el = $("#status");
  const txt = $("#status-text");
  document.body.classList.remove("state-incident", "state-recovering");
  if (!S.connected) { el.className = "status idle"; txt.textContent = "Reconnecting to Nabd…"; return; }
  const inc = S.incident;
  if (inc) {
    const verifying = inc.stage === "verify";
    el.className = `status ${verifying ? "warn" : "crit"}`;
    document.body.classList.add(verifying ? "state-recovering" : "state-incident");
    txt.textContent = verifying ? `${inc.id} · verifying recovery` : `${inc.id} · ${STAGE_LABEL[inc.stage] || inc.stage}`;
    return;
  }
  const last = S.lastResult;
  if (last && Date.now() / 1000 - last.closedAt < 30) {
    el.className = `status ${last.status === "resolved" || last.status === "transient" ? "ok" : "crit"}`;
    txt.textContent = last.status === "resolved" ? `${last.id} recovered in ${fmtDur(last.mttr_s)}`
      : last.status === "transient" ? `${last.id} closed as transient` : `${last.id} escalated to on-call`;
    return;
  }
  el.className = "status ok";
  txt.textContent = "All systems nominal";
}

setInterval(() => {
  const clock = $("#status-clock");
  clock.textContent = S.incident ? `T+${fmtDur(Date.now() / 1000 - S.incident.opened)}` : "";
  setStatus();
}, 1000);

/* ================================================================ events */

const handlers = {
  telemetry: (ev) => updateTelemetry(ev.data),
  replay_done: () => {
    S.replaying = false;
    if (!S.incident && !$(".idle-card", feed())) feed().appendChild(idleCard());
    for (const id of S.pending) {
      const ev = S.approvalEvents?.[id];
      if (ev) showApproval(ev);
    }
  },
  info: (ev) => sysRow(ev.title),
  settings: (ev) => applySettings(ev.settings),
  chaos: (ev) => { row(ev, ev.title, "chaos"); refreshState(); },
  benchmark: (ev) => onBenchmark(ev),
  incident_opened: (ev) => {
    S.incident = { id: ev.incident, opened: ev.ts, kind: ev.kind, stage: "detect", alerts: (ev.alerts || []).length, cost: 0, llmCalls: 0, tokIn: 0, tokOut: 0,
      budget: { actions: 0, money_sar: 0, limits: S.settings.budget || { actions: 6, money_sar: 10000, minutes: 15 } } };
    S.roots.clear(); S.symptoms.clear(); S.fixed.clear(); S.hyp = {}; S.act = {}; renderTags();
    S.toolsCard = null;
    append(h("div", { class: "divider", text: `${ev.incident} · opened ${fmtTime(ev.ts)}` }));
    row(ev, ev.title);
    renderStepper("detect"); renderIncidentHead();
  },
  stage: (ev) => { if (S.incident) S.incident.stage = ev.stage; renderStepper(ev.stage); setStatus(); },
  alert_storm: (ev) => {
    if (S.incident) S.incident.alerts = ev.count;
    const shown = ev.alerts.slice(0, 5);
    card("storm", "Alerts", `${ev.count} alert${ev.count > 1 ? "s" : ""} across ${ev.services.length} service${ev.services.length > 1 ? "s" : ""}`,
      h("div", { class: "alert-chips" }, shown.map((a) =>
        h("span", { class: `achip ${a.severity}`, title: a.summary, text: `${a.name} on ${a.service}` })),
        ev.alerts.length > shown.length ? h("span", { class: "hint", text: `and ${ev.alerts.length - shown.length} more` }) : null));
    renderIncidentHead();
  },
  triage: (ev) => row(ev, ev.title),
  tool_call: onToolCall,
  tool_result: onToolResult,
  thinking: (ev) => {
    const body = h("div", { class: "thinking", text: ev.text });
    const more = h("button", { class: "more", text: "show all", onclick: () => { body.classList.toggle("open"); more.textContent = body.classList.contains("open") ? "show less" : "show all"; } });
    card("claude", `${llm()} · ${ev.role} · thinking`, null, body, ev.text.length > 380 ? more : null);
  },
  llm_text: (ev) => card("claude", `${llm()} · ${ev.role}`, null, h("div", { class: "sub", text: ev.text })),
  llm_call: (ev) => {
    if (S.incident) {
      S.incident.llmCalls = (S.incident.llmCalls || 0) + 1;
      S.incident.tokIn = (S.incident.tokIn || 0) + ev.tokens.in + (ev.tokens.cache_read || 0);
      S.incident.tokOut = (S.incident.tokOut || 0) + ev.tokens.out;
      S.incident.cost = ev.cost;
      renderIncidentHead();
    }
  },
  router: (ev) => {
    if (S.incident) { S.incident.route = ev.route; renderIncidentHead(); }
    card("router", "Router", null, h("div", { class: "head" }, h("span", { class: `chip ${ev.route}`, text: routeLabel(ev.route) }), h("span", { class: "sub", text: ev.reason })));
  },
  memory_hit: (ev) => card("memory", "Incident memory", ev.title, h("div", { class: "sub", text: `Last time this was fixed in ${fmtDur(ev.match.mttr_s)}.` })),
  hypothesis: (ev) => hypCard(ev.hypothesis),
  suspect: (ev) => card("bad", "Suspect", ev.item, h("div", { class: "sub", text: ev.reason })),
  ruled_out: (ev) => row(ev, `Ruled out ${ev.item}: ${ev.reason}`),
  clusters: (ev) => {
    S.roots = new Set(ev.roots); S.symptoms = new Set(ev.symptoms); renderTags();
    // a repair to the books sits on the same component as its cause: count distinct components only
    const roots = [...new Map(ev.clusters.map((c) => [c.root, c])).values()];
    if (roots.length > 1) {
      card("", "Triage", `${roots.length} independent root causes`, h("div", { class: "chips" },
        roots.map((c) => h("span", { class: "nchip", text: c.symptoms.length ? `${c.root}, hurting ${c.symptoms.join(", ")}` : c.root }))));
    }
  },
  skeptic: onSkeptic,
  plan: (ev) => card("", "Plan", ev.title, h("ol", { class: "evidence" }, ev.steps.map((st) => h("li", {},
    st.phase === "heal" ? h("span", { class: "phase", text: "repair" }) : h("span", { class: "phase fix", text: "fix" }), ` ${st.action}  (${st.hypothesis})`)))),
  settle: (ev) => row(ev, ev.title, "settle"),
  handoff: (ev) => card("handoff", "Handed to a human", ev.handoff.what,
    h("div", { class: "sub", text: ev.handoff.why }), ev.handoff.ask ? h("div", { class: "ask", text: `Ask: ${ev.handoff.ask}` }) : null),
  budget: (ev) => { if (S.incident) { S.incident.budget = ev.budget; renderIncidentHead(); } },
  action_proposed: (ev) => actionCard(ev.action),
  action_blocked: (ev) => { updateAction(ev.action); },
  action_corrected: (ev) => { updateAction(ev.action); row(ev, ev.title); },
  action_skipped: (ev) => updateAction(ev.action),
  approval_required: (ev) => {
    updateAction(ev.action);
    S.pending.add(ev.approval_id);
    S.approvalEvents = S.approvalEvents || {};
    S.approvalEvents[ev.approval_id] = ev;
    if (!S.replaying) showApproval(ev);
  },
  approval_result: (ev) => {
    S.pending.delete(ev.approval_id);
    if (S.currentApproval === ev.approval_id) hideApproval();
    row(ev, ev.title);
  },
  action_running: (ev) => updateAction(ev.action),
  action_result: (ev) => {
    updateAction(ev.action);
    if (ev.action.status === "done") {
      const node = ev.action.name === "flush_redis_keys" ? "redis" : ev.action.name === "terminate_db_sessions" ? "postgres"
        : ev.action.name === "switch_db_route" ? "orders-db-link" : ev.action.params.service;
      if (node) { S.fixed.add(node); renderTags(); }
    }
  },
  action_rejected: (ev) => updateAction(ev.action),
  guard_reverted: (ev) => { updateAction(ev.action); card("bad", "Guard", ev.title); },
  guard_regression: (ev) => { updateAction(ev.action); card("bad", "No-regression guard", ev.title); },
  verify_progress: onVerify,
  retry: (ev) => card("bad", "Retry", ev.title),
  recovered: (ev) => {
    card("ok", null, null, h("div", { class: "big-result" }, h("span", { class: "num", text: fmtDur(ev.mttr_s) }),
      h("div", {}, h("div", { class: "title", text: "Recovered and verified" }), h("div", { class: "sub", text: "Measured from the first fault to three clean synthetic checkouts." }))));
  },
  escalated: (ev) => card("bad", "Escalated", ev.title),
  transient: (ev) => card("ok", "Transient", ev.title),
  postmortem_ready: (ev) => {
    card("", "Report", ev.title, h("div", {}, h("button", { class: "btn", text: "Open postmortem", onclick: () => openPostmortem(ev.postmortem_id) })));
  },
  incident_closed: (ev) => {
    const sm = ev.summary;
    S.lastResult = { id: sm.id, status: sm.status, mttr_s: sm.mttr_s, closedAt: ev.ts };
    S.incident = null;
    S.roots.clear(); S.symptoms.clear(); renderTags();
    renderStepper(null); renderIncidentHead(); setStatus();
    if (S.tele) renderMizan(S.tele);
    if (!S.replaying) setTimeout(() => { S.fixed.clear(); renderTags(); }, 15000);
    loadPmList();
  },
};

/* Stage mode: the same War Room with only the story on screen, for a short live demo. Open /?stage or press S;
   the choice is remembered in this browser. The working steps still happen, they are just not drawn. */
const STAGE_QUIET = new Set(["triage", "tool_call", "tool_result", "thinking", "llm_text", "llm_call", "router", "memory_hit",
  "suspect", "ruled_out", "clusters", "skeptic", "plan", "settle", "info", "alert_storm", "action_corrected",
  "approval_result", "chaos"]);
function readStage() {
  try { return new URLSearchParams(location.search).has("stage") || localStorage.getItem("nabd-stage") === "1"; }
  catch { return false; }
}
function setStage(on) {
  S.stage = on;
  document.body.classList.toggle("stage", on);
  try { localStorage.setItem("nabd-stage", on ? "1" : "0"); } catch { /* private window: this visit only */ }
}

function handle(ev) {
  if (ev.seq && ev.seq <= S.lastSeq) return;
  if (ev.seq) S.lastSeq = ev.seq;
  if (S.stage && STAGE_QUIET.has(ev.type)) {
    if (ev.type === "chaos") refreshState();
    if (ev.type === "alert_storm" && S.incident) S.incident.alerts = ev.count;
    if (ev.type === "approval_result") {  // the card must still close, wherever the answer came from
      S.pending.delete(ev.approval_id);
      if (S.currentApproval === ev.approval_id) hideApproval();
    }
    return;
  }
  const fn = handlers[ev.type];
  if (fn) {
    try { fn(ev); } catch (e) { console.error(ev.type, e); }
  }
}

function connect() {
  const es = new EventSource("/api/stream");
  es.onopen = () => {
    S.connected = true;
    if (S.hadError) {
      S.hadError = false; S.lastSeq = 0; S.replaying = true;
      feed().replaceChildren(); S.incident = null;
    }
    setStatus();
  };
  es.onmessage = (m) => { try { handle(JSON.parse(m.data)); } catch (e) { console.error(e); } };
  es.onerror = () => { S.connected = false; S.hadError = true; setStatus(); };
}

/* ================================================================ telemetry */

function spark(canvas, values, color, max) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 100, hgt = canvas.clientHeight || 20;
  if (canvas.width !== w * dpr) { canvas.width = w * dpr; canvas.height = hgt * dpr; }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, hgt);
  const vals = values.filter((v) => v !== null && v !== undefined);
  if (vals.length < 2) return;
  const top = Math.max(max || 0, ...vals) || 1;
  ctx.beginPath();
  values.forEach((v, i) => {
    const x = (i / Math.max(1, values.length - 1)) * w;
    const y = hgt - 2 - ((v || 0) / top) * (hgt - 4);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.stroke();
  ctx.lineTo(w, hgt); ctx.lineTo(0, hgt); ctx.closePath();
  ctx.globalAlpha = 0.15; ctx.fillStyle = color; ctx.fill(); ctx.globalAlpha = 1;
}

function buildServiceList() {
  $("#svc-list").replaceChildren(
    h("div", { class: "svc head" }, h("span"), h("span", { text: "Service" }), h("span", { text: "Trend" }),
      h("span", { class: "m", text: "Errors" }), h("span", { class: "m", text: "p95" }), h("span", { class: "m", text: "req/s" })),
    ...SERVICES.map((n) => h("div", { class: "svc", "data-svc": n },
    h("span", { class: "dot" }), h("span", { class: "n", text: n }), h("canvas", { height: 20 }),
    h("span", { class: "m err" }), h("span", { class: "m p95" }), h("span", { class: "m rps" }))));
}

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function updateTelemetry(t) {
  if (!t) return;
  S.tele = t;
  updateTopology(t);
  const gw = t.services.gateway || {};
  const alerts = t.alerts || [];
  const kpis = [
    ["Requests per second", num(gw.rps, 1), "good"],
    ["Server errors", pct(gw.err), gw.err > 0.05 ? "bad" : gw.err > 0.01 ? "meh" : "good"],
    ["p95 latency", gw.p95_ms === null || gw.p95_ms === undefined ? "–" : `${gw.p95_ms} ms`, gw.p95_ms > 800 ? "bad" : gw.p95_ms > 300 ? "meh" : "good"],
  ];
  $("#kpis").replaceChildren(...kpis.map(([k, v, c]) => h("div", { class: "kpi" }, h("span", { class: "k", text: k }), h("span", { class: `v ${c}`, text: v }))));

  for (const n of SERVICES) {
    const sv = t.services[n] || {};
    const hist = (S.hist[n] = S.hist[n] || { p95: [], err: [] });
    hist.p95.push(sv.p95_ms ?? null); hist.err.push(sv.err ?? null);
    if (hist.p95.length > 90) { hist.p95.shift(); hist.err.shift(); }
    const rowEl = $(`.svc[data-svc="${n}"]`);
    if (!rowEl) continue;
    const st = t.health[n] || "ok";
    $(".dot", rowEl).className = `dot ${st}`;
    const bad = (sv.err || 0) > 0.05;
    $(".err", rowEl).textContent = pct(sv.err); $(".err", rowEl).classList.toggle("bad", bad);
    $(".p95", rowEl).textContent = sv.p95_ms === null || sv.p95_ms === undefined ? "–" : `${sv.p95_ms}ms`;
    $(".rps", rowEl).textContent = num(sv.rps, 1);
    spark($("canvas", rowEl), bad || st !== "ok" ? hist.err : hist.p95, bad || st !== "ok" ? cssVar("--crit") : cssVar("--accent"), bad ? 1 : 300);
  }

  const pay = t.services.payments || {};
  S.memHist.push({ t: t.at, v: pay.mem_mb ?? null });
  if (S.memHist.length > 120) S.memHist.shift();
  drawMemory(pay.mem_limit_mb || 320);

  const r = t.infra.redis, p = t.infra.postgres, l = t.infra.link;
  const meter = (label, value, ratio) => h("div", { class: "meter" },
    h("div", { class: "mh" }, h("span", { text: label }), h("b", { text: value })),
    h("div", { class: "mt" }, h("div", { class: `mf ${ratio >= 0.9 ? "crit" : ratio >= 0.75 ? "warn" : ""}`, style: `width:${Math.min(100, Math.round((ratio || 0) * 100))}%` })));
  $("#infra").replaceChildren(
    meter("Redis memory", r.ok ? `${num(r.used_mb, 1)} / ${num(r.max_mb)} MB` : "unreachable", r.ratio || (r.ok ? 0 : 1)),
    meter("Postgres client connections", p.ok ? `${p.connections} / ${p.max_connections - p.reserved_for_admin}${p.blocked_queries ? ` · ${p.blocked_queries} blocked` : ""}` : "refusing", p.ok ? p.ratio : 1),
    h("div", { class: "meter" }, h("div", { class: "mh" }, h("span", { text: "Orders → DB link p95" }),
      h("b", { text: `primary ${l.primary_p95_ms ?? "–"} ms · standby ${l.standby_p95_ms ?? "–"} ms` }))));

  renderMizan(t);
  $("#alert-count").textContent = alerts.length ? `${alerts.length} firing` : "";
  $("#alerts").replaceChildren(...(alerts.length ? alerts.map((a) => h("li", { class: `${a.severity}${a.source === "mizan" ? " mizan" : ""}` },
    h("span", { class: "an", text: `${a.name}` }), h("span", { class: "as", text: a.summary || a.service }))) : [h("li", {}, h("span", { class: "as", text: "Nothing firing." }))]));
}

/* ---------------------------------------------------------------- the status band (Mizan) */

const sar = (x) => `${Math.round(x || 0).toLocaleString("en-US")} SAR`;
const INV = {  // each of Mizan's books: its name, and what to say when it holds and when it breaks
  money: { name: "Every payment has an order",
    ok: (v) => [String(v.checked ?? 0), "payments matched"],
    bad: (v) => [sar(v.amount_sar), `${v.violations} paid with no order`],
    off: (v) => [`${sar(v.amount_sar)} charged with no order`,
      `${v.violations} customers paid and were told their order exists. It does not.`] },
  fulfilled: { name: "Every order reaches shipping",
    ok: (v) => [String(v.checked ?? 0), "orders on track"],
    bad: (v) => [String(v.violations), "orders will never ship"],
    off: (v) => [`${v.violations} paid orders will never ship`,
      "They are in no queue, and fulfillment never saw them."] },
  conserved: { name: "Every queued order is counted",
    ok: (v) => [String(v.produced ?? 0), "events counted"],
    bad: (v) => [String(v.violations), "events vanished"],
    off: (v) => [`${v.violations} order events vanished`,
      "Sent to the queue, never consumed, never dead-lettered."] },
  progress: { name: "Shipping keeps moving",
    ok: (v) => [String(v.pending ?? 0), v.pending ? "waiting to ship" : "queue clear"],
    bad: (v) => [`${Math.round(v.stuck?.age_s || v.heartbeat_age_s || 0)} s`, "stuck on one order"],
    off: (v) => [`Shipping stuck for ${Math.round(v.stuck?.age_s || v.heartbeat_age_s || 0)} seconds`,
      `${v.pending} orders are waiting behind ${v.stuck ? v.stuck.order_ref : "a silent worker"}.`] },
  prices: { name: "Prices match the supplier",
    ok: (v) => [String(v.checked ?? 0), "prices match"],
    bad: (v) => [String(v.violations), `off by up to ${v.worst_pct}%`],
    off: (v) => [`${v.violations} prices drifted from the supplier`,
      `Our catalog is off by up to ${v.worst_pct}%, and it gets worse on every run.`] },
};
const INV_ORDER = ["money", "fulfilled", "conserved", "progress", "prices"];
const setText = (sel, text) => { const el = $(sel); if (el.textContent !== text) el.textContent = text; };

function renderMizan(t) {
  const inv = t.mizan?.invariants || {};
  const classic = t.classic_alerts ?? (t.alerts || []).filter((a) => a.source !== "mizan").length;
  const broken = INV_ORDER.filter((k) => inv[k]?.ok === false);
  const confirmed = broken.filter((k) => inv[k].confirmed);
  setText("#mz-classic", String(classic));
  setText("#mz-broken", String(confirmed.length + (t.learned?.state === "watching" ? (t.learned.broken ? 1 : 0) + (t.learned.stalled ? 1 : 0) : 0)));

  const L = t.learned || {};
  const lBroken = L.state === "watching" && (L.broken || L.stalled);
  const last = S.lastResult;
  const justRecovered = last && last.status === "resolved" && Date.now() / 1000 - last.closedAt < 30;
  let state = "ok", kicker = "Dukkan, live", title = "Dukkan is healthy",
    sub = "Payments, orders, the queue, shipping and prices all agree.";
  if (confirmed.length) {
    state = "off";
    [title, sub] = INV[confirmed[0]].off(inv[confirmed[0]]);
    kicker = classic ? `Mizan sees broken books, and ${classic} alerts are firing` : "Silent failure: monitoring sees nothing wrong";
  } else if (lBroken) {
    state = "off";
    const w = (L.worst || L.stalls || [])[0] || {};
    const sus = (L.suspects || [])[0];
    title = L.broken ? "A law nobody wrote just broke" : "Work stopped moving";
    sub = `${w.now || ""}${sus ? `. Suspect: ${sus.service}.` : "."}`;
    kicker = classic ? `Mizan's learned laws broke, and ${classic} alerts are firing` : "Silent failure: monitoring sees nothing wrong";
  } else if (classic) {
    state = "loud";
    const a = (t.alerts || []).find((x) => x.source !== "mizan");
    title = `${classic} alert${classic > 1 ? "s" : ""} firing`;
    sub = a?.summary || "";
    kicker = "Loud failure";
  } else if (broken.length) {
    kicker = "Mizan is re-checking";
    title = "Something does not add up";
    sub = "A mismatch must hold on two checks in a row before Nabd treats it as real.";
  } else if (justRecovered) {
    state = "recovered";
    kicker = `${last.id} closed`;
    title = `Recovered in ${fmtDur(last.mttr_s)}`;
    sub = "Verified with three clean checkouts, and the books balance again.";
  }
  // during a failure the band keeps saying what kind it is; the incident's stage is in the header already
  if (S.incident && state === "ok") kicker = `${S.incident.id}: Nabd is on it (${(STAGE_LABEL[S.incident.stage] || S.incident.stage).toLowerCase()})`;
  const hero = $("#hero");
  if (!hero.classList.contains(state)) hero.className = `hero ${state}`;
  setText("#hero-kicker", kicker);
  setText("#hero-title", title);
  setText("#hero-sub", sub);

  const learnedPill = !L.state || L.enabled === false ? null : (() => {
    const st = L.state !== "watching" ? "warn" : lBroken ? "crit" : "ok";
    const big = L.state !== "watching" ? `${Math.round((L.progress || 0) * 100)}%`
      : L.broken ? String(L.broken) : L.stalled ? "stalled" : String(L.laws ?? 0);
    const small = L.state !== "watching" ? "learning from healthy traffic" : L.broken ? `of ${L.laws} laws broken` : L.stalled ? "a consumer stopped" : null;
    return h("li", { class: `learned ${st}`, title: "Pairs of flows that always moved in a fixed ratio while the store was healthy. Nobody wrote them." },
      h("span", { class: "mk" }), h("span", { class: "lk", text: "Laws it learned itself" }),
      h("span", { class: "lv", text: big }), small ? h("span", { class: "ls", text: small }) : null);
  })();
  $("#mz-list").replaceChildren(learnedPill, ...INV_ORDER.filter((k) => inv[k]).map((k) => {
    const v = inv[k];
    const st = v.ok === null || v.ok === undefined ? "unknown" : v.ok ? "ok" : v.confirmed ? "crit" : "warn";
    const [big, small] = st === "unknown" ? ["?", "cannot read"] : v.ok ? INV[k].ok(v) : INV[k].bad(v);
    return h("li", { class: st, title: v.detail },
      h("span", { class: "mk" }),
      h("span", { class: "lk", text: INV[k].name }),
      h("span", { class: "lv", text: big }),
      st === "ok" ? null : h("span", { class: "ls", text: small }));
  }));
}

function drawMemory(limit) {
  const canvas = $("#mem-chart");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 300, hgt = canvas.clientHeight || 120;
  if (canvas.width !== w * dpr) { canvas.width = w * dpr; canvas.height = hgt * dpr; }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, hgt);
  const pts = S.memHist.filter((p) => p.v !== null);
  const top = limit * 1.08;
  const pad = { l: 34, r: 8, t: 8, b: 16 };
  const iw = w - pad.l - pad.r, ih = hgt - pad.t - pad.b;
  const now = Date.now() / 1000;
  const span = 240;
  const X = (ts) => pad.l + ((ts - (now - span * 0.75)) / span) * iw;
  const Y = (v) => pad.t + ih - (v / top) * ih;
  ctx.font = `11px ${cssVar("--sans")}`;
  ctx.fillStyle = cssVar("--faint");
  ctx.strokeStyle = cssVar("--line");
  ctx.lineWidth = 1;
  for (const v of [0, 100, 200, 300]) {
    ctx.beginPath(); ctx.moveTo(pad.l, Y(v)); ctx.lineTo(w - pad.r, Y(v)); ctx.stroke();
    ctx.fillText(`${v}`, 4, Y(v) + 3);
  }
  ctx.strokeStyle = cssVar("--crit"); ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(pad.l, Y(limit)); ctx.lineTo(w - pad.r, Y(limit)); ctx.stroke();
  ctx.setLineDash([]); ctx.fillStyle = cssVar("--crit"); ctx.fillText(`limit ${limit} MB`, w - pad.r - 78, Y(limit) - 4);
  const nowX = X(now);
  ctx.strokeStyle = cssVar("--line-2"); ctx.beginPath(); ctx.moveTo(nowX, pad.t); ctx.lineTo(nowX, pad.t + ih); ctx.stroke();
  ctx.fillStyle = cssVar("--faint"); ctx.fillText("now", nowX - 10, hgt - 3);
  if (pts.length < 2) return;
  ctx.beginPath();
  pts.forEach((p, i) => (i ? ctx.lineTo(X(p.t), Y(p.v)) : ctx.moveTo(X(p.t), Y(p.v))));
  ctx.strokeStyle = cssVar("--accent"); ctx.lineWidth = 1.8; ctx.stroke();

  const recent = pts.slice(-20);
  let label = "";
  if (recent.length >= 8) {
    const n = recent.length, mt = recent.reduce((a, p) => a + p.t, 0) / n, mv = recent.reduce((a, p) => a + p.v, 0) / n;
    const slope = recent.reduce((a, p) => a + (p.t - mt) * (p.v - mv), 0) / (recent.reduce((a, p) => a + (p.t - mt) ** 2, 0) || 1);
    const last = recent[n - 1];
    if (slope > 0.4) {
      const tto = (limit - last.v) / slope;
      const endT = last.t + Math.min(tto, span);
      ctx.setLineDash([3, 4]); ctx.strokeStyle = cssVar("--warn");
      ctx.beginPath(); ctx.moveTo(X(last.t), Y(last.v)); ctx.lineTo(X(endT), Y(last.v + slope * (endT - last.t))); ctx.stroke();
      ctx.setLineDash([]);
      label = `+${slope.toFixed(1)} MB/s · OOM in ~${fmtDur(tto)}`;
    } else {
      label = `${num(last.v)} MB · stable`;
    }
  }
  $("#mem-forecast").textContent = label;
}

function renderLogStats(st) {
  if (!st) return;
  const rawTok = Math.round(st.raw_chars / 4), sumTok = Math.round(st.summary_chars / 4);
  const ratio = sumTok ? Math.round(rawTok / sumTok) : 0;
  $("#logstats").replaceChildren(
    h("div", { class: "side" }, h("span", { class: "big", text: kfmt(st.raw_lines) }), h("span", { class: "lbl", text: `raw log lines (≈${kfmt(rawTok)} tokens)` })),
    h("span", { class: "arrow", text: "→" }),
    h("div", { class: "side" }, h("span", { class: "big", text: kfmt(st.templates) }), h("span", { class: "lbl", text: `Drain3 templates (≈${kfmt(sumTok)} tokens)` })),
    h("div", { class: "foot", text: ratio ? `${ratio}× less to read. Only error and warning templates reach the model.` : "Waiting for the first investigation." }));
}

/* ================================================================ approval */

function showApproval(ev) {
  S.currentApproval = ev.approval_id;
  const a = ev.action, hy = ev.hypothesis;
  $("#approval-incident").textContent = ev.incident;
  $("#approval-title").textContent = a.description;
  $("#approval-body").replaceChildren(
    h("div", { class: "why" }, h("b", { text: `${hy.id} · ` }), hy.title, ` (${Math.round(hy.confidence * 100)}% confidence)`),
    h("ul", {}, (hy.evidence || []).filter((e) => !(S.stage && e.startsWith("trace "))).slice(0, S.stage ? 2 : 3)
      .map((e) => h("li", { text: e }))),
    h("div", { class: "chips" }, h("span", { class: `risk ${a.risk}`, text: `${a.risk} risk` }), " ",
      h("span", { class: "hint", text: "Blast radius:" }), (a.blast_radius || []).map((n) => h("span", { class: "nchip", text: n }))),
    hy.skeptic ? h("div", { class: "hint", text: `Skeptic ${hy.skeptic.verdict}: ${hy.skeptic.reasoning}` }) : null);
  $("#approval").hidden = false;
  $("#approval-approve").focus();
}

function hideApproval() {
  $("#approval").hidden = true;
  S.currentApproval = null;
}

async function decide(decision, via) {
  const id = S.currentApproval;
  if (!id) return;
  try { await api("/api/approve", { approval_id: id, decision, via }); } catch (e) { sysRow(`Approval failed: ${e.message}`); }
}

/* ================================================================ settings + state */

function applySettings(st) {
  S.settings = st;
  $("#engine-select").value = st.engine;
  for (const b of $$("#mode-toggle button")) b.classList.toggle("active", b.dataset.mode === st.mode);
  const chip = $("#engine-chip");
  chip.className = `chip ${st.claude_available ? "claude" : "core"}`;
  chip.textContent = st.claude_available ? `${st.llm_name}, ${st.effort} effort` : "Core engine, no API key";
  $("#engine-select").disabled = !st.claude_available;
  $("#sw-business").checked = st.business_invariants !== false;
  $("#sw-learned").checked = st.learned_invariants !== false;
}

async function refreshState() {
  try {
    const st = await api("/api/state");
    applySettings(st.settings);
    markChaos(st.chaos);
    renderLogStats(st.log_stats);
  } catch { /* the stream shows the connection state */ }
}

async function init() {
  setStage(readStage());
  document.addEventListener("keydown", (e) => {
    if (e.key.toLowerCase() === "s" && !e.ctrlKey && !e.metaKey && !e.altKey && !/INPUT|SELECT|TEXTAREA/.test(e.target.tagName)) {
      setStage(!S.stage);
    }
  });
  const st = await api("/api/state");
  S.faults = st.faults; S.scenarios = st.scenarios; S.stages = st.stages;
  applySettings(st.settings);
  buildTopology(); buildChaos(); buildServiceList(); renderStepper(null); renderIncidentHead();
  menu("#break-toggle", "#break-menu");
  menu("#settings-toggle", "#settings-menu");
  document.addEventListener("click", closeMenus);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenus(); });
  markChaos(st.chaos); renderLogStats(st.log_stats);
  if (st.telemetry) updateTelemetry(st.telemetry);
  const closed = st.agent.closed;
  if (closed.length) { const c = closed[closed.length - 1]; S.lastResult = { id: c.id, status: c.status, mttr_s: c.mttr_s, closedAt: 0 }; }
  connect();
  setInterval(refreshState, 8000);

  $("#btn-mystery").onclick = () => { closeMenus(); api("/api/chaos/mystery", {}).catch((e) => sysRow(e.message)); };
  $("#btn-storm").onclick = () => { closeMenus(); api("/api/chaos/scenario", { scenario: "perfect_storm" }).catch((e) => sysRow(e.message)); };
  $("#btn-scenario").onclick = () => { closeMenus(); api("/api/chaos/scenario", { scenario: $("#scenario-select").value }).catch((e) => sysRow(e.message)); };
  $("#btn-reset").onclick = () => { closeMenus(); api("/api/chaos/reset", {}).then(refreshState).catch((e) => sysRow(e.message)); };
  $("#engine-select").onchange = (e) => api("/api/settings", { engine: e.target.value }).then(applySettings);
  for (const b of $$("#mode-toggle button")) b.onclick = () => api("/api/settings", { mode: b.dataset.mode }).then(applySettings);
  $("#sw-business").onchange = (e) => api("/api/settings", { business_invariants: e.target.checked }).then(applySettings);
  $("#sw-learned").onchange = (e) => api("/api/settings", { learned_invariants: e.target.checked }).then(applySettings);
  $("#btn-relearn").onclick = () => api("/api/learned/relearn", {}).catch((e) => sysRow(e.message));
  $("#approval-approve").onclick = () => decide("approve", "button");
  $("#approval-reject").onclick = () => decide("reject", "button");
  for (const tab of $$(".tab")) tab.onclick = () => showView(tab.dataset.view);
  for (const b of $$("#pm-lang button")) b.onclick = () => { S.pmLang = b.dataset.lang; renderPm(); };
  $("#bench-start").onclick = startBenchmark;
  $("#bench-stop").onclick = () => api("/api/benchmark/stop", {});
  $("#mem-clear").onclick = () => api("/api/memory/clear", {}).then(loadMemory);
  buildBenchSetup();
  loadPmList();
}

function showView(name) {
  for (const tab of $$(".tab")) tab.classList.toggle("active", tab.dataset.view === name);
  for (const v of ["war", "tel", "pm", "bench", "mem"]) $(`#view-${v}`).hidden = v !== name;
  if (name === "tel") drawMemory((S.tele?.services?.payments?.mem_limit_mb) || 320);
  if (name === "pm") loadPmList();
  if (name === "bench") loadBenchmark();
  if (name === "mem") loadMemory();
}

/* ================================================================ postmortems */

function inline(text) {
  const frag = document.createDocumentFragment();
  const re = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
    const tok = m[0];
    if (tok.startsWith("**")) frag.appendChild(h("b", { text: tok.slice(2, -2) }));
    else if (tok.startsWith("`")) frag.appendChild(h("code", { text: tok.slice(1, -1) }));
    else frag.appendChild(h("i", { text: tok.slice(1, -1) }));
    last = m.index + tok.length;
  }
  if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
  return frag;
}

function renderMarkdown(md) {
  const out = document.createDocumentFragment();
  let list = null;
  for (const line of md.split("\n")) {
    if (line.startsWith("- ")) {
      if (!list) { list = h("ul"); out.appendChild(list); }
      list.appendChild(h("li", {}, inline(line.slice(2))));
      continue;
    }
    list = null;
    if (!line.trim()) continue;
    if (line.startsWith("### ")) out.appendChild(h("h3", {}, inline(line.slice(4))));
    else if (line.startsWith("## ")) out.appendChild(h("h2", {}, inline(line.slice(3))));
    else if (line.startsWith("# ")) out.appendChild(h("h1", {}, inline(line.slice(2))));
    else if (line.startsWith("> ")) out.appendChild(h("blockquote", {}, inline(line.slice(2))));
    else out.appendChild(h("p", {}, inline(line)));
  }
  return out;
}

async function loadPmList() {
  let items = [];
  try { items = await api("/api/postmortems"); } catch { return; }
  $("#pm-list").replaceChildren(...(items.length ? items.map((p) => h("li", {},
    h("button", { class: `pm-item ${S.pmCurrent?.id === p.id ? "active" : ""}`, onclick: () => openPostmortem(p.id, false) },
      h("span", { class: "pi", text: `${p.id} · ${p.status}` }), h("span", { class: "pt", text: p.title }),
      h("span", { class: "pmeta", text: `${p.started_at} · recovered in ${fmtDur(p.durations?.mttr_s)} · ${routeLabel(p.engine?.route)}` })))) :
    [h("li", { class: "empty", text: "None yet." })]));
}

async function openPostmortem(id, switchView = true) {
  try { S.pmCurrent = await api(`/api/postmortems/${id}`); } catch { return; }
  if (switchView) showView("pm");
  renderPm();
  loadPmList();
}

function renderPm() {
  for (const b of $$("#pm-lang button")) b.classList.toggle("active", b.dataset.lang === S.pmLang);
  const body = $("#pm-body");
  const pm = S.pmCurrent;
  if (!pm) return;
  body.setAttribute("dir", S.pmLang === "ar" ? "rtl" : "ltr");
  body.setAttribute("lang", S.pmLang);
  body.replaceChildren(renderMarkdown(pm.markdown[S.pmLang] || ""));
  const dl = $("#pm-download");
  dl.href = `/api/postmortems/${pm.id}/markdown?lang=${S.pmLang}`;
  dl.setAttribute("download", `${pm.id}-${S.pmLang}.md`);
}

/* ================================================================ benchmark */

function buildBenchSetup() {
  const box = $("#bench-scenarios");
  const items = [...Object.entries(S.faults).filter(([, f]) => !f.held_out && !f.silent).map(([id, f]) => [id, f.title]),
    ...Object.entries(S.scenarios).map(([id, sc]) => [id, `${sc.title} (${sc.faults.length} faults)`]),
    ...Object.entries(S.faults).filter(([, f]) => f.held_out).map(([id, f]) => [id, `Held-out: ${f.title}`]),
    ...Object.entries(S.faults).filter(([, f]) => f.silent).map(([id, f]) => [id, `Silent: ${f.title}`])];
  box.replaceChildren(...items.map(([id, title]) => h("label", {}, h("input", { type: "checkbox", value: id, checked: true }), title)));
}

async function startBenchmark() {
  const scenarios = $$("#bench-scenarios input:checked").map((i) => i.value);
  try {
    await api("/api/benchmark/start", { scenarios, runs: Number($("#bench-runs").value || 1), baseline: $("#bench-baseline").checked });
    loadBenchmark();
  } catch (e) { $("#bench-status").textContent = `Could not start: ${e.message}`; }
}

function onBenchmark(ev) {
  if (ev.status) renderBenchStatus(ev.status);
  if (!$("#view-bench").hidden) loadBenchmark();
}

function renderBenchStatus(st) {
  const done = st.done || 0, total = st.total || 0;
  $("#bench-bar").style.width = total ? `${(done / total) * 100}%` : "0%";
  $("#bench-start").disabled = st.running;
  $("#bench-status").textContent = st.running
    ? `Running ${done + 1} of ${total}: ${st.current?.engine} · ${st.current?.scenario} · run ${st.current?.run}. Approvals are automatic while it runs.`
    : total ? `Finished ${done} of ${total}.` : "Each scenario injects real faults, waits for Nabd to recover the store, then resets. Expect about a minute per scenario.";
}

async function loadBenchmark() {
  let b;
  try { b = await api("/api/benchmark"); } catch { return; }
  renderBenchStatus(b.status);
  const sum = b.results.summary || {};
  const card = (key, title) => {
    const x = sum[key];
    if (!x) return null;
    const cells = [["Root cause right", pct(x.rca_accuracy, 0)], ["Correct outcome", pct(x.outcome_rate ?? x.recovery_rate, 0)],
      ["Recovered", pct(x.recovery_rate, 0)], ["Median MTTR", fmtDur(x.median_mttr_s)],
      ["Wrong actions", String(x.wrong_actions)], ["Gate caught", String(x.gated_actions ?? 0)]];
    if (x.books_balanced_rate !== undefined) {
      cells.push(["Books balanced", pct(x.books_balanced_rate, 0)], ["SAR left in limbo", Math.round(x.money_left_sar || 0).toLocaleString("en-US")],
        ["Items repaired", String(x.repaired_items || 0)]);
    }
    return h("div", { class: `bsum ${key.split(":")[0]}` }, h("h3", { text: `${title} · ${x.runs} runs` }),
      h("div", { class: "grid" }, cells.map(([k, v]) => h("div", {}, h("div", { class: "k", text: k }), h("div", { class: "v", text: v })))));
  };
  const ORDER = ["nabd", "nabd-no-learned", "nabd-no-books", "nabd-no-mizan", "baseline"];
  const NAMES = { nabd: "Nabd", "nabd-no-learned": "Without learned laws", "nabd-no-books": "Without hand-written books",
    "nabd-no-mizan": "Without Mizan", baseline: "Runbook" };
  const rank = (e) => (ORDER.includes(e) ? ORDER.indexOf(e) : 1);
  const engines = [...new Set(Object.keys(sum).map((k) => k.split(":")[0]))].sort((a, b) => rank(a) - rank(b));
  const title = (e) => NAMES[e] || e;
  const hasGroups = Object.keys(sum).some((k) => k.includes(":"));
  // an ablation compares many versions on the silent faults: one card each keeps it readable
  const ablation = engines.length > 2;
  $("#bench-summary").replaceChildren(...(hasGroups
    ? engines.flatMap((e) => ablation ? [card(`${e}:silent`, `${title(e)} · silent faults`)]
      : [card(`${e}:known`, `${title(e)} · known faults`), card(`${e}:silent`, `${title(e)} · silent faults`),
        card(`${e}:held_out`, `${title(e)} · held-out faults`)])
    : engines.map((e) => card(e, title(e)))).filter(Boolean));
  const rows = b.results.rows || [];
  const yn = (v) => h("span", { class: v ? "yes" : "no", text: v ? "yes" : "no" });
  $("#bench-table").replaceChildren(
    h("thead", {}, h("tr", {}, ["Engine", "Scenario", "Group", "Detect", "Root cause", "Outcome", "Recovered", "MTTR", "Actions", "Wrong", "Books", "Repaired", "Route", "LLM cost"].map((t) => h("th", { text: t })))),
    h("tbody", {}, rows.map((r) => h("tr", {},
      h("td", { text: r.engine }), h("td", { text: r.scenario }), h("td", { text: r.group === "held_out" ? "held-out" : r.group || "known" }),
      h("td", { class: "mono", text: r.detected ? fmtDur(r.detect_s) : "missed" }), h("td", {}, yn(r.rca_correct)),
      h("td", { title: r.status || "" }, yn(r.outcome_ok ?? r.recovered)), h("td", {}, yn(r.recovered)),
      h("td", { class: "mono", text: fmtDur(r.mttr_s) }), h("td", { class: "mono", text: String(r.actions ?? 0) }),
      h("td", { class: "mono", text: String(r.wrong_actions ?? 0), title: (r.wrong_detail || []).join("; ") }),
      h("td", { title: r.money_left_sar ? `${Math.round(r.money_left_sar)} SAR charged without an order` : "" },
        r.books_balanced === undefined ? "–" : yn(r.books_balanced)),
      h("td", { class: "mono", text: String(r.repaired_items ?? 0), title: (r.repairs || []).join("; ") }),
      h("td", { text: routeLabel(r.route) }), h("td", { class: "mono", text: costText(r.cost_usd) })))));
}

/* ================================================================ memory */

async function loadMemory() {
  let items = [];
  try { items = await api("/api/memory"); } catch { return; }
  $("#mem-list").replaceChildren(...(items.length ? items.slice().reverse().map((m) => h("div", { class: "mem-item" },
    h("div", { class: "mt", text: `${m.incident} · ${m.title}` }),
    h("div", { class: "mm", text: `fixed in ${fmtDur(m.mttr_s)} · reused ${m.reused || 0}× · ${m.roots.map((r) => r.action.name).join(", ")}` }),
    h("div", { class: "chips" }, m.signals.slice(0, 14).map((sg) => h("span", { class: "sig", text: sg })))))
    : [h("p", { class: "empty", text: "Nothing learned yet. Resolve an incident and it appears here." })]));
}

init().catch((e) => {
  document.body.prepend(h("div", { class: "row sys", style: "padding:10px 18px" }, `Could not load Nabd: ${e.message}`));
});
