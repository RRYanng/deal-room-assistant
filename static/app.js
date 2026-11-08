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
  batch: null,          // the files in the batch table
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

function renderChat({ keepScroll = false } = {}) {
  const chat = $("chat");
  const scrollTop = chat.scrollTop;
  if (!state.entries.length) {
    chat.innerHTML = '<div class="empty-state">Upload files or ask about a company by name</div>';
    return;
  }
  chat.innerHTML = state.entries.map(entryHtml).join("");
  chat.scrollTop = keepScroll ? scrollTop : chat.scrollHeight;
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
    openDrawer(button.dataset.openIssues === "all" ? { view: "issues" }
      : { view: "issues", companyId: Number(button.dataset.openIssues) });
  } else if (button.dataset.openBrief) {
    showBrief(state.entries.find((x) => x.id === Number(button.dataset.openBrief)).brief);
  }
});

// ---------------------------------------------------------------- ask

function setAsking(busy) {
  $("ask-button").disabled = busy;
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

// A file that replaces one with the same name starts from that file's date, unless the new file
// gives a date of its own. The server keeps the old date too if the date is left empty.
function replacedDate(warnings) {
  const replaced = warnings.find((w) => w.kind === "replaces_file");
  return replaced?.doc_date || "";
}

function dateHint(s, date, edited, kept) {
  if (date && !edited && !s.doc_date && date === kept) return "Date of the file this one replaces.";
  if (!date && kept) return `If left empty, the date of the file this one replaces (${kept}) is kept.`;
  if (s.doc_date) return `Suggested from: ${s.doc_date_why}.`;
  return "No date found. Add one if you know it.";
}

function openUploadDialog(file, s) {
  state.upload = { file, suggestion: s, dateEdited: false };
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

  syncNewCompanyField();
  renderUploadWarnings(s.warnings);
  applyReplacedDate(s.warnings);
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
  if (request !== warningRequest) return;
  renderUploadWarnings(warnings);
  applyReplacedDate(warnings);
}

function applyReplacedDate(warnings) {
  state.upload.keptDate = replacedDate(warnings);
  const { suggestion: s, dateEdited, keptDate } = state.upload;
  if (!s.doc_date && !dateEdited) $("upload-date").value = keptDate;
  $("upload-date-why").textContent = dateHint(s, $("upload-date").value, dateEdited, keptDate);
}

$("upload-company").onchange = () => { syncNewCompanyField(); refreshUploadWarnings(); };
$("upload-date").addEventListener("input", () => {
  if (!state.upload) return;
  state.upload.dateEdited = true;
  $("upload-date-why").textContent = dateHint(state.upload.suggestion, $("upload-date").value, true, state.upload.keptDate);
});
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

// One file keeps the single-file dialog. Several files go to the batch table.
function chooseFiles(fileList) {
  const files = [...fileList];
  if (files.length === 1) analyzeFile(files[0]);
  else if (files.length > 1) openBatch(files);
  $("upload-file").value = "";
}

$("upload-file").onchange = () => chooseFiles($("upload-file").files);
const zone = $("dropzone");
["dragenter", "dragover"].forEach((type) => zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.add("over"); }));
["dragleave", "drop"].forEach((type) => zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.remove("over"); }));
zone.addEventListener("drop", (e) => chooseFiles(e.dataTransfer.files));

// ---------------------------------------------------------------- batch upload

const BATCH_POLL_MS = 700;
const BATCH_STATUS = {
  reading: "Reading…", ready: "Ready", "needs-company": "Needs company", warning: "Warning",
  waiting: "Waiting", uploading: "Uploading", stored: "Waiting to analyze", analyzing: "Analyzing",
  done: "Done", failed: "Failed",
};

function companyNameOf(value, newName) {
  if (value === NEW_COMPANY) return (newName || "").trim();
  const company = state.companies.find((c) => String(c.id) === value);
  return company ? company.name : "";
}

const rowCompany = (row) => companyNameOf(row.companyValue, row.newCompany);

function companyOptions(placeholder) {
  return `<option value="">${esc(placeholder)}</option>
    ${state.companies.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}
    <option value="${NEW_COMPANY}">+ New company…</option>`;
}

// Warnings for one row, from the program's checks plus what only the batch can know.
function rowNotices(batch, row) {
  if (row.error) return [{ kind: "failed", message: row.error }];
  if (batch.phase !== "review" || !row.suggestion) return [];
  const notices = [];
  const c = row.suggestion.company;
  const name = rowCompany(row);
  if (!row.companyPicked && c.model_error) notices.push({ kind: "model_error", message: c.model_error });
  else if (!row.companyPicked && c.source === "none") {
    notices.push({ kind: "no_company", message: name
      ? `No company recognized in this file. Check that it belongs to ${name}.`
      : "No company recognized in this file. Choose a company to upload it." });
  }
  notices.push(...row.warnings);
  const clash = batch.rows.some((other) => other !== row && other.status === "ready" && name
    && other.filename === row.filename && rowCompany(other).toLowerCase() === name.toLowerCase());
  if (clash) {
    notices.push({ kind: "replaces_file",
      message: `Another file in this batch is also named ${row.filename} and going to ${name}. The later file replaces the earlier one.` });
  }
  return notices;
}

function rowStatus(batch, row) {
  if (row.status !== "ready" || batch.phase !== "review") return row.status;
  if (!rowCompany(row)) return "needs-company";
  return rowNotices(batch, row).length ? "warning" : "ready";
}

function rowResult(row) {
  if (row.status !== "done") return "";
  return [row.replaced ? "Replaced the previous version." : "", extractionText(row.extraction)].filter(Boolean).join(" ");
}

function renderRowState(batch, row) {
  if (state.batch !== batch) return;
  const body = document.querySelector(`#batch-table tbody[data-row="${row.key}"]`);
  const status = rowStatus(batch, row);
  body.querySelector(".batch-status").dataset.status = status;
  body.querySelector(".batch-status").textContent = BATCH_STATUS[status];
  const result = rowResult(row);
  body.querySelector(".batch-notes").innerHTML = rowNotices(batch, row).map((w) =>
    `<div class="warning" role="alert" data-kind="${esc(w.kind)}">${esc(w.message)}</div>`).join("")
    + (result ? `<div class="batch-result">${esc(result)}</div>` : "");
}

function renderRow(batch, row) {
  if (state.batch !== batch) return;
  const body = document.querySelector(`#batch-table tbody[data-row="${row.key}"]`);
  const s = row.suggestion;
  const locked = batch.phase !== "review" ? "disabled" : "";
  const name = esc(row.filename);
  let fields = '<td colspan="3" class="muted">–</td>';
  if (s) {
    const isCsv = row.filename.toLowerCase().endsWith(".csv");
    const typeOptions = [["deck", "Pitch deck"], ["notes", "Meeting notes"], ["crm", "CRM records"]]
      .map(([value, label]) => `<option value="${value}" ${(value === "crm") !== isCsv ? "disabled" : ""}>${label}</option>`).join("");
    const isNew = row.companyValue === NEW_COMPANY;
    fields = `<td><select data-field="companyValue" aria-label="Company for ${name}" title="${esc(companyHint(s.company))}" ${locked}>${companyOptions("Choose a company…")}</select>
        <input class="batch-new-company" data-field="newCompany" type="text" placeholder="New company name"
          aria-label="New company name for ${name}" value="${esc(row.newCompany)}" autocomplete="off" ${isNew ? "" : "hidden"} ${locked}></td>
      <td><select data-field="docType" aria-label="Material type for ${name}" title="Suggested from: ${esc(s.doc_type_why)}." ${locked}>${typeOptions}</select></td>
      <td><input data-field="docDate" type="date" aria-label="Material date for ${name}" value="${esc(row.docDate)}"
        title="${esc(dateHint(s, row.docDate, row.dateEdited, replacedDate(row.warnings)))}" ${locked}></td>`;
  }
  body.innerHTML = `<tr><td class="batch-file">${name}</td>${fields}<td class="batch-status" data-file="${name}"></td></tr>
    <tr><td colspan="5" class="batch-notes"></td></tr>`;
  if (s) {
    body.querySelector('[data-field="companyValue"]').value = row.companyValue;
    body.querySelector('[data-field="docType"]').value = row.docType;
  }
  renderRowState(batch, row);
}

function renderBatchFooter(batch) {
  if (state.batch !== batch) return;
  const count = (test) => batch.rows.filter(test).length;
  const reading = count((r) => r.status === "reading");
  const ready = batch.rows.filter((r) => r.status === "ready");
  const needCompany = ready.filter((r) => !rowCompany(r)).length;
  const warned = ready.filter((r) => rowCompany(r) && rowNotices(batch, r).length).length;
  const unreadable = count((r) => r.status === "failed");
  const review = batch.phase === "review";
  const finished = count((r) => r.status === "done" || r.status === "failed");

  $("batch-title").textContent = review ? `Confirm ${plural(batch.rows.length, "file")}`
    : batch.phase === "finished" ? "Upload finished" : `Uploading ${plural(batch.rows.length, "file")}`;
  document.querySelector(".batch-set-all").hidden = !review;
  $("batch-set-all").disabled = reading > 0;
  $("batch-cancel").hidden = !review;
  $("batch-confirm").disabled = batch.phase === "sending" || (review && (reading > 0 || !ready.length || needCompany > 0));
  $("batch-confirm").textContent = { finished: "Close", running: "Keep working" }[batch.phase] || "Upload all";

  if (reading) $("batch-summary").textContent = `Reading ${plural(reading, "file")}…`;
  else if (review) {
    $("batch-summary").textContent = [
      needCompany ? `${plural(needCompany, "file")} still ${needCompany === 1 ? "needs" : "need"} a company.` : "",
      warned ? `${plural(warned, "file")} with warnings can still be uploaded.` : "",
      unreadable ? `${plural(unreadable, "file")} could not be read and will be skipped.` : "",
    ].filter(Boolean).join(" ");
  } else if (batch.phase === "finished") {
    const failed = count((r) => r.status === "failed");
    $("batch-summary").textContent = `Finished: ${count((r) => r.status === "done")} done, ${failed} failed.`;
  } else {
    $("batch-summary").textContent = `${finished} of ${batch.rows.length} finished. This goes on in the background if you keep working.`;
  }
}

function renderBatchStates(batch) {
  batch.rows.forEach((row) => renderRowState(batch, row));
  renderBatchFooter(batch);
}

function renderBatch(batch) {
  $("batch-table").querySelectorAll("tbody").forEach((body) => body.remove());
  for (const row of batch.rows) {
    const body = document.createElement("tbody");
    body.dataset.row = row.key;
    $("batch-table").appendChild(body);
    renderRow(batch, row);
  }
  renderBatchFooter(batch);
}

async function openBatch(files) {
  try {
    await loadCompanies();
  } catch (err) {
    pushEntry({ kind: "note", lines: [{ text: `Could not read the files: ${err.message}` }] });
    return;
  }
  const batch = {
    phase: "review",
    rows: files.map((file, key) => ({ key, file, filename: file.name, status: "reading", error: null, suggestion: null,
      companyValue: "", newCompany: "", docType: "", docDate: "", dateEdited: false, warnings: [], warningRequest: 0, companyPicked: false,
      serverRow: null })),
  };
  state.batch = batch;
  $("batch-error").hidden = true;
  $("batch-set-all").innerHTML = companyOptions("Choose…");
  $("batch-set-all-new").hidden = true;
  $("batch-set-all-new").value = "";
  renderBatch(batch);
  $("batch-dialog").showModal();
  for (const row of batch.rows) {
    if (state.batch !== batch) return;
    await analyzeRow(batch, row);
  }
}

async function analyzeRow(batch, row) {
  try {
    const form = new FormData();
    form.append("file", row.file);
    const s = await api("/api/uploads/analyze", { method: "POST", body: form });
    row.suggestion = s;
    row.status = "ready";
    // The company selected at the top of the page wins over what the file suggests.
    row.companyValue = state.current ? String(state.current.id) : s.company.existing ? String(s.company.id) : s.company.name ? NEW_COMPANY : "";
    row.newCompany = !state.current && !s.company.existing ? s.company.name || "" : "";
    row.docType = s.doc_type;
    row.warnings = s.warnings;
    row.docDate = s.doc_date || replacedDate(s.warnings);
  } catch (err) {
    row.status = "failed";
    row.error = err.message;
  }
  renderRow(batch, row);
  renderBatchStates(batch);
  if (state.current && row.suggestion) await refreshRowWarnings(batch, row);
}

async function refreshRowWarnings(batch, row) {
  const request = ++row.warningRequest;
  const s = row.suggestion;
  const warnings = await postJson("/api/uploads/warnings", {
    chosen_company: rowCompany(row),
    suggested_company: s.company.name,
    mentioned: s.mentioned.map((m) => m.name),
    filename: s.filename,
  }).catch(() => []);
  if (request !== row.warningRequest) return;
  row.warnings = warnings;
  applyRowReplacedDate(batch, row);
  renderBatchStates(batch);
}

function applyRowReplacedDate(batch, row) {
  const s = row.suggestion;
  if (s.doc_date || row.dateEdited || batch.phase !== "review" || state.batch !== batch) return;
  row.docDate = replacedDate(row.warnings);
  const input = document.querySelector(`#batch-table tbody[data-row="${row.key}"] [data-field="docDate"]`);
  input.value = row.docDate;
  input.title = dateHint(s, row.docDate, false, row.docDate);
}

let batchWarningTimer = null;

function refreshWarningsSoon(batch, rows) {
  clearTimeout(batchWarningTimer);
  batchWarningTimer = setTimeout(() => rows.forEach((row) => refreshRowWarnings(batch, row)), 250);
}

function editBatchRow(e) {
  const batch = state.batch;
  const field = e.target.dataset.field;
  if (!batch || !field) return;
  const row = batch.rows[Number(e.target.closest("tbody").dataset.row)];
  row[field] = e.target.value;
  if (field === "docDate") {
    row.dateEdited = true;
    e.target.title = dateHint(row.suggestion, row.docDate, true, replacedDate(row.warnings));
  }
  if (field === "companyValue") {
    row.companyPicked = true;
    e.target.closest("td").querySelector('[data-field="newCompany"]').hidden = row.companyValue !== NEW_COMPANY;
    $("batch-set-all").value = "";
    $("batch-set-all-new").hidden = true;
    refreshRowWarnings(batch, row);
    renderBatchStates(batch);
  } else if (field === "newCompany") {
    row.companyPicked = true;
    row.warnings = [];
    refreshWarningsSoon(batch, [row]);
    renderBatchStates(batch);
  }
}

$("batch-table").addEventListener("change", editBatchRow);
$("batch-table").addEventListener("input", (e) => { if (e.target.dataset.field === "newCompany") editBatchRow(e); });

function applySetAll() {
  const batch = state.batch;
  const value = $("batch-set-all").value;
  $("batch-set-all-new").hidden = value !== NEW_COMPANY;
  const newCompany = value === NEW_COMPANY ? $("batch-set-all-new").value.trim() : "";
  if (!batch || !value || (value === NEW_COMPANY && !newCompany)) return;
  const rows = batch.rows.filter((r) => r.status === "ready");
  for (const row of rows) {
    row.companyValue = value;
    row.newCompany = newCompany;
    row.companyPicked = true;
    row.warnings = [];
    renderRow(batch, row);
  }
  refreshWarningsSoon(batch, rows);
  renderBatchStates(batch);
}

$("batch-set-all").onchange = applySetAll;
$("batch-set-all-new").oninput = applySetAll;

function closeBatchDialog() {
  const batch = state.batch;
  state.batch = null; // a batch still being read stops here; one already uploading goes on
  clearTimeout(batchWarningTimer);
  $("batch-dialog").close();
  return batch;
}

$("batch-cancel").onclick = closeBatchDialog;
$("batch-dialog").addEventListener("cancel", closeBatchDialog); // Escape

$("batch-confirm").onclick = () => {
  if (state.batch.phase === "review") uploadBatch(state.batch);
  else closeBatchDialog();
};

function batchLines(batch) {
  return batch.rows.map((row) => {
    const company = row.serverRow?.company?.name || (row.suggestion ? rowCompany(row) : "");
    const detail = row.error || rowResult(row);
    return { text: `${row.filename}${company ? ` → ${company}` : ""}: ${BATCH_STATUS[row.status]}${detail ? `. ${detail}` : ""}` };
  });
}

async function uploadBatch(batch) {
  const rows = batch.rows.filter((r) => r.status === "ready");
  clearTimeout(batchWarningTimer);
  batch.phase = "sending";
  batch.issuesBefore = state.issues;
  rows.forEach((row) => { row.status = "waiting"; });
  rows[0].status = "uploading";
  $("batch-error").hidden = true;
  batch.rows.forEach((row) => renderRow(batch, row));
  renderBatchFooter(batch);

  const form = new FormData();
  rows.forEach((row) => form.append("files", row.file));
  form.append("items", JSON.stringify(rows.map((row) => ({
    company_id: row.companyValue === NEW_COMPANY ? null : Number(row.companyValue),
    new_company: row.companyValue === NEW_COMPANY ? row.newCompany.trim() : null,
    doc_type: row.docType,
    doc_date: row.docDate || null,
  }))));

  let started;
  try {
    await loadIssues();
    batch.issuesBefore = state.issues;
    started = await api("/api/uploads/batch", { method: "POST", body: form });
  } catch (err) { // nothing was stored, so the table can be used again
    batch.phase = "review";
    rows.forEach((row) => { row.status = "ready"; });
    batch.rows.forEach((row) => renderRow(batch, row));
    renderBatchFooter(batch);
    $("batch-error").textContent = `The files were not uploaded: ${err.message}`;
    $("batch-error").hidden = false;
    return;
  }

  batch.phase = "running";
  batch.id = started.batch_id;
  batch.uploading = rows;
  pushEntry({ kind: "user", text: `Upload ${plural(batch.rows.length, "file")}` });
  batch.entry = pushEntry({ kind: "note", lines: batchLines(batch) });
  setMode("ask");
  applyBatchProgress(batch, started);
  pollBatch(batch);
}

function applyBatchProgress(batch, progress) {
  let changed = false;
  batch.uploading.forEach((row, i) => {
    const server = progress.rows[i];
    if (row.status !== server.status) changed = true;
    row.status = server.status;
    row.error = server.error;
    row.replaced = server.replaced;
    row.extraction = server.extraction;
    row.serverRow = server;
  });
  renderBatchStates(batch);
  if (changed) showBatchProgress(batch);
  return changed;
}

function showBatchProgress(batch) {
  batch.entry.lines = batchLines(batch);
  renderChat({ keepScroll: true });
}

async function pollBatch(batch) {
  let progress;
  try {
    progress = await api(`/api/uploads/batch/${batch.id}`);
  } catch (err) {
    for (const row of batch.uploading) {
      if (row.status === "done" || row.status === "failed") continue;
      row.status = "failed";
      row.error = `Lost track of this file (${err.message}). Check the Library for what was stored.`;
    }
    await finishBatch(batch);
    return;
  }
  // Each stored file can already be asked about, and each result can change the counts.
  if (applyBatchProgress(batch, progress)) await refreshAfterChange().catch(() => {});
  if (progress.finished) await finishBatch(batch);
  else setTimeout(() => pollBatch(batch), BATCH_POLL_MS);
}

async function finishBatch(batch) {
  batch.phase = "finished";
  renderBatchStates(batch);
  const companies = new Map(batch.uploading.filter((r) => r.serverRow?.company)
    .map((r) => [r.serverRow.company.id, r.serverRow.company]));
  if (companies.size === 1) setCurrent([...companies.values()][0]);
  await refreshAfterChange().catch(() => {});
  const lines = batchLines(batch);
  const counts = [...companies.values()].map((company) => {
    const before = batch.issuesBefore.find((c) => c.id === company.id);
    const after = state.issues.find((c) => c.id === company.id);
    if (!after) return "";
    const was = before && before.total !== after.total ? ` (was ${before.total})` : "";
    return `${company.name} now has ${plural(after.total, "issue")}${was}.`;
  }).filter(Boolean);
  if (counts.length) lines.push({ text: counts.join(" ") });
  batch.entry.lines = lines;
  batch.entry.issuesCompany = companies.size === 1 ? [...companies.keys()][0] : companies.size ? "all" : null;
  renderChat();
}

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
  if (document.querySelector("dialog[open]")) return; // a dialog handles its own Escape
  if (!$("brief-overlay").hidden) $("brief-overlay").hidden = true;
  else if (!$("claims-overlay").hidden) closeClaims();
  else if (state.drawer) closeDrawer();
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

// ---------------------------------------------------------------- claim check

// The server decides every status; the page only shows it. Nothing is a finding until confirmed.
const CLAIM_STATUS = {
  contradicted: ["Contradicted", "st-bad"],
  possibly_overstated: ["Possibly overstated", "st-warn"],
  externally_supported: ["Externally supported", "st-ok"],
  consistent_with_company_data: ["Consistent with company data", "st-info"],
  unverified: ["Unverified", "st-none"],
};
const EVIDENCE_SOURCE = { internal: "Company materials", external: "Web source", calculation: "Calculation" };
const EVIDENCE_STANCE = { supports: "supports", contradicts: "contradicts", overstates: "suggests overstated" };
const CLAIM_POLL_MS = 1000;

const claims = { companyId: null, data: null, view: "claims", showDismissed: false, timer: null, memo: null, reviewing: null };

async function openClaims() {
  await loadCompanies().catch(() => {});
  const ids = state.companies.map((c) => c.id);
  if (!ids.includes(claims.companyId)) claims.companyId = state.current ? state.current.id : ids[0] ?? null;
  $("claims-company").innerHTML = state.companies.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  $("claims-company").value = claims.companyId ? String(claims.companyId) : "";
  claims.view = "claims";
  $("claims-overlay").hidden = false;
  await loadClaims();
}

function closeClaims() {
  $("claims-overlay").hidden = true;
  stopClaimPoll();
}

async function loadClaims() {
  stopClaimPoll();
  if (!claims.companyId) {
    claims.data = null;
    $("claims-body").innerHTML = '<div class="muted">No companies yet. Switch to Upload to add a deck first.</div>';
    syncClaimButtons();
    return;
  }
  const companyId = claims.companyId;
  try {
    const data = await api(`/api/companies/${companyId}/claims`);
    if (companyId !== claims.companyId) return; // the user picked another company meanwhile
    claims.data = data;
    renderClaims();
    if (data.running) claims.timer = setTimeout(() => pollClaimRun(companyId, data.running.id), CLAIM_POLL_MS);
  } catch (err) {
    $("claims-body").innerHTML = `<div class="answer error">${esc(err.message)}</div>`;
  }
}

function stopClaimPoll() {
  clearTimeout(claims.timer);
  claims.timer = null;
}

async function pollClaimRun(companyId, runId) {
  if (companyId !== claims.companyId || $("claims-overlay").hidden) return;
  try {
    const run = await api(`/api/companies/${companyId}/claim-runs/${runId}`);
    if (run.status === "running") {
      claims.data.running = run;
      if (claims.view === "claims") renderClaims();
      claims.timer = setTimeout(() => pollClaimRun(companyId, runId), CLAIM_POLL_MS);
    } else {
      await loadClaims();
    }
  } catch (err) {
    claims.timer = setTimeout(() => pollClaimRun(companyId, runId), CLAIM_POLL_MS * 3);
  }
}

function syncClaimButtons() {
  const running = !!(claims.data && claims.data.running);
  $("claims-run").disabled = !claims.companyId || running;
  $("claims-run").textContent = running ? "Checking…" : "Run claim check";
  $("claims-memo").textContent = claims.view === "memo" ? "Back to claims" : "Memo";
  $("claims-memo").disabled = !claims.data || !claims.data.run;
}

function safeUrl(url) {
  return /^https?:\/\//i.test(url || "") ? url : null;
}

function siteOf(url) {
  return url.replace(/^https?:\/\/(www\.)?/i, "").split("/")[0];
}

function evidenceItemHtml(e) {
  const url = safeUrl(e.url);
  const where = url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(siteOf(url))}</a>`
    : e.filename ? esc(`${e.filename} · ${locationOf(e)}`) : "";
  return `<div class="ev ev-${esc(e.stance)}">
    <div class="ev-kind">${esc(EVIDENCE_SOURCE[e.source] || e.source)} · ${esc(EVIDENCE_STANCE[e.stance] || e.stance)}</div>
    <div>${esc(e.note)}</div>
    ${e.quote ? `<div class="quote">“${esc(e.quote)}”</div>` : ""}
    ${where ? `<div class="source-loc">${where}</div>` : ""}
  </div>`;
}

function reviewCellHtml(c) {
  const actions = `<div class="review-actions">
      <button type="button" class="secondary small-button" data-review="confirmed" data-fp="${esc(c.fingerprint)}">Confirm</button>
      <button type="button" class="secondary small-button" data-review="dismissed" data-fp="${esc(c.fingerprint)}">Dismiss</button>
    </div>`;
  if (c.review_state === "awaiting_review") {
    const changed = c.evidence_changed
      ? `<div class="muted small">Evidence changed since it was ${esc(c.review.decision)}: ${esc(c.review.reason)}</div>` : "";
    return `<div class="muted small">Awaiting review</div>${changed}${actions}`;
  }
  const label = c.review_state === "confirmed" ? "Confirmed" : "Dismissed";
  return `<div class="review-done ${c.review_state}"><b>${label}</b></div>
    <div class="small">${esc(c.review.reason)}</div>${actions}`;
}

function claimRowHtml(c) {
  const [label, cls] = CLAIM_STATUS[c.status] || [c.status, "st-none"];
  const evidence = c.evidence.length ? c.evidence.map(evidenceItemHtml).join("")
    : '<div class="muted small">No evidence found.</div>';
  return `<tr class="${c.review_state === "dismissed" ? "dismissed" : ""}">
    <td><span class="status ${cls}">${esc(label)}</span></td>
    <td><div class="claim-quote">“${esc(c.quote)}”</div><div class="muted small">${esc(c.filename)} · ${esc(locationOf(c))}</div></td>
    <td>${evidence}</td>
    <td>${reviewCellHtml(c)}</td>
  </tr>`;
}

function runSummaryHtml(d) {
  const parts = [];
  if (d.running) {
    parts.push(`<div class="answer pending">Checking ${esc(d.running.filename)}: ${esc(d.running.stage || "starting")}…</div>`);
  }
  if (d.failed_run) {
    const kept = d.run ? " The results below are from the last check that finished." : "";
    parts.push(`<div class="answer error">The latest claim check failed: ${esc(d.failed_run.error)}${kept}</div>`);
  }
  if (d.run) {
    const when = d.run.finished_at ? ` · ${esc(d.run.finished_at)} UTC` : "";
    parts.push(`<div class="muted small">Last check: ${esc(d.run.filename)}${when}</div>`);
    if (d.run.notes.length) parts.push(`<ul class="run-notes">${d.run.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>`);
  }
  return parts.join("");
}

function renderClaims() {
  syncClaimButtons();
  const d = claims.data;
  if (claims.view === "memo") return renderMemo();
  const head = runSummaryHtml(d);
  if (!d.run) {
    $("claims-body").innerHTML = head + (d.running || d.failed_run ? "" : `<div class="muted">No claim check yet. Run one
      to list the deck's key claims and the evidence for and against each.</div>`);
    return;
  }
  const visible = d.claims.filter((c) => claims.showDismissed || c.review_state !== "dismissed");
  const byStatus = Object.keys(CLAIM_STATUS).map((s) => [s, visible.filter((c) => c.status === s).length])
    .filter(([, n]) => n).map(([s, n]) => `<span class="status ${CLAIM_STATUS[s][1]}">${esc(CLAIM_STATUS[s][0])}: ${n}</span>`);
  const dismissed = d.counts.dismissed
    ? `<button type="button" class="link small" id="claims-toggle-dismissed">${claims.showDismissed ? "Hide" : "Show"}
       dismissed (${d.counts.dismissed})</button>` : "";
  const competitors = d.competitors.length ? `<h3>Competitors found by web research</h3><ul>${d.competitors.map((c) => {
    const url = safeUrl(c.url);
    return `<li><b>${esc(c.name)}</b>${c.description ? ` — ${esc(c.description)}` : ""}
      ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(siteOf(url))}</a>` : ""}</li>`;
  }).join("")}</ul>` : "";
  $("claims-body").innerHTML = `${head}
    <div class="claims-summary">${byStatus.join(" ")}
      <span class="muted small">${d.counts.awaiting_review} awaiting review · ${d.counts.confirmed} confirmed</span> ${dismissed}</div>
    <div class="muted small">Evidence statuses describe the evidence found, not whether a claim is true. Nothing is a
      finding until you confirm it.</div>
    <table class="claims-table">
      <thead><tr><th>Evidence status</th><th>Claim</th><th>Evidence</th><th>Review</th></tr></thead>
      <tbody>${visible.map(claimRowHtml).join("")}</tbody>
    </table>
    ${competitors}`;
}

async function toggleMemo() {
  if (claims.view === "memo") {
    claims.view = "claims";
    return renderClaims();
  }
  claims.view = "memo";
  claims.memo = null;
  renderClaims();
  try {
    claims.memo = await api(`/api/companies/${claims.companyId}/memo`);
  } catch (err) {
    claims.memo = { error: err.message };
  }
  if (claims.view === "memo") renderMemo();
}

function renderMemo() {
  syncClaimButtons();
  const m = claims.memo;
  if (!m) { $("claims-body").innerHTML = '<div class="answer pending">Building the memo…</div>'; return; }
  if (m.error) { $("claims-body").innerHTML = `<div class="answer error">${esc(m.error)}</div>`; return; }
  $("claims-body").innerHTML = `<div class="overlay-actions memo-actions">
      <button type="button" class="secondary" id="memo-copy">Copy Markdown</button>
      <button type="button" class="secondary" id="memo-download">Download .md</button>
    </div>
    <div class="muted small">${m.findings.length} confirmed finding${m.findings.length === 1 ? "" : "s"} ·
      ${m.awaiting_review.length} awaiting review · ${m.dismissed.length} dismissed</div>
    <pre class="memo">${esc(m.markdown)}</pre>`;
}

$("nav-claims").onclick = openClaims;
$("claims-close").onclick = closeClaims;
$("claims-overlay").onclick = (e) => { if (e.target === $("claims-overlay")) closeClaims(); };
$("claims-company").onchange = () => {
  claims.companyId = Number($("claims-company").value) || null;
  claims.view = "claims";
  claims.showDismissed = false;
  loadClaims();
};
$("claims-memo").onclick = toggleMemo;
$("claims-run").onclick = async () => {
  if (!claims.companyId) return;
  $("claims-run").disabled = true;
  try {
    const run = await postJson(`/api/companies/${claims.companyId}/claim-check`, {});
    claims.view = "claims";
    claims.data = { ...(claims.data || { run: null, failed_run: null, claims: [], competitors: [], counts: {} }), running: run };
    renderClaims();
    claims.timer = setTimeout(() => pollClaimRun(claims.companyId, run.id), CLAIM_POLL_MS);
  } catch (err) {
    $("claims-body").insertAdjacentHTML("afterbegin", `<div class="answer error">${esc(err.message)}</div>`);
    syncClaimButtons();
  }
};

$("claims-body").addEventListener("click", async (e) => {
  if (e.target.closest("#claims-toggle-dismissed")) {
    claims.showDismissed = !claims.showDismissed;
    return renderClaims();
  }
  if (e.target.closest("#memo-copy")) {
    const button = $("memo-copy");
    try { await navigator.clipboard.writeText(claims.memo.markdown); button.textContent = "Copied"; }
    catch { button.textContent = "Copy failed"; }
    return setTimeout(() => { if ($("memo-copy")) $("memo-copy").textContent = "Copy Markdown"; }, 1500);
  }
  if (e.target.closest("#memo-download")) {
    const blob = new Blob([claims.memo.markdown], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `memo-${claims.memo.company.toLowerCase().replace(/[^a-z0-9]+/g, "-")}.md`;
    a.click();
    return URL.revokeObjectURL(a.href);
  }
  const button = e.target.closest("[data-review]");
  if (button) openReview(button.dataset.fp, button.dataset.review);
});

// ---------------------------------------------------------------- review dialog

function openReview(fingerprint, decision) {
  const claim = claims.data.claims.find((c) => c.fingerprint === fingerprint);
  if (!claim) return;
  claims.reviewing = { fingerprint, decision, companyId: claims.companyId };
  $("review-title").textContent = decision === "confirmed" ? "Confirm this finding?" : "Dismiss this flag?";
  $("review-claim").textContent = `“${claim.quote}”`;
  $("review-reason").value = "";
  $("review-error").hidden = true;
  $("review-save").disabled = true;
  $("review-save").textContent = decision === "confirmed" ? "Confirm" : "Dismiss";
  $("review-dialog").showModal();
  $("review-reason").focus();
}

$("review-reason").addEventListener("input", () => { $("review-save").disabled = !$("review-reason").value.trim(); });
$("review-cancel").onclick = () => $("review-dialog").close();
$("review-form").onsubmit = async (e) => {
  e.preventDefault();
  const r = claims.reviewing;
  const reason = $("review-reason").value.trim();
  if (!r || !reason) return;
  $("review-save").disabled = true;
  try {
    await postJson(`/api/companies/${r.companyId}/reviews`, { fingerprint: r.fingerprint, decision: r.decision, reason });
    $("review-dialog").close();
    await loadClaims();
  } catch (err) {
    $("review-error").textContent = err.message;
    $("review-error").hidden = false;
    $("review-save").disabled = false;
  }
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
