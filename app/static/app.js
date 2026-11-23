/* AP Autopilot - front end (vanilla JS, no build step). */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = (x) => {
  if (x === null || x === undefined || isNaN(x)) return "—";
  return "₹" + Number(x).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};
const inrShort = (x) => {
  x = Number(x || 0);
  if (x >= 1e7) return "₹" + (x / 1e7).toFixed(2) + " Cr";
  if (x >= 1e5) return "₹" + (x / 1e5).toFixed(2) + " L";
  return inr(x);
};
const OUTCOME_TEXT = { APPROVED: "Approved", NEEDS_REVIEW: "Needs review", ON_HOLD: "On hold", REJECTED: "Rejected" };
const OUTCOME_COLOR = { APPROVED: "#067647", NEEDS_REVIEW: "#f79009", ON_HOLD: "#c4320a", REJECTED: "#b42318" };
const ICON = { pass: "✓", fail: "!", warn: "!", info: "i", skip: "–" };

const state = { health: null, stages: [], es: null, runId: null, timer: null, t0: 0, filter: "ALL", runs: [] };

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}
function toast(msg, ms = 2600) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}

/* ------------------------------------------------------------------ tabs */
function show(view) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => (v.hidden = v.id !== "view-" + view));
  if (view === "dashboard") loadDashboard();
  if (view === "reference") loadReference();
}
$$(".tab").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

/* ------------------------------------------------------------------ init */
async function init() {
  const h = await api("/api/health");
  state.health = h;
  state.stages = h.stages;
  const pill = $("#aiPill");
  if (h.ocr_available) { pill.className = "pill ai"; pill.textContent = "Runs locally · OCR ready"; pill.title = "Digital PDFs are read from the text layer, scans with local OCR. No API key or internet needed."; }
  else { pill.className = "pill offline"; pill.textContent = "OCR unavailable"; pill.title = (h.ocr_problem || "") + " - scanned invoices will go to manual entry"; }
  $("#buyerName").textContent = `Buyer: ${h.buyer.name} · GSTIN ${h.buyer.gstin}`;
  $("#flowPreview").innerHTML = h.stages.map((s) => `<li>${esc(s.title)}${s.id === "extract" ? '<span class="ai-tag">OCR</span>' : ""}</li>`).join("");
  const samples = await api("/api/samples");
  $("#scenarioList").innerHTML = samples.map((s) => `
    <div class="scn">
      <div class="scn-top"><span class="scn-tag">${esc(s.tag)}</span><span class="badge ${esc(s.expected)}" title="Expected outcome">${esc(OUTCOME_TEXT[s.expected] || s.expected)}</span></div>
      <div class="scn-title">${esc(s.title)}</div>
      <div class="scn-story">${esc(s.story)}</div>
      <div class="scn-actions">
        <button class="btn small" data-run="${esc(s.file)}">▶ Run</button>
        <button class="btn ghost small" data-preview="${esc(s.file)}">Preview PDF</button>
      </div>
    </div>`).join("");
  $$("[data-run]").forEach((b) => b.addEventListener("click", () => runSample(b.dataset.run)));
  $$("[data-preview]").forEach((b) => b.addEventListener("click", () => openDoc(`/api/samples/${encodeURIComponent(b.dataset.preview)}`, b.dataset.preview)));
  refreshBadge();
  const fromHash = location.hash.match(/^#run=(.+)$/);
  if (fromHash) openRun(fromHash[1]);
}

/* ------------------------------------------------------------------ submit */
async function runSample(file) {
  try {
    const { run_id } = await api(`/api/runs/sample/${encodeURIComponent(file)}`, { method: "POST" });
    openRun(run_id);
  } catch (e) { toast("Could not start: " + e.message); }
}
async function uploadFile(file) {
  if (!file) return;
  if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") { toast("Please choose a PDF file"); return; }
  const fd = new FormData(); fd.append("file", file);
  try {
    const { run_id } = await api("/api/runs", { method: "POST", body: fd });
    openRun(run_id);
  } catch (e) { toast("Upload failed: " + e.message); }
}
const dz = $("#dropzone");
dz.addEventListener("click", (e) => { if (e.target.tagName !== "INPUT") { e.preventDefault(); $("#fileInput").click(); } });
$("#fileInput").addEventListener("change", (e) => { uploadFile(e.target.files[0]); e.target.value = ""; });
["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => uploadFile(e.dataTransfer.files[0]));

/* ------------------------------------------------------------------ live run view */
function openRun(runId) {
  show("process");
  if (state.es) state.es.close();
  clearInterval(state.timer);
  state.runId = runId;
  history.replaceState(null, "", "#run=" + runId);
  $("#emptyState").hidden = true;
  $("#runView").hidden = false;
  $("#runTitle").textContent = "Loading…";
  $("#runMeta").textContent = runId;
  $("#exportBtn").href = `/api/runs/${runId}/export`;
  $("#docBtn").onclick = () => openDoc(`/api/runs/${runId}/document`, runId);
  $("#stepper").innerHTML = state.stages.map((s) => `<div class="step" id="step-${s.id}" title="${esc(s.title)}"></div>`).join("");
  $("#timeline").innerHTML = state.stages.map((s, i) => `
    <div class="stage pending" id="stage-${s.id}">
      <div class="stage-head">
        <span class="stage-idx">${i + 1}</span>
        <span class="stage-name">${esc(s.title)}</span>
        <span class="stage-summary">Waiting…</span>
        <span class="stage-ms"></span>
        <span class="chev">›</span>
      </div>
      <div class="stage-body"></div>
    </div>`).join("");
  $$(".stage-head").forEach((h) => h.addEventListener("click", () => {
    const st = h.parentElement; if (!st.classList.contains("pending")) st.classList.toggle("open");
  }));
  const dc = $("#decisionCard");
  dc.className = "decision-card pending";
  dc.innerHTML = `<div class="decision-label">Decision</div><div class="decision-outcome">Processing…</div>`;
  $("#runTimer").textContent = "0.0s";

  let replay = true;
  const es = new EventSource(`/api/runs/${runId}/events`);
  state.es = es;
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === "run_started") {
      $("#runTitle").textContent = ev.filename;
      $("#runMeta").textContent = `Run ${ev.run_id} · source: ${ev.source}`;
      state.t0 = ev.ts * 1000;
      replay = Date.now() - state.t0 > 3000;
      if (!replay) {
        state.timer = setInterval(() => ($("#runTimer").textContent = ((Date.now() - state.t0) / 1000).toFixed(1) + "s"), 100);
      }
    } else if (ev.type === "stage_started") {
      const el = $("#stage-" + ev.stage);
      el.className = "stage running";
      $(".stage-summary", el).textContent = ev.stage === "extract" ? "Reading the document (scans take a few seconds)…" : "Running…";
      $("#step-" + ev.stage).className = "step running";
      if (!replay) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    } else if (ev.type === "stage_completed") {
      renderStage(ev, replay);
    } else if (ev.type === "run_failed") {
      toast("Processing error: " + ev.error, 6000);
    } else if (ev.type === "run_completed") {
      clearInterval(state.timer);
      $("#runTimer").textContent = (ev.duration_ms / 1000).toFixed(2) + "s";
      es.close();
      loadDecision(runId);
      refreshBadge();
    }
  };
  es.onerror = () => { /* the browser retries automatically; replay is idempotent */ };
}

function renderStage(ev, replay) {
  const el = $("#stage-" + ev.stage);
  el.className = "stage " + ev.status;
  $(".stage-summary", el).textContent = ev.summary || "";
  $(".stage-ms", el).textContent = ev.status === "skip" ? "" : ev.duration_ms >= 1000 ? (ev.duration_ms / 1000).toFixed(1) + "s" : ev.duration_ms + " ms";
  $("#step-" + ev.stage).className = "step " + ev.status;
  $(".stage-body", el).innerHTML = stageBody(ev);
  const interesting = ev.status === "fail" || ev.status === "warn" || ["extract", "three_way"].includes(ev.stage);
  if (interesting && ev.status !== "skip") el.classList.add("open");
}

function checksHtml(checks) {
  if (!checks || !checks.length) return "";
  return `<ul class="checks">${checks.map((c) => `
    <li class="check ${esc(c.status)}">
      <span class="check-ico">${ICON[c.status] || "•"}</span>
      <div><div class="check-title">${esc(c.title)}</div><div class="check-detail">${esc(c.detail)}</div></div>
      ${c.effect ? `<span class="effect ${esc(c.effect)}">${esc(c.effect)}</span>` : "<span></span>"}
    </li>`).join("")}</ul>`;
}

function kv(pairs) {
  return `<div class="kv">${pairs.map(([k, v, fmt]) => {
    const isNull = v === null || v === undefined || v === "";
    const val = isNull ? "not on document" : fmt === "money" ? inr(v) : esc(v);
    return `<div><div class="k">${esc(k)}</div><div class="v ${isNull ? "null" : ""}">${val}</div></div>`;
  }).join("")}</div>`;
}

function stageBody(ev) {
  const d = ev.data || {};
  let extra = "";
  if (ev.stage === "intake" && d.doc) {
    extra = kv([["Type", d.doc.kind], ["Pages", d.doc.pages], ["Text characters", d.doc.text_chars], ["Size", d.doc.size_kb + " KB"]]);
  }
  if (ev.stage === "extract" && d.invoice) {
    const i = d.invoice;
    extra = `<div class="sub-h">Extracted fields <span class="muted">· via ${esc(d.extractor)}</span></div>` + kv([
      ["Vendor", i.vendor_name], ["Vendor GSTIN", i.vendor_gstin], ["Invoice #", i.invoice_number], ["Invoice date", i.invoice_date],
      ["PO reference", i.po_reference], ["Due date", i.due_date], ["Subtotal", i.subtotal, "money"], ["CGST", i.cgst, "money"],
      ["SGST", i.sgst, "money"], ["IGST", i.igst, "money"], ["Total", i.total, "money"], ["Prices incl. tax", i.prices_include_tax ? "Yes" : "No"],
      ["Bank account", i.bank_account_number], ["IFSC", i.bank_ifsc], ["Bill-to GSTIN", i.buyer_gstin], ["Currency", i.currency],
    ]);
    if (i.line_items && i.line_items.length) {
      extra += `<div class="sub-h">Line items</div><div class="table-wrap"><table class="tbl"><thead><tr><th>Description</th><th>HSN/SAC</th><th class="num">Qty</th><th class="num">Unit price</th><th class="num">Amount</th></tr></thead><tbody>${
        i.line_items.map((l) => `<tr><td>${esc(l.description)}</td><td>${esc(l.hsn_sac || "")}</td><td class="num">${esc(l.quantity ?? "—")}</td><td class="num">${inr(l.unit_price)}</td><td class="num">${inr(l.amount)}</td></tr>`).join("")
      }</tbody></table></div>`;
    }
  }
  if (ev.stage === "vendor" && d.vendor) {
    const v = d.vendor;
    extra = kv([["Vendor ID", v.vendor_id], ["Legal name", v.name], ["Master GSTIN", v.gstin], ["Status", v.status], ["Payment terms", v.terms_days + " days"], ["Bank on file", v.bank]]);
  }
  if (ev.stage === "po_match") {
    if (d.candidates && d.candidates.length) {
      extra += `<div class="sub-h">PO inference - open POs for this vendor scored on line-item & amount fit</div><table class="tbl"><thead><tr><th>PO</th><th>Fit score</th></tr></thead><tbody>${
        d.candidates.map((c) => `<tr><td class="mono">${esc(c.po)}</td><td><span class="bar3" style="width:160px"><span class="r" style="width:${Math.round(c.score * 100)}%"></span></span> ${Math.round(c.score * 100)}%</td></tr>`).join("")
      }</tbody></table>`;
    }
    if (d.mapping && d.mapping.length) {
      extra += `<div class="sub-h">Invoice line → PO line</div><table class="tbl"><thead><tr><th>Invoice line</th><th>PO line</th><th class="num">Match</th></tr></thead><tbody>${
        d.mapping.map((m) => `<tr class="${m.po_line ? "" : "bad"}"><td>${esc(m.invoice_line)}</td><td>${esc(m.po_line || "— not on PO —")}</td><td class="num">${Math.round(m.score * 100)}%</td></tr>`).join("")
      }</tbody></table>`;
    }
  }
  if (ev.stage === "three_way" && d.rows) {
    extra = `<div class="sub-h">Line-level 3-way match</div><div class="table-wrap"><table class="tbl"><thead><tr>
      <th>PO line</th><th class="num">PO price</th><th class="num">Billed (net)</th><th class="num">Var</th>
      <th class="num">Ordered</th><th class="num">Received</th><th class="num">Billed before</th><th class="num">Billed now</th><th class="num">Payable now</th></tr></thead><tbody>${
      d.rows.map((r) => {
        const over = r.billed_before + r.invoiced_now > r.received + 1e-9;
        const pv = r.price_var_pct;
        return `<tr><td>${esc(r.po_line)}. ${esc(r.item)}</td><td class="num">${inr(r.po_price)}</td><td class="num">${inr(r.inv_price_net)}</td>
        <td class="num ${pv > (state.health.policy.unit_price_tolerance_pct) ? "bad" : ""}">${pv === null ? "—" : (pv > 0 ? "+" : "") + pv.toFixed(2) + "%"}</td>
        <td class="num">${r.ordered}</td><td class="num">${r.received}</td><td class="num">${r.billed_before}</td>
        <td class="num ${over ? "bad" : ""}">${r.invoiced_now}</td><td class="num ${over ? "bad" : "ok"}">${r.billable_now}</td></tr>`;
      }).join("")}</tbody></table></div>`;
    if (d.prior_invoices && d.prior_invoices.length) {
      extra += `<div class="muted small" style="margin-top:6px">Earlier invoices on this PO: ${d.prior_invoices.map((p) => `${esc(p.invoice_number)} (${esc(p.invoice_date)}, ${inr(p.total)}, ${esc(p.status)})`).join("; ")}</div>`;
    }
  }
  if (ev.stage === "decision" && d.decision) {
    extra = `<div class="muted small">Policy: most severe effect wins - reject › hold › review › approve. ${d.decision.stats.checks_passed} checks passed, ${d.decision.stats.checks_failed} failed.</div>`;
  }
  return checksHtml(ev.checks) + extra;
}

async function loadDecision(runId) {
  if (runId !== state.runId) return;
  const r = await api(`/api/runs/${runId}`);
  const d = (r.result || {}).decision;
  const card = $("#decisionCard");
  if (!d) { card.innerHTML = `<div class="decision-outcome">No decision</div>`; return; }
  card.className = "decision-card " + d.outcome;
  const reasons = d.reasons || [];
  let html = `<div class="decision-label">Decision</div>
    <div class="decision-outcome">${esc(OUTCOME_TEXT[d.outcome])}</div>
    <div class="decision-headline">${esc(d.headline)}</div>
    <div class="decision-meta">
      ${d.amount != null ? `<span class="tag">Invoice ${inr(d.amount)}</span>` : ""}
      <span class="tag">Queue: ${esc(d.queue)}</span>
      ${d.payment_due ? `<span class="tag">Pay on ${esc(d.payment_due)}</span>` : ""}
      <span class="tag">${r.extractor ? "Read by " + esc(r.extractor.split(" ")[0]) : "Not read"}</span>
    </div>`;
  if (d.partial_approval) {
    html += `<div class="d-sec"><h4>Recommended short-pay</h4><div class="partial">
      <div><div class="k">Payable now</div><div class="v">${inr(d.partial_approval.gross)}</div></div>
      <div><div class="k">Withhold</div><div class="v">${inr(d.partial_approval.withheld)}</div></div></div></div>`;
  }
  if (reasons.length) {
    html += `<div class="d-sec"><h4>Why (${reasons.length} check${reasons.length > 1 ? "s" : ""})</h4><ol>${reasons.map((x) => `<li><span class="effect ${esc(x.effect)}">${esc(x.effect)}</span> <strong>${esc(x.title)}</strong> - ${esc(x.detail)}</li>`).join("")}</ol></div>`;
  }
  html += `<div class="d-sec"><h4>Next actions</h4><ul>${(d.next_actions || []).map((a) => `<li>${esc(a)}</li>`).join("")}</ul></div>`;
  if (d.vendor_message) {
    const m = d.vendor_message;
    html += `<div class="d-sec"><h4>Draft to vendor <span class="muted">(not sent - human approves)</span></h4><div class="email">
      <div class="row">To: ${esc(m.to)}</div><div class="row">Subject: <strong>${esc(m.subject)}</strong></div><pre>${esc(m.body)}</pre>
      <button class="btn ghost small" id="copyEmail">Copy</button></div></div>`;
  }
  if (d.outcome === "ON_HOLD") {
    html += `<div class="d-sec"><div class="muted small">No email is drafted for holds on purpose: a fraudster controls the contact details on the invoice. Verify by phone using the vendor master.</div></div>`;
  }
  if (["NEEDS_REVIEW", "ON_HOLD"].includes(d.outcome)) {
    if (r.resolution) {
      const res = r.resolution;
      html += `<div class="d-sec"><div class="resolved ${res.final_status === "REJECTED" ? "rej" : ""}"><strong>${esc(res.final_status === "APPROVED" ? "Approved" : "Rejected")}</strong> by ${esc(res.reviewer)} · ${esc(res.at)}${res.note ? `<br/><span class="muted">"${esc(res.note)}"</span>` : ""}</div></div>`;
    } else {
      const holdNote = d.outcome === "ON_HOLD" ? "Verification note (required to release), e.g. 'Called +91… on file, confirmed by CFO'" : "Reviewer note (optional)";
      html += `<div class="d-sec review-box"><h4>Reviewer action</h4><textarea id="revNote" placeholder="${esc(holdNote)}"></textarea>
        <div class="review-actions"><button class="btn ok small" id="revApprove">${d.outcome === "ON_HOLD" ? "Verified - release" : "Approve"}</button>
        <button class="btn bad small" id="revReject">Reject</button></div></div>`;
    }
  }
  card.innerHTML = html;
  const ce = $("#copyEmail");
  if (ce) ce.onclick = () => { navigator.clipboard.writeText(`Subject: ${d.vendor_message.subject}\n\n${d.vendor_message.body}`); toast("Email draft copied"); };
  const ra = $("#revApprove"), rr = $("#revReject");
  if (ra) ra.onclick = () => resolveRun(runId, "approve");
  if (rr) rr.onclick = () => resolveRun(runId, "reject");
}

async function resolveRun(runId, action) {
  try {
    await api(`/api/runs/${runId}/resolve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, note: $("#revNote").value }) });
    toast(action === "approve" ? "Approved - ledger updated" : "Rejected - ledger updated");
    loadDecision(runId); refreshBadge();
  } catch (e) { toast(e.message, 4000); }
}

/* ------------------------------------------------------------------ dashboard */
async function refreshBadge() {
  try {
    const s = await api("/api/stats");
    const b = $("#openBadge"); b.hidden = !s.open_exceptions; b.textContent = s.open_exceptions;
  } catch (_) {}
}

async function loadDashboard() {
  const [s, runs] = await Promise.all([api("/api/stats"), api("/api/runs")]);
  state.runs = runs;
  const k = [
    ["Invoices processed", s.runs, `${inrShort(s.value_processed)} total value`],
    ["Straight-through", Math.round(s.straight_through_rate * 100) + "%", "approved with no human touch"],
    ["Value approved", inrShort(s.value_approved), "auto-approved + released by reviewers"],
    ["Value protected", inrShort(s.value_protected), "duplicates, fraud holds, short-pays"],
    ["Open exceptions", s.open_exceptions, "awaiting a reviewer"],
    ["Avg. processing time", (s.avg_duration_ms / 1000).toFixed(1) + "s", "per invoice, end to end"],
  ];
  $("#kpis").innerHTML = k.map(([a, b, c]) => `<div class="kpi"><div class="k">${esc(a)}</div><div class="v">${esc(b)}</div><div class="s">${esc(c)}</div></div>`).join("");
  const total = Object.values(s.by_decision).reduce((a, b) => a + b, 0) || 1;
  $("#mixBar").innerHTML = Object.entries(s.by_decision).filter(([, v]) => v).map(([kk, v]) =>
    `<div style="flex:${v};background:${OUTCOME_COLOR[kk]}" title="${OUTCOME_TEXT[kk]}: ${v}">${v}</div>`).join("") || "";
  $("#mixLegend").innerHTML = Object.entries(s.by_decision).map(([kk, v]) => `<span style="--c:${OUTCOME_COLOR[kk]}">${OUTCOME_TEXT[kk]} · ${v} (${Math.round((v / total) * 100)}%)</span>`).join("");
  const max = Math.max(1, ...s.top_reasons.map((r) => r[1]));
  $("#topReasons").innerHTML = s.top_reasons.length ? s.top_reasons.map(([t, n]) => `<div class="reason-row"><span>${esc(t)}</span><span class="reason-bar"><div style="width:${(n / max) * 100}%"></div></span><strong>${n}</strong></div>`).join("") : `<div class="muted small">No exceptions yet.</div>`;
  renderRuns();
}

function renderRuns() {
  const f = state.filter;
  const rows = state.runs.filter((r) => f === "ALL" || (f === "OPEN" ? ["NEEDS_REVIEW", "ON_HOLD"].includes(r.decision) && !r.resolution : r.decision === f));
  $("#runsEmpty").hidden = rows.length > 0;
  $("#runsTable tbody").innerHTML = rows.map((r) => {
    const when = new Date(r.created_at).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const dec = r.status === "running" ? `<span class="badge RUNNING">Running</span>` : r.decision ? `<span class="badge ${esc(r.decision)}">${esc(OUTCOME_TEXT[r.decision])}</span>` : `<span class="badge">Error</span>`;
    const res = r.resolution ? `<span class="badge ${esc(r.resolution.final_status)}">${esc(OUTCOME_TEXT[r.resolution.final_status])}</span> <span class="muted small">by reviewer</span>` : ["NEEDS_REVIEW", "ON_HOLD"].includes(r.decision) ? `<span class="muted small">Open</span>` : `<span class="muted small">—</span>`;
    return `<tr class="click" data-id="${esc(r.id)}"><td class="small">${esc(when)}</td><td class="small">${esc(r.filename)}</td><td>${esc(r.vendor_name || "—")}</td>
      <td class="mono">${esc(r.invoice_number || "—")}</td><td class="num">${inr(r.total)}</td><td>${dec}</td>
      <td class="small" style="max-width:360px">${esc(r.headline || "")}</td><td>${res}</td><td class="num small">${r.duration_ms ? (r.duration_ms / 1000).toFixed(1) + "s" : ""}</td></tr>`;
  }).join("");
  $$("#runsTable tbody tr").forEach((tr) => tr.addEventListener("click", () => openRun(tr.dataset.id)));
}
$$("#filters .chip").forEach((c) => c.addEventListener("click", () => {
  state.filter = c.dataset.f; $$("#filters .chip").forEach((x) => x.classList.toggle("active", x === c)); renderRuns();
}));

/* ------------------------------------------------------------------ reference */
async function loadReference() {
  const d = await api("/api/reference");
  $("#poList").innerHTML = d.purchase_orders.map((po) => `
    <div class="po"><div class="po-head"><strong>${esc(po.po_number)}</strong><span>${esc(po.vendor)}</span><span class="muted small">${esc(po.department)} · ${esc(po.po_date)}</span>
      <span class="badge ${po.status === "OPEN" ? "APPROVED" : "REJECTED"}">${esc(po.status)}</span><span style="margin-left:auto" class="num"><strong>${inr(po.total)}</strong> <span class="muted small">pre-tax</span></span></div>
      <table class="tbl"><thead><tr><th>#</th><th>Item</th><th class="num">Unit price</th><th class="num">GST</th><th class="num">Ordered</th><th class="num">Received</th><th class="num">Billed</th><th>Progress (received / billed)</th></tr></thead><tbody>${
        po.lines.map((l) => `<tr><td>${l.line_no}</td><td>${esc(l.description)}</td><td class="num">${inr(l.unit_price)}</td><td class="num">${Math.round(l.tax_rate * 100)}%</td>
        <td class="num">${l.qty} ${esc(l.uom)}</td><td class="num">${l.received}</td><td class="num ${l.billed > l.received ? "bad" : ""}">${l.billed}</td>
        <td><span class="bar3"><span class="r" style="width:${Math.min(100, (l.received / l.qty) * 100)}%"></span><span class="b" style="width:${Math.min(100, (l.billed / l.qty) * 100)}%"></span></span></td></tr>`).join("")
      }</tbody></table></div>`).join("");
  $("#vendorTable").innerHTML = `<thead><tr><th>ID</th><th>Legal name</th><th>GSTIN</th><th>Status</th><th>Bank (masked)</th><th>IFSC</th><th class="num">Terms</th><th>Contact</th></tr></thead><tbody>${
    d.vendors.map((v) => `<tr><td>${esc(v.vendor_id)}</td><td>${esc(v.legal_name)}</td><td class="mono">${esc(v.gstin)}</td>
    <td><span class="badge ${v.status === "ACTIVE" ? "APPROVED" : "REJECTED"}">${esc(v.status)}</span>${v.status_reason ? `<div class="muted small">${esc(v.status_reason)}</div>` : ""}</td>
    <td class="mono">${esc(v.bank_name)} ${esc(v.bank_account)}</td><td class="mono">${esc(v.bank_ifsc)}</td><td class="num">${v.payment_terms_days}d</td><td class="small">${esc(v.contact_email)}</td></tr>`).join("")}</tbody>`;
  $("#ledgerTable").innerHTML = `<thead><tr><th>#</th><th>Vendor</th><th>Invoice #</th><th>Date</th><th>PO</th><th class="num">Subtotal</th><th class="num">Tax</th><th class="num">Total</th><th>Status</th><th>Source</th></tr></thead><tbody>${
    d.ledger.map((l) => `<tr><td>${l.id}</td><td>${esc(l.vendor_id)}</td><td class="mono">${esc(l.invoice_number || "—")}</td><td>${esc(l.invoice_date || "—")}</td><td class="mono">${esc(l.po_number || "—")}</td>
    <td class="num">${inr(l.subtotal)}</td><td class="num">${inr(l.tax_total)}</td><td class="num">${inr(l.total)}</td><td><span class="badge ${esc(l.status === "PAID" ? "APPROVED" : l.status)}">${esc(l.status)}</span></td><td class="small">${esc(l.source)}</td></tr>`).join("")}</tbody>`;
}

/* ------------------------------------------------------------------ misc */
function openDoc(url, title) {
  $("#docTitle").textContent = title; $("#docFrame").src = url; $("#docModal").hidden = false;
}
$("#docClose").onclick = () => { $("#docModal").hidden = true; $("#docFrame").src = "about:blank"; };
$("#docModal").addEventListener("click", (e) => { if (e.target.id === "docModal") $("#docClose").click(); });
$("#resetBtn").onclick = async () => {
  if (!confirm("Clear all runs and restore the seeded AP ledger?")) return;
  await api("/api/reset", { method: "POST" });
  if (state.es) state.es.close();
  history.replaceState(null, "", "#");
  $("#runView").hidden = true; $("#emptyState").hidden = false;
  refreshBadge(); toast("Demo data reset");
  if (!$("#view-dashboard").hidden) loadDashboard();
  if (!$("#view-reference").hidden) loadReference();
};

init().catch((e) => toast("Failed to load: " + e.message, 6000));
