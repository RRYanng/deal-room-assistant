// Deal Room Assistant - single-page UI. Plain JavaScript, no build step.

const state = {
  companyId: null,
  companyName: "",
  chats: {},        // companyId -> list of chat entries {question, answer|error|pending}
  lastBrief: null,
};

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  let body = null;
  try { body = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const detail = body && body.detail;
    throw new Error(typeof detail === "string" ? detail : `Request failed (${res.status})`);
  }
  return body;
}

const METRIC_NAMES = {
  registered_customers: "Registered customers",
  paying_customers: "Paying customers",
  revenue: "Revenue",
  funding_raised: "Funding raised",
  other: "Other",
};
const metricName = (m, label) => (m === "other" && label ? label : METRIC_NAMES[m] || m);

function formatValue(m) {
  if (m.value === null || m.value === undefined) return esc(m.value_raw);
  const n = Number(m.value).toLocaleString("en-US", { maximumFractionDigits: 2 });
  if (m.currency) return `${esc(m.currency)} ${n}`;
  return n;
}

// Highlight the quote inside the chunk text, tolerating whitespace differences.
function highlight(text, quote) {
  const words = String(quote || "").trim().split(/\s+/).filter(Boolean);
  if (!words.length) return esc(text);
  const pattern = new RegExp(words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s+"));
  const m = pattern.exec(text);
  if (!m) return esc(text);
  return esc(text.slice(0, m.index)) + "<mark>" + esc(m[0]) + "</mark>" + esc(text.slice(m.index + m[0].length));
}

// ---------------------------------------------------------------- companies

async function loadHealth() {
  try {
    const h = await api("/api/health");
    $("health").textContent = `Q&A: ${h.llm_chat_model} · Extraction: ${h.llm_extract_model}`;
  } catch { $("health").textContent = "API unavailable"; }
}

async function loadCompanies() {
  const companies = await api("/api/companies");
  const list = $("company-list");
  list.innerHTML = companies.length ? "" : '<li class="muted">No companies yet.</li>';
  for (const c of companies) {
    const li = document.createElement("li");
    li.className = c.id === state.companyId ? "active" : "";
    li.innerHTML = `<span>${esc(c.name)}</span><span class="muted small">${c.documents} docs</span>`;
    li.onclick = () => selectCompany(c.id, c.name);
    list.appendChild(li);
  }
}

async function selectCompany(id, name) {
  state.companyId = id;
  state.companyName = name;
  $("company-title").textContent = name;
  $("upload-section").classList.remove("disabled");
  for (const el of ["question", "ask-button", "brief-requirements", "brief-button"]) $(el).disabled = false;
  $("upload-status").textContent = "";
  $("brief-status").textContent = "";
  renderChat();
  await Promise.all([loadCompanies(), refreshCompanyData()]);
}

async function refreshCompanyData() {
  if (!state.companyId) return;
  const id = state.companyId;
  const [docs, metrics, checks] = await Promise.all([
    api(`/api/companies/${id}/documents`),
    api(`/api/companies/${id}/metrics`),
    api(`/api/companies/${id}/checks`),
  ]);
  if (id !== state.companyId) return; // user switched company meanwhile
  renderDocuments(docs);
  renderMetrics(metrics);
  renderChecks(checks);
}

$("new-company-form").onsubmit = async (e) => {
  e.preventDefault();
  const name = $("new-company-name").value.trim();
  if (!name) return;
  try {
    const c = await api("/api/companies", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
    });
    $("new-company-name").value = "";
    await selectCompany(c.id, c.name);
  } catch (err) { alert(err.message); }
};

// ---------------------------------------------------------------- documents

const DOC_TYPES = { deck: "Deck", notes: "Notes", crm: "CRM" };

function renderDocuments(docs) {
  const list = $("document-list");
  if (!docs.length) { list.innerHTML = '<li class="muted">No documents yet.</li>'; return; }
  list.innerHTML = docs.map((d) => `
    <li>
      <div class="doc-name">${esc(d.filename)}</div>
      <div class="muted small">
        <span class="badge">${esc(DOC_TYPES[d.doc_type] || d.doc_type)}</span>
        ${esc(d.doc_date || "no date")} · ${d.chunks} chunks ·
        <button type="button" class="link small" data-reextract="${d.id}">re-extract</button> ·
        <button type="button" class="link small" data-delete="${d.id}" data-name="${esc(d.filename)}">remove</button>
      </div>
    </li>`).join("");
  list.querySelectorAll("[data-reextract]").forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true;
      setStatus("upload-status", "Extracting metrics…");
      try {
        const r = await api(`/api/documents/${btn.dataset.reextract}/extract`, { method: "POST" });
        setStatus("upload-status", extractionText(r), r.status === "ok" ? "ok" : "error");
        await refreshCompanyData();
      } catch (err) { setStatus("upload-status", err.message, "error"); }
      btn.disabled = false;
    };
  });
  bindDeleteButtons(list);
}

function bindDeleteButtons(list) {
  list.querySelectorAll("[data-delete]").forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm(`Remove ${btn.dataset.name}? Its chunks and metrics are deleted too.`)) return;
      try {
        await api(`/api/documents/${btn.dataset.delete}`, { method: "DELETE" });
        setStatus("upload-status", `Removed ${btn.dataset.name}.`, "ok");
        await Promise.all([loadCompanies(), refreshCompanyData()]);
      } catch (err) { setStatus("upload-status", err.message, "error"); }
    };
  });
}

function setStatus(id, text, kind = "") {
  const el = $(id);
  el.textContent = text;
  el.className = `status ${kind}`;
}

function extractionText(x) {
  if (!x) return "";
  if (x.status === "ok") {
    const dropped = x.dropped.length ? `, ${x.dropped.length} dropped (failed quote check)` : "";
    return `Metrics: ${x.kept} extracted${dropped}.`;
  }
  return `Metric extraction failed: ${x.error}`;
}

$("upload-form").onsubmit = async (e) => {
  e.preventDefault();
  const file = $("upload-file").files[0];
  if (!file || !state.companyId) return;
  const form = new FormData();
  form.append("file", file);
  form.append("doc_type", $("upload-type").value);
  if ($("upload-date").value) form.append("doc_date", $("upload-date").value);
  $("upload-button").disabled = true;
  setStatus("upload-status", `Uploading ${file.name} and extracting metrics…`);
  try {
    const r = await api(`/api/companies/${state.companyId}/documents`, { method: "POST", body: form });
    const replaced = r.replaced ? " (replaced the previous version)" : "";
    const ok = !r.extraction || r.extraction.status === "ok";
    setStatus("upload-status", `Stored ${r.filename}${replaced}: ${r.chunks} chunks.\n${extractionText(r.extraction)}`,
      ok ? "ok" : "error");
    $("upload-form").reset();
    await Promise.all([loadCompanies(), refreshCompanyData()]);
  } catch (err) {
    setStatus("upload-status", err.message, "error");
  } finally {
    $("upload-button").disabled = false;
  }
};

// ---------------------------------------------------------------- metrics & checks

function sourceSpan(s) {
  return `<span class="src" title="${esc(s.quote)}">${esc(s.loc_label)}</span>`;
}

function renderMetrics(metrics) {
  if (!metrics.length) { $("metrics").innerHTML = '<div class="muted">No metrics extracted yet.</div>'; return; }
  const rows = metrics.map((m) => `
    <tr>
      <td>${esc(metricName(m.metric, m.metric_label))}</td>
      <td class="num">${formatValue(m)}</td>
      <td class="num">${m.as_of_date ? esc(m.as_of_date) : '<span class="badge warn">unclear basis</span>'}</td>
      <td>${sourceSpan(m)}</td>
    </tr>`).join("");
  $("metrics").innerHTML = `
    <table>
      <thead><tr><th>Metric</th><th>Value</th><th>As of</th><th>Source</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="muted small" style="margin-top:6px">Hover a source to see the exact quote.</div>`;
}

function conflictHtml(c) {
  const sources = c.sources.map((s) => `
    <div class="conflict-source"><b>${esc(s.value_raw)}</b> — ${sourceSpan(s)}
      <div class="quote">“${esc(s.quote)}”</div></div>`).join("");
  return `<div><b>${esc(metricName(c.metric, c.metric_label))}</b> as of ${esc(c.as_of_date)}:
    ${c.values.length} different values. Not resolved — check the sources.${sources}</div>`;
}

function renderChecks(checks) {
  const parts = [];
  parts.push(`<div class="check-group"><div class="box ${checks.conflicts.length ? "danger" : ""}">
    <div class="box-title">Conflicts (${checks.conflicts.length})</div>
    ${checks.conflicts.length ? checks.conflicts.map(conflictHtml).join("<hr>") : '<span class="muted">None detected.</span>'}
  </div></div>`);
  parts.push(`<div class="check-group"><div class="box ${checks.missing.length ? "warn" : ""}">
    <div class="box-title">Missing key metrics (${checks.missing.length})</div>
    ${checks.missing.length ? `<ul>${checks.missing.map((m) =>
      `<li>${esc(metricName(m))}: Not found in the provided materials</li>`).join("")}</ul>` : '<span class="muted">None.</span>'}
  </div></div>`);
  if (checks.unclear_basis.length) {
    parts.push(`<div class="check-group"><div class="box warn">
      <div class="box-title">Unclear basis (${checks.unclear_basis.length})</div>
      <ul>${checks.unclear_basis.map((u) => `<li>${esc(metricName(u.metric, u.metric_label))}: ${esc(u.value_raw)}
        — ${esc(u.reason)} (${sourceSpan(u)})</li>`).join("")}</ul>
    </div></div>`);
  }
  $("checks").innerHTML = parts.join("");
}

// ---------------------------------------------------------------- Q&A

function chatEntries() {
  return (state.chats[state.companyId] ||= []);
}

function renderChat() {
  const chat = $("chat");
  const entries = chatEntries();
  if (!entries.length) {
    chat.innerHTML = '<div class="empty-state">Ask a question about this company, or try a suggestion below.</div>';
    return;
  }
  chat.innerHTML = entries.map((e, i) => `
    <div class="question">${esc(e.question)}</div>
    ${e.pending ? '<div class="answer pending">Searching the materials and checking sources…</div>'
      : e.error ? `<div class="answer error">${esc(e.error)}</div>`
      : answerHtml(e.answer, i)}`).join("");
  chat.querySelectorAll("[data-toggle]").forEach((btn) => {
    btn.onclick = () => {
      const box = document.getElementById(btn.dataset.toggle);
      box.hidden = !box.hidden;
      btn.textContent = box.hidden ? btn.dataset.show : "Hide evidence";
    };
  });
  chat.scrollTop = chat.scrollHeight;
}

function evidenceHtml(citations) {
  return citations.map((c) => `
    <div class="evidence-item">
      <div class="evidence-loc"><b>${esc(c.filename)}</b> · ${esc(c.loc_label.replace(c.filename, "").trim())}
        ${c.doc_date ? ` · material date ${esc(c.doc_date)}` : ""}</div>
      <div class="evidence-text">${highlight(c.chunk_text, c.quote)}</div>
    </div>`).join("");
}

function answerHtml(a, index) {
  const claims = a.claims.map((claim, j) => {
    const id = `ev-${index}-${j}`;
    const label = `Show evidence (${claim.citations.length})`;
    return `<div class="claim">${esc(claim.text)}
      <div><button type="button" class="link small" data-toggle="${id}" data-show="${label}">${label}</button></div>
      <div class="evidence" id="${id}" hidden>${evidenceHtml(claim.citations)}</div></div>`;
  }).join("");
  const open = a.open_items.length ? `<div class="box warn"><div class="box-title">Open items</div>
    <ul>${a.open_items.map((o) => `<li>${esc(o)}</li>`).join("")}</ul></div>` : "";
  const conflicts = a.conflicts.length ? `<div class="box danger"><div class="box-title">Conflicting information</div>
    ${a.conflicts.map(conflictHtml).join("<hr>")}</div>` : "";
  const unclear = a.unclear_basis.length ? `<div class="box warn"><div class="box-title">Unclear basis</div>
    <ul>${a.unclear_basis.map((u) => `<li>${esc(metricName(u.metric, u.metric_label))}: ${esc(u.value_raw)} — ${esc(u.reason)}</li>`).join("")}</ul></div>` : "";
  const tools = [...new Set(a.tool_calls.map((t) => t.tool))].join(", ") || "none";
  return `<div class="answer">
    ${claims || '<div class="muted">No supported claims.</div>'}
    ${conflicts}${unclear}${open}
    <div class="answer-foot"><span>Tools: ${esc(tools)}</span>
      <a href="/api/runs/${esc(a.run_id)}/log" target="_blank" rel="noopener">View agent log</a></div>
  </div>`;
}

async function ask(question) {
  if (!question || !state.companyId) return;
  const companyId = state.companyId;
  const entry = { question, pending: true };
  chatEntries().push(entry);
  renderChat();
  $("question").value = "";
  $("ask-button").disabled = true;
  try {
    entry.answer = await api(`/api/companies/${companyId}/ask`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }),
    });
  } catch (err) {
    entry.error = err.message;
  } finally {
    entry.pending = false;
    $("ask-button").disabled = false;
    if (companyId === state.companyId) renderChat();
  }
}

$("ask-form").onsubmit = (e) => { e.preventDefault(); ask($("question").value.trim()); };
$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask($("question").value.trim()); }
});
document.querySelectorAll("#suggestions .chip").forEach((chip) => {
  chip.onclick = () => { if (state.companyId) ask(chip.textContent); };
});

// ---------------------------------------------------------------- brief

$("brief-form").onsubmit = async (e) => {
  e.preventDefault();
  if (!state.companyId) return;
  $("brief-button").disabled = true;
  setStatus("brief-status", "Writing the brief and checking every source…");
  try {
    const brief = await api(`/api/companies/${state.companyId}/brief`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ requirements: $("brief-requirements").value.trim() }),
    });
    state.lastBrief = brief;
    setStatus("brief-status", "");
    showBrief(brief);
  } catch (err) {
    setStatus("brief-status", err.message, "error");
  } finally {
    $("brief-button").disabled = false;
  }
};

function cites(citations) {
  return citations.map((c) => `<span class="src" title="${esc(c.quote)}">${esc(c.loc_label)}</span>`).join(", ");
}

function showBrief(b) {
  const sections = b.sections.map((s) => `<h3>${esc(s.title)}</h3><ul>${s.items.map((i) =>
    `<li>${esc(i.text)} <span class="cite">[${cites(i.citations)}]</span></li>`).join("")}</ul>`).join("");
  const metrics = b.key_metrics.length ? `<table><thead><tr><th>Metric</th><th>Value</th><th>As of</th><th>Source</th></tr></thead>
    <tbody>${b.key_metrics.map((m) => `<tr><td>${esc(metricName(m.metric))}</td><td class="num">${formatValue(m)}</td>
    <td class="num">${esc(m.as_of_date)}</td><td>${sourceSpan(m)}</td></tr>`).join("")}</tbody></table>`
    : '<div class="muted">No key metric with a stated as-of date.</div>';
  const comms = b.recent_communications.length ? `<ul>${b.recent_communications.map((c) =>
    `<li><b>${esc(c.date)}</b> with ${esc(c.contact)}: ${esc(c.summary)} <i>Next:</i> ${esc(c.next_steps)}
      <span class="cite">[${sourceSpan(c)}]</span></li>`).join("")}</ul>`
    : '<div class="muted">CRM history: Not found in the provided materials.</div>';
  const missing = b.missing.length ? `<ul>${b.missing.map((m) => `<li>${esc(metricName(m.metric))}: Not found in the provided materials</li>`).join("")}</ul>`
    : '<div class="muted">None of the key metrics are missing.</div>';
  const conflicts = b.conflicts.length ? b.conflicts.map(conflictHtml).join("<hr>") : '<div class="muted">No conflicts detected.</div>';
  const unclear = b.unclear_basis.length ? `<h3>Unclear basis</h3><ul>${b.unclear_basis.map((u) =>
    `<li>${esc(metricName(u.metric, u.metric_label))}: ${esc(u.value_raw)} — ${esc(u.reason)} <span class="cite">[${sourceSpan(u)}]</span></li>`).join("")}</ul>` : "";
  const open = b.open_items.length ? `<h3>Open items</h3><ul>${b.open_items.map((o) => `<li>${esc(o)}</li>`).join("")}</ul>` : "";

  $("brief-title").textContent = `Company brief: ${b.company}`;
  $("brief-body").innerHTML = `
    <div class="muted small">Requirements: ${esc(b.requirements)} · Generated ${esc(b.generated_at)} · Hover a source for the exact quote.</div>
    ${sections}
    <h3>Key metrics</h3>${metrics}
    <h3>Recent communications</h3>${comms}
    <h3>Missing information</h3>${missing}
    <h3>Conflicting information</h3>${conflicts}
    ${unclear}${open}`;
  $("brief-overlay").hidden = false;
}

$("brief-close").onclick = () => { $("brief-overlay").hidden = true; };
$("brief-overlay").onclick = (e) => { if (e.target === $("brief-overlay")) $("brief-overlay").hidden = true; };
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("brief-overlay").hidden = true; });
$("brief-copy").onclick = async () => {
  if (!state.lastBrief) return;
  try { await navigator.clipboard.writeText(state.lastBrief.markdown); $("brief-copy").textContent = "Copied"; }
  catch { $("brief-copy").textContent = "Copy failed"; }
  setTimeout(() => { $("brief-copy").textContent = "Copy Markdown"; }, 1500);
};
$("brief-download").onclick = () => {
  if (!state.lastBrief) return;
  const blob = new Blob([state.lastBrief.markdown], { type: "text/markdown" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `brief-${state.lastBrief.company.toLowerCase().replace(/[^a-z0-9]+/g, "-")}.md`;
  a.click();
  URL.revokeObjectURL(a.href);
};

// ---------------------------------------------------------------- start

loadHealth();
loadCompanies().catch((err) => { $("company-list").innerHTML = `<li class="status error">${esc(err.message)}</li>`; });
