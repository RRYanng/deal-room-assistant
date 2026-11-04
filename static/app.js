// Deal Room Assistant - conversation-first UI. Plain JavaScript, no build step.

const state = {
  companies: [],
  current: null,        // {id, name} or null: the Company selector; questions naming another company change it
  entries: [],          // the conversation
  issues: [],
  drawer: null,         // null, {view: "library"} or {view: "issues", companyId}
  openLibrary: new Set(),
  openEvidence: new Set(),
  upload: null,         // the file and suggestions behind the open confirmation dialog
  lastBrief: null,
};

let nextEntryId = 1;

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

const postJson = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

const METRIC_NAMES = {
  registered_customers: "Registered customers",
  paying_customers: "Paying customers",
  revenue: "Revenue",
  funding_raised: "Funding raised",
  other: "Other",
};
const metricName = (m, label) => (m === "other" && label ? label : METRIC_NAMES[m] || m);
const DOC_TYPES = { deck: "Deck", notes: "Notes", crm: "CRM" };
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const locationOf = (s) => s.loc_label.replace(s.filename, "").trim();

// Highlight the quotes inside the chunk text, tolerating whitespace differences. Quotes may overlap.
function highlight(text, quotes) {
  const ranges = [];
  for (const quote of quotes) {
    const words = String(quote || "").trim().split(/\s+/).filter(Boolean);
    if (!words.length) continue;
    const pattern = new RegExp(words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s+"));
    const m = pattern.exec(text);
    if (m) ranges.push([m.index, m.index + m[0].length]);
  }
  ranges.sort((a, b) => a[0] - b[0]);
  let html = "";
  let pos = 0;
  for (const [start, end] of ranges) {
    if (end <= pos) continue;
    const from = Math.max(start, pos);
    html += esc(text.slice(pos, from)) + "<mark>" + esc(text.slice(from, end)) + "</mark>";
    pos = end;
  }
  return html + esc(text.slice(pos));
}

function sourceSpan(s) {
  return `<span class="src" title="${esc(s.quote)}">${esc(s.loc_label)}</span>`;
}

// ---------------------------------------------------------------- shared data

function setCurrent(company) {
  state.current = company;
  $("company-select").value = company ? String(company.id) : "";
}

function renderCompanySelect() {
  $("company-select").innerHTML = '<option value="">Auto-detect</option>' +
    state.companies.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  $("company-select").value = state.current ? String(state.current.id) : "";
}

$("company-select").onchange = () => {
  const company = state.companies.find((c) => String(c.id) === $("company-select").value);
  state.current = company ? { id: company.id, name: company.name } : null;
};

async function loadCompanies() {
  state.companies = await api("/api/companies");
  renderCompanySelect();
}

async function loadIssues() {
  state.issues = await api("/api/issues");
  const total = state.issues.reduce((sum, c) => sum + c.total, 0);
  $("issues-badge").textContent = total;
  $("issues-badge").hidden = !total;
}

// Issues are computed by the server on every request, so re-fetching keeps everything current.
async function refreshAfterChange() {
  await Promise.all([loadCompanies(), loadIssues()]);
  await renderDrawer();
}

async function loadHealth() {
  try {
    const h = await api("/api/health");
    $("brand").title = `Q&A: ${h.llm_chat_model} · Extraction: ${h.llm_extract_model}`;
  } catch { $("brand").title = "API unavailable"; }
}

// ---------------------------------------------------------------- conversation

function pushEntry(entry) {
  entry.id = nextEntryId++;
  state.entries.push(entry);
  renderChat();
  return entry;
}

function updateEntry(entry, fields) {
  for (const key of ["pending", "error", "answer", "lines", "brief"]) delete entry[key];
  Object.assign(entry, fields);
  renderChat();
}

function renderChat() {
  const chat = $("chat");
  if (!state.entries.length) {
    chat.innerHTML = '<div class="empty-state">Upload files or ask about a company by name</div>';
    return;
  }
  chat.innerHTML = state.entries.map(entryHtml).join("");
  chat.scrollTop = chat.scrollHeight;
}

function entryHtml(e) {
  if (e.kind === "user") return `<div class="question">${esc(e.text)}</div>`;
  if (e.pending) return `<div class="answer pending">${esc(e.pending)}</div>`;
  if (e.error) return `<div class="answer error">${esc(e.error)}</div>`;
  if (e.kind === "message") return `<div class="answer">${esc(e.text)}</div>`;
  if (e.kind === "answer") {
    return `<div class="turn"><div class="answering-about">Answering about: <b>${esc(e.company.name)}</b></div>
      ${answerHtml(e.answer, e.id)}</div>`;
  }
  if (e.kind === "choose") {
    const buttons = e.companies.map((c) => `<button type="button" class="secondary"
      data-choose="${e.id}" data-company="${c.id}" ${e.chosen ? "disabled" : ""}>${esc(c.name)}</button>`).join("");
    const none = e.companies.length ? "" : `<div class="muted small">No companies yet. Switch to Upload to add a file first.</div>`;
    return `<div class="answer">${esc(e.message)}<div class="choices">${buttons}</div>${none}</div>`;
  }
  if (e.kind === "note") {
    const lines = e.lines.map((l) => `<div class="note-line">${esc(l.text)}</div>`).join("");
    const link = e.issuesCompany ? `<div class="answer-foot"><button type="button" class="link small"
      data-open-issues="${e.issuesCompany}">View issues</button></div>` : "";
    return `<div class="answer">${lines}${link}</div>`;
  }
  if (e.kind === "brief") {
    return `<div class="answer">Brief for <b>${esc(e.brief.company)}</b> is ready.
      <div class="answer-foot"><button type="button" class="link small" data-open-brief="${e.id}">Open brief</button></div></div>`;
  }
  return "";
}

function evidenceHtml(evidence, supports) {
  return evidence.map((e) => `
    <div class="evidence-item">
      <div class="evidence-loc"><b>${esc(e.filename)}</b> · ${esc(locationOf(e))}
        ${e.doc_date ? ` · material date ${esc(e.doc_date)}` : ""}${supports ? ` · ${supports(e)}` : ""}</div>
      <div class="evidence-text">${highlight(e.chunk_text, e.quotes)}</div>
    </div>`).join("");
}

// One evidence panel per group of claims that share a source passage, so no passage appears twice.
function claimGroupHtml(a, group, id) {
  const open = state.openEvidence.has(id);
  const show = `Show evidence (${group.evidence.length})`;
  // Claims need numbers only when a passage supports some of the group's claims but not all.
  const numbered = group.evidence.some((e) => e.claims.length < group.claims.length);
  const num = (i) => group.claims.indexOf(i) + 1;
  const texts = group.claims.map((i) => `<div class="claim-text">
    ${numbered ? `<span class="claim-num">${num(i)}</span> ` : ""}${esc(a.claims[i].text)}</div>`).join("");
  const supports = numbered ? (e) => `supports ${e.claims.map(num).join(", ")}` : null;
  return `<div class="claim">${texts}
    <div><button type="button" class="link small" data-toggle="${id}" data-show="${show}">${open ? "Hide evidence" : show}</button></div>
    <div class="evidence" id="${id}" ${open ? "" : "hidden"}>${evidenceHtml(group.evidence, supports)}</div></div>`;
}

function conflictHtml(c) {
  const sources = c.sources.map((s) => `
    <div class="conflict-source"><b>${esc(s.display_value)}</b> — ${sourceSpan(s)}
      <div class="quote">“${esc(s.quote)}”</div></div>`).join("");
  return `<div><b>${esc(metricName(c.metric, c.metric_label))}</b> as of ${esc(c.as_of_date)}:
    ${c.values.length} different values. Not resolved — check the sources.${sources}</div>`;
}

function answerHtml(a, entryId) {
  const claims = a.evidence_groups.map((g, j) => claimGroupHtml(a, g, `ev-${entryId}-${j}`)).join("");
  const open = a.open_items.length ? `<div class="box warn"><div class="box-title">Open items</div>
    <ul>${a.open_items.map((o) => `<li>${esc(o)}</li>`).join("")}</ul></div>` : "";
  const conflicts = a.conflicts.length ? `<div class="box danger"><div class="box-title">Conflicting information</div>
    ${a.conflicts.map(conflictHtml).join("<hr>")}</div>` : "";
  const unclear = a.unclear_basis.length ? `<div class="box warn"><div class="box-title">Unclear basis</div>
    <ul>${a.unclear_basis.map((u) => `<li>${esc(metricName(u.metric, u.metric_label))}: ${esc(u.display_value)} — ${esc(u.reason)}</li>`).join("")}</ul></div>` : "";
  const tools = [...new Set(a.tool_calls.map((t) => t.tool))].join(", ") || "none";
  return `<div class="answer">
    ${claims || '<div class="muted">No answer found in this company\'s materials.</div>'}
    ${conflicts}${unclear}${open}
    <div class="answer-foot"><span>Tools: ${esc(tools)}</span>
      <a href="/api/runs/${esc(a.run_id)}/log" target="_blank" rel="noopener">View agent log</a></div>
  </div>`;
}

$("chat").addEventListener("click", (e) => {
  const button = e.target.closest("button");
  if (!button) return;
  if (button.dataset.toggle) {
    const box = document.getElementById(button.dataset.toggle);
    box.hidden = !box.hidden;
    button.textContent = box.hidden ? button.dataset.show : "Hide evidence";
    if (box.hidden) state.openEvidence.delete(button.dataset.toggle);
    else state.openEvidence.add(button.dataset.toggle);
  } else if (button.dataset.choose) {
    chooseCompany(Number(button.dataset.choose), Number(button.dataset.company));
  } else if (button.dataset.openIssues) {
    openDrawer({ view: "issues", companyId: Number(button.dataset.openIssues) });
  } else if (button.dataset.openBrief) {
    showBrief(state.entries.find((x) => x.id === Number(button.dataset.openBrief)).brief);
  }
});

// ---------------------------------------------------------------- ask

function setAsking(busy) {
  $("ask-button").disabled = busy;
  document.querySelectorAll("#suggestions .chip").forEach((chip) => { chip.disabled = busy; });
}

async function ask(question) {
  if (!question || $("ask-button").disabled) return;
  pushEntry({ kind: "user", text: question });
  $("question").value = "";
  const entry = pushEntry({ kind: "answer", pending: "Searching the materials and checking sources…" });
  setAsking(true);
  try {
    const r = await postJson("/api/ask", { question, last_company_id: state.current ? state.current.id : null });
    if (r.resolution === "out_of_scope") {
      updateEntry(entry, { kind: "message", text: r.message });
    } else if (r.resolution === "answered") {
      setCurrent(r.company);
      updateEntry(entry, { company: r.company, answer: r.answer });
    } else {
      updateEntry(entry, { kind: "choose", question, message: r.message, companies: r.companies });
    }
  } catch (err) {
    updateEntry(entry, { error: err.message });
  } finally {
    setAsking(false);
  }
}

// The user picked a company by hand, so name matching is skipped for this question.
async function chooseCompany(entryId, companyId) {
  const choice = state.entries.find((x) => x.id === entryId);
  const company = choice.companies.find((c) => c.id === companyId);
  choice.chosen = true;
  setCurrent(company);
  const entry = pushEntry({ kind: "answer", pending: "Searching the materials and checking sources…" });
  setAsking(true);
  try {
    const answer = await postJson(`/api/companies/${company.id}/ask`, { question: choice.question });
    if (answer.out_of_scope) updateEntry(entry, { kind: "message", text: "I can only answer questions about the uploaded company materials." });
    else updateEntry(entry, { company, answer });
  } catch (err) {
    updateEntry(entry, { error: err.message });
  } finally {
    setAsking(false);
  }
}

$("ask-pane").onsubmit = (e) => { e.preventDefault(); ask($("question").value.trim()); };
$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask($("question").value.trim()); }
});
document.querySelectorAll("#suggestions .chip").forEach((chip) => {
  chip.onclick = () => ask(chip.textContent);
});

function setMode(mode) {
  $("mode-ask").classList.toggle("active", mode === "ask");
  $("mode-upload").classList.toggle("active", mode === "upload");
  $("mode-ask").setAttribute("aria-pressed", mode === "ask");
  $("mode-upload").setAttribute("aria-pressed", mode === "upload");
  $("ask-pane").hidden = mode !== "ask";
  $("upload-pane").hidden = mode !== "upload";
}
$("mode-ask").onclick = () => { setMode("ask"); $("question").focus(); };
$("mode-upload").onclick = () => setMode("upload");

// ---------------------------------------------------------------- upload

const NEW_COMPANY = "new";

async function analyzeFile(file) {
  const zone = $("dropzone");
  const text = $("dropzone-text");
  const idle = text.innerHTML;
  zone.classList.add("busy");
  text.textContent = `Reading ${file.name}…`;
  try {
    const form = new FormData();
    form.append("file", file);
    const suggestion = await api("/api/uploads/analyze", { method: "POST", body: form });
    await loadCompanies();
    openUploadDialog(file, suggestion);
  } catch (err) {
    pushEntry({ kind: "note", lines: [{ text: `Could not read ${file.name}: ${err.message}` }] });
  } finally {
    zone.classList.remove("busy");
    text.innerHTML = idle;
    $("upload-file").value = "";
  }
}

function companyHint(c) {
  if (c.model_error) return "The file could not be checked for a company. You can still choose one.";
  if (c.source === "none") return "No company recognized in this file. Please choose one.";
  const from = { "file name": "from the file name", "file content": "from the file's content", model: "by the model, from the file's content" };
  return `Suggested: ${c.name} (${from[c.source]}).`;
}

function openUploadDialog(file, s) {
  state.upload = { file, suggestion: s };
  const options = state.companies.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  $("upload-company").innerHTML = `<option value="">Choose a company…</option>${options}
    <option value="${NEW_COMPANY}">+ New company…</option>`;
  // With nothing recognized in the file, start from the company selected at the top of the page.
  const fallback = state.current ? String(state.current.id) : "";
  $("upload-company").value = s.company.existing ? String(s.company.id) : s.company.name ? NEW_COMPANY : fallback;
  $("upload-new-company").value = s.company.existing ? "" : s.company.name || "";
  $("upload-filename").textContent = s.filename;
  $("upload-company-why").textContent = companyHint(s.company);

  // A CSV can only be CRM records, and CRM records can only be a CSV.
  const isCsv = s.filename.toLowerCase().endsWith(".csv");
  for (const option of $("upload-type").options) option.disabled = (option.value === "crm") !== isCsv;
  $("upload-type").value = s.doc_type;
  $("upload-type-why").textContent = `Suggested from: ${s.doc_type_why}.`;
  $("upload-date").value = s.doc_date || "";
  $("upload-date-why").textContent = s.doc_date ? `Suggested from: ${s.doc_date_why}.` : "No date found. Add one if you know it.";

  syncNewCompanyField();
  renderUploadWarnings(s.warnings);
  $("upload-dialog").showModal();
}

function chosenCompanyName() {
  const value = $("upload-company").value;
  if (value === NEW_COMPANY) return $("upload-new-company").value.trim();
  const company = state.companies.find((c) => String(c.id) === value);
  return company ? company.name : "";
}

function syncNewCompanyField() {
  $("upload-new-company").hidden = $("upload-company").value !== NEW_COMPANY;
}

function renderUploadWarnings(warnings) {
  const modelError = state.upload?.suggestion.company.model_error;
  const notices = (modelError ? [{ kind: "model_error", message: modelError }] : []).concat(warnings);
  $("upload-warnings").innerHTML = notices.map((w) =>
    `<div class="warning" role="alert" data-kind="${esc(w.kind)}">${esc(w.message)}</div>`).join("");
  const name = chosenCompanyName();
  $("upload-confirm").disabled = !name;
  $("upload-confirm").textContent = !name ? "Add" : warnings.length ? "Add anyway" : `Add to ${name}`;
}

let warningRequest = 0;

async function refreshUploadWarnings() {
  if (!state.upload) return;
  const request = ++warningRequest;
  const s = state.upload.suggestion;
  const warnings = await postJson("/api/uploads/warnings", {
    chosen_company: chosenCompanyName(),
    suggested_company: s.company.name,
    mentioned: s.mentioned.map((m) => m.name),
    filename: s.filename,
  }).catch(() => []);
  if (request === warningRequest) renderUploadWarnings(warnings);
}

$("upload-company").onchange = () => { syncNewCompanyField(); refreshUploadWarnings(); };
let newCompanyTimer = null;
$("upload-new-company").oninput = () => {
  renderUploadWarnings([]);
  clearTimeout(newCompanyTimer);
  newCompanyTimer = setTimeout(refreshUploadWarnings, 250);
};
function closeUploadDialog() {
  state.upload = null;
  $("upload-dialog").close();
}

$("upload-cancel").onclick = closeUploadDialog;
$("upload-dialog").addEventListener("cancel", closeUploadDialog); // Escape

$("upload-form").onsubmit = (e) => {
  e.preventDefault();
  const name = chosenCompanyName();
  if (!name || !state.upload) return;
  const details = {
    file: state.upload.file,
    companyValue: $("upload-company").value,
    companyName: name,
    docType: $("upload-type").value,
    docDate: $("upload-date").value,
  };
  closeUploadDialog();
  storeFile(details);
};

async function storeFile({ file, companyValue, companyName, docType, docDate }) {
  pushEntry({ kind: "user", text: `Upload ${file.name} to ${companyName}` });
  const entry = pushEntry({ kind: "note", pending: `Storing ${file.name} and extracting metrics…` });
  try {
    const company = companyValue === NEW_COMPANY
      ? await postJson("/api/companies", { name: companyName })
      : { id: Number(companyValue), name: companyName };
    await loadIssues();
    const before = state.issues.find((c) => c.id === company.id);

    const form = new FormData();
    form.append("file", file);
    form.append("doc_type", docType);
    if (docDate) form.append("doc_date", docDate);
    const r = await api(`/api/companies/${company.id}/documents`, { method: "POST", body: form });

    setCurrent(company);
    await refreshAfterChange();
    const after = state.issues.find((c) => c.id === company.id);
    const lines = [{ text: `Stored ${r.filename} in ${company.name}${r.replaced ? " (replaced the previous version)" : ""}: ${plural(r.chunks, "chunk")}.` }];
    lines.push({ text: extractionText(r.extraction) });
    const changed = before && before.total !== after.total && companyValue !== NEW_COMPANY;
    lines.push({ text: `${company.name} now has ${plural(after.total, "issue")}${changed ? ` (was ${before.total})` : ""}.` });
    updateEntry(entry, { lines, issuesCompany: company.id });
    setMode("ask");
  } catch (err) {
    updateEntry(entry, { error: `${file.name} was not stored: ${err.message}` });
    await refreshAfterChange();
  }
}

function extractionText(x) {
  if (!x) return "";
  if (x.status === "ok") {
    const dropped = x.dropped.length ? `, ${x.dropped.length} dropped (failed quote check)` : "";
    return `Metrics: ${x.kept} extracted${dropped}.`;
  }
  return `Metric extraction failed: ${x.error}`;
}

$("upload-file").onchange = () => { if ($("upload-file").files[0]) analyzeFile($("upload-file").files[0]); };
const zone = $("dropzone");
["dragenter", "dragover"].forEach((type) => zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.add("over"); }));
["dragleave", "drop"].forEach((type) => zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.remove("over"); }));
zone.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) analyzeFile(e.dataTransfer.files[0]); });

// ---------------------------------------------------------------- drawer: Library and Issues

function openDrawer(drawer) {
  state.drawer = drawer;
  renderDrawer();
}

function closeDrawer() {
  state.drawer = null;
  renderDrawer();
}

function toggleDrawer(view) {
  if (state.drawer && state.drawer.view === view) closeDrawer();
  else openDrawer({ view });
}

$("nav-library").onclick = () => toggleDrawer("library");
$("nav-issues").onclick = () => toggleDrawer("issues");
$("drawer-close").onclick = closeDrawer;

let drawerRequest = 0;

async function renderDrawer() {
  const drawer = state.drawer;
  $("drawer").hidden = !drawer;
  $("nav-library").classList.toggle("active", !!drawer && drawer.view === "library");
  $("nav-issues").classList.toggle("active", !!drawer && drawer.view === "issues");
  if (!drawer) return;
  const request = ++drawerRequest;
  try {
    const html = drawer.view === "library" ? await libraryHtml()
      : drawer.companyId ? await issueDetailHtml(drawer.companyId) : await issueListHtml();
    if (request !== drawerRequest) return; // the user moved on, or something newer is rendering
    $("drawer-title").textContent = drawer.view === "library" ? "Library" : "Issues";
    $("drawer-body").innerHTML = html;
  } catch (err) {
    if (request === drawerRequest) $("drawer-body").innerHTML = `<div class="answer error">${esc(err.message)}</div>`;
  }
}

// Answers come from the buttons, not the dialog's close event: browsers deliver that event with the
// next painted frame, so it can arrive after the dialog has already been opened again.
let answerDelete = null;

function confirmDelete(name) {
  $("delete-message").textContent = `Delete ${name}? Its chunks and metrics are deleted too.`;
  $("delete-dialog").showModal();
  return new Promise((resolve) => { answerDelete = resolve; });
}

function finishDelete(answer) {
  $("delete-dialog").close();
  if (answerDelete) answerDelete(answer);
  answerDelete = null;
}

$("delete-cancel").onclick = () => finishDelete(false);
$("delete-confirm").onclick = () => finishDelete(true);
$("delete-dialog").addEventListener("cancel", () => finishDelete(false)); // Escape

async function libraryHtml() {
  await loadCompanies();
  if (!state.companies.length) return '<div class="muted">No companies yet. Switch to Upload to add a file.</div>';
  const cards = await Promise.all(state.companies.map(async (c) => {
    const open = state.openLibrary.has(c.id);
    const [docs, metrics] = await Promise.all([
      api(`/api/companies/${c.id}/documents`),
      open ? api(`/api/companies/${c.id}/metrics`) : null,
    ]);
    return `<div class="company-card">
      <button type="button" class="company-head" data-library-company="${c.id}" aria-expanded="${open}">
        <span>${esc(c.name)}</span>
        <span class="muted">${plural(docs.length, "file")} · ${open ? "hide" : "show"} key metrics</span>
      </button>
      ${docs.length ? `<ul class="company-files">${docs.map(documentHtml).join("")}</ul>` : ""}
      ${open ? `<div class="company-metrics"><div class="section-title">Key metrics</div>${metricsHtml(metrics)}</div>` : ""}
    </div>`;
  }));
  return cards.join("");
}

function documentHtml(d) {
  return `<li>
    <div class="doc-name">${esc(d.filename)}</div>
    <div class="muted small">
      <span class="badge">${esc(DOC_TYPES[d.doc_type] || d.doc_type)}</span>
      ${esc(d.doc_date || "no date")} · ${plural(d.chunks, "chunk")} ·
      <button type="button" class="link small" data-reextract="${d.id}" data-name="${esc(d.filename)}">re-extract</button> ·
      <button type="button" class="link small danger-link" data-delete="${d.id}" data-name="${esc(d.filename)}">delete</button>
    </div>
  </li>`;
}

function metricsHtml(metrics) {
  if (!metrics.length) return '<div class="muted">No metrics extracted yet.</div>';
  const rows = metrics.map((m) => `
    <tr>
      <td>${esc(metricName(m.metric, m.metric_label))}</td>
      <td class="num">${esc(m.display_value)}</td>
      <td class="num">${m.as_of_date ? esc(m.as_of_date) : '<span class="badge warn">unclear basis</span>'}</td>
      <td>${sourceSpan(m)}</td>
    </tr>`).join("");
  return `<table>
      <thead><tr><th>Metric</th><th>Value</th><th>As of</th><th>Source</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="muted small" style="margin-top:6px">Hover a source to see the exact quote.</div>`;
}

async function issueListHtml() {
  await loadIssues();
  if (!state.issues.length) return '<div class="muted">No companies yet. Switch to Upload to add a file.</div>';
  return state.issues.map((c) => `
    <button type="button" class="issue-row" data-issues-company="${c.id}">
      <span>${esc(c.name)} · ${plural(c.total, "issue")}</span>
      <span class="muted">${plural(c.conflicts, "conflict")}, ${c.missing} missing, ${c.unclear_basis} unclear</span>
    </button>`).join("");
}

function sourceLine(s, extra = "") {
  return `<div class="source-line">
    <span class="source-value">${esc(s.display_value)}</span> ${extra}
    <div class="source-loc">${esc(s.filename)} · ${esc(locationOf(s))}</div>
    <div class="quote">“${esc(s.quote)}”</div>
  </div>`;
}

async function issueDetailHtml(companyId) {
  const [checks] = await Promise.all([api(`/api/companies/${companyId}/checks`), loadIssues()]);
  const company = state.issues.find((c) => c.id === companyId);
  const conflicts = checks.conflicts.map((c) => `
    <div class="issue-card conflict">
      <div class="issue-title">${esc(metricName(c.metric, c.metric_label))} as of ${esc(c.as_of_date)}</div>
      <div class="muted small">${c.values.length} different values. Not resolved — check the sources.</div>
      ${c.sources.map((s) => sourceLine(s)).join("")}
    </div>`).join("");
  const missing = checks.missing.map((m) => `
    <div class="issue-card">
      <div class="issue-title">${esc(metricName(m))}</div>
      <div class="muted small">Not found in the provided materials.</div>
    </div>`).join("");
  const unclear = checks.unclear_basis.map((u) => `
    <div class="issue-card unclear">
      <div class="issue-title">${esc(metricName(u.metric, u.metric_label))}</div>
      <div class="muted small">${esc(u.reason)}</div>
      ${sourceLine(u)}
    </div>`).join("");
  const group = (title, count, body) => `<div class="drawer-section">
    <div class="section-title">${title} (${count})</div>${body || '<div class="muted small">None.</div>'}</div>`;
  return `<button type="button" class="link small" data-issues-back="1">← All companies</button>
    <h2 class="dialog-title" style="margin:10px 0 14px">${esc(company.name)} · ${plural(company.total, "issue")}</h2>
    ${group("Conflicts", checks.conflicts.length, conflicts)}
    ${group("Missing", checks.missing.length, missing)}
    ${group("Unclear basis", checks.unclear_basis.length, unclear)}`;
}

$("drawer-body").addEventListener("click", async (e) => {
  const button = e.target.closest("button");
  if (!button) return;
  const data = button.dataset;
  if (data.libraryCompany) {
    const id = Number(data.libraryCompany);
    if (state.openLibrary.has(id)) state.openLibrary.delete(id);
    else state.openLibrary.add(id);
    renderDrawer();
  } else if (data.issuesCompany) {
    openDrawer({ view: "issues", companyId: Number(data.issuesCompany) });
  } else if (data.issuesBack) {
    openDrawer({ view: "issues" });
  } else if (data.delete) {
    if (!await confirmDelete(data.name)) return;
    try {
      await api(`/api/documents/${data.delete}`, { method: "DELETE" });
      pushEntry({ kind: "note", lines: [{ text: `Deleted ${data.name}.` }] });
      await refreshAfterChange();
    } catch (err) {
      pushEntry({ kind: "note", error: `Could not delete ${data.name}: ${err.message}` });
    }
  } else if (data.reextract) {
    button.disabled = true;
    try {
      const r = await api(`/api/documents/${data.reextract}/extract`, { method: "POST" });
      pushEntry({ kind: "note", lines: [{ text: `${data.name}: ${extractionText(r)}` }] });
      await refreshAfterChange();
    } catch (err) {
      pushEntry({ kind: "note", error: `Could not re-extract ${data.name}: ${err.message}` });
    }
    button.disabled = false;
  }
});

// ---------------------------------------------------------------- brief

async function openBriefDialog() {
  await loadCompanies().catch(() => {});
  renderBriefDialog();
  $("brief-dialog").showModal();
  if (state.current) $("brief-requirements").focus();
}

function renderBriefDialog() {
  const company = state.current;
  $("brief-requirements-label").hidden = !company;
  $("brief-generate").hidden = !company;
  $("brief-company").innerHTML = company
    ? `<div class="muted small">Company</div><div class="current-name">${esc(company.name)}</div>`
    : state.companies.length
      ? `Which company do you mean?<div class="choices">${state.companies.map((c) =>
        `<button type="button" class="secondary" data-brief-company="${c.id}">${esc(c.name)}</button>`).join("")}</div>`
      : '<div class="muted">No companies yet. Switch to Upload to add a file first.</div>';
}

$("brief-button").onclick = openBriefDialog;
$("brief-cancel").onclick = () => $("brief-dialog").close();
$("brief-company").addEventListener("click", (e) => {
  const button = e.target.closest("[data-brief-company]");
  if (!button) return;
  setCurrent(state.companies.find((c) => c.id === Number(button.dataset.briefCompany)));
  renderBriefDialog();
  $("brief-requirements").focus();
});

$("brief-form").onsubmit = (e) => {
  e.preventDefault();
  if (!state.current) return;
  const company = state.current;
  const requirements = $("brief-requirements").value.trim();
  $("brief-dialog").close();
  generateBrief(company, requirements);
};

async function generateBrief(company, requirements) {
  pushEntry({ kind: "user", text: `Generate a brief for ${company.name}${requirements ? `: ${requirements}` : ""}` });
  const entry = pushEntry({ kind: "brief", pending: "Writing the brief and checking every source…" });
  $("brief-button").disabled = true;
  try {
    const brief = await postJson(`/api/companies/${company.id}/brief`, { requirements });
    updateEntry(entry, { brief });
    showBrief(brief);
  } catch (err) {
    updateEntry(entry, { error: err.message });
  } finally {
    $("brief-button").disabled = false;
  }
}

function cites(citations) {
  return citations.map((c) => `<span class="src" title="${esc(c.quote)}">${esc(c.loc_label)}</span>`).join(", ");
}

function showBrief(b) {
  state.lastBrief = b;
  const sections = b.sections.map((s) => `<h3>${esc(s.title)}</h3><ul>${s.items.map((i) =>
    `<li>${esc(i.text)} ${i.calculated ? '<span class="calculated-note">(calculated)</span> ' : ""}${i.note ? `<span class="conflict-note">${esc(i.note)}</span> ` : ""}<span class="cite">[${cites(i.citations)}]</span></li>`).join("")}</ul>`).join("");
  const metrics = b.key_metrics.length ? `<table><thead><tr><th>Metric</th><th>Value</th><th>As of</th><th>Source</th></tr></thead>
    <tbody>${b.key_metrics.map((m) => `<tr><td>${esc(metricName(m.metric))}</td><td class="num">${esc(m.display_value)}</td>
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
    `<li>${esc(metricName(u.metric, u.metric_label))}: ${esc(u.display_value)} — ${esc(u.reason)} <span class="cite">[${sourceSpan(u)}]</span></li>`).join("")}</ul>` : "";
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
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("brief-overlay").hidden) $("brief-overlay").hidden = true;
  else if (state.drawer && !document.querySelector("dialog[open]")) closeDrawer();
});
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

async function start() {
  renderChat();
  setMode("ask");
  loadHealth();
  window.appStarted = true;
  try {
    await Promise.all([loadCompanies(), loadIssues()]);
    if (!state.companies.length) setMode("upload");
  } catch (err) {
    pushEntry({ kind: "note", error: err.message });
  }
}

start();
