const state = {
  batches: [],
  selectedBatch: null,
  selectedBatchIds: new Set(),
  deletingBatchIds: new Set(),
  currentPaper: null,
  system: null,
  toolSources: null,
  activeTab: "new",
  pollTimer: null,
  pollBusy: false,
  pollCount: 0,
  systemRequestToken: 0,
  listRequestToken: 0,
  detailRequestToken: 0,
  detailController: null,
  exportItems: [],
  exportRequestToken: 0,
  exportController: null,
  toolSourcesRequestToken: 0,
  toolSourcesController: null,
  zoteroCollectionTokens: {rename: 0, export: 0},
  deletePreviewToken: 0,
  pendingDeleteIds: [],
  deleteOperationId: null,
  deletionActive: false,
  deletionTerminal: false,
  deletePollTimer: null,
  deletePollFailures: 0,
  helpTrigger: null,
  helpPinned: false,
};

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const esc = value => String(value ?? "").replace(/[&<>"']/g, character => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
})[character]);

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body instanceof FormData ? {} : {"Content-Type": "application/json"}),
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let message = await response.text();
    try {
      const payload = JSON.parse(message);
      message = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail || payload);
    } catch (_) {}
    const error = new Error(message || `请求失败（${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return response.headers.get("content-type")?.includes("json") ? response.json() : response.text();
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(node.timer);
  node.timer = setTimeout(() => node.classList.remove("show"), 3800);
}

const STATUS_LABELS = {
  draft: "未开始",
  running: "处理中",
  paused: "已暂停",
  completed: "阶段完成",
  partial_failed: "部分失败",
  failed: "失败",
  queued: "排队中",
  matching: "匹配题录中",
  needs_match: "待确认题录",
  ready: "题录就绪",
  looking_for_pdf: "查找全文中",
  institution_pending: "机构获取中",
  needs_pdf: "待补全文",
  needs_pdf_review: "待确认 PDF",
  pdf_ready: "PDF 就绪",
  endnote_pending: "待写入 Zotero",
  complete: "已完成",
  skipped: "已跳过",
  pending: "等待中",
  stopping: "正在停止",
  deleting: "正在清理",
  missing: "已不存在",
};

const METADATA_LABELS = {
  pending: "等待匹配",
  matching: "匹配中",
  needs_review: "待确认",
  unavailable: "未找到",
  verified: "已确认",
};

const PDF_LABELS = {
  pending: "等待获取",
  searching: "查找中",
  verified: "已验证",
  accepted: "已接受",
  needs_review: "待确认",
  not_downloaded: "待下载",
  not_found: "未找到",
  rejected: "已拒绝",
};

const ZOTERO_LABELS = {
  pending: "等待提交",
  pending_commit: "提交中",
  verified: "已核验",
  uncertain: "待对账",
};

const VERSION_LABELS = {
  published: "正式发表版本",
  accepted: "作者接受稿",
  preprint: "预印本",
  unknown: "版本不明",
  publishedVersion: "正式发表版本",
  acceptedVersion: "作者接受稿",
  submittedVersion: "预印本",
};

const DELETION_LABELS = {
  pending: "等待清理",
  stopping: "正在停止",
  deleting: "正在清理",
  completed: "已清理",
  failed: "清理失败",
  missing: "已不存在",
};

function translated(value, labels, fallback = "未知") {
  return labels[value] || fallback;
}

function statusLabel(value) {
  return translated(value, STATUS_LABELS, "未知状态");
}

function statusTone(value) {
  if (["failed", "partial_failed", "needs_match", "needs_pdf_review", "uncertain", "rejected"].includes(value)) return "error";
  if (["paused", "needs_pdf", "institution_pending", "stopping", "deleting", "pending", "not_found", "not_downloaded"].includes(value)) return "warn";
  if (["draft", "queued", "skipped", "missing"].includes(value)) return "muted";
  return "";
}

function statusChip(value, label = statusLabel(value)) {
  return `<span class="status-chip ${statusTone(value)}">${esc(label)}</span>`;
}

function formatBytes(value) {
  let bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes < 0) bytes = 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (bytes >= 1024 && unit < units.length - 1) {
    bytes /= 1024;
    unit += 1;
  }
  const precision = unit === 0 || bytes >= 100 ? 0 : bytes >= 10 ? 1 : 2;
  return `${bytes.toFixed(precision)} ${units[unit]}`;
}

function setButtonBusy(button, busy, busyText) {
  if (!button) return;
  if (busy) {
    button.dataset.originalText = button.textContent;
    button.textContent = busyText;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  } else {
    button.textContent = button.dataset.originalText || button.textContent;
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

function emptyTaskDetail(title = "选择一个批次", text = "进度与待处理论文会显示在这里") {
  $("#task-detail").classList.remove("loading");
  $("#task-detail").innerHTML = `<div class="empty-state"><span class="empty-icon" aria-hidden="true">⌁</span><strong>${esc(title)}</strong><span>${esc(text)}</span></div>`;
}

function invalidateDetailRequest() {
  state.detailRequestToken += 1;
  state.detailController?.abort();
  state.detailController = null;
}

async function showTab(name) {
  state.activeTab = name;
  $$(".tab").forEach(button => {
    const active = button.dataset.tab === name;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  $$(".tab-panel").forEach(panel => panel.classList.toggle("active", panel.id === `tab-${name}`));
  closeHelp();
  if (name === "tasks") {
    await refreshBatches();
    startPolling();
  } else {
    stopPolling();
  }
  if (name === "tools") await loadToolSources();
  if (name === "settings") await refreshSystemStatus();
}

function renderSystemStatus() {
  const probe = state.system?.zotero || {};
  const badge = $("#system-badge");
  badge.textContent = probe.ready ? "Zotero 已连接" : (probe.enabled ? "Zotero 待授权" : "Zotero 未连接");
  badge.className = `badge ${probe.ready ? "" : probe.enabled ? "warn" : "error"}`;
  $("#system-info").textContent = JSON.stringify(state.system, null, 2);

  const ocr = state.system?.ocr || {};
  const ocrStatus = $("#ocr-status");
  ocrStatus.textContent = ocr.ready ? `Tesseract ${ocr.tesseract_version || "可用"}` : "Tesseract 未就绪";
  ocrStatus.className = `status-pill ${ocr.ready ? "" : "warn"}`;
  renderEnvironmentStatus();
}

async function refreshSystemStatus() {
  const token = ++state.systemRequestToken;
  const system = await api("/api/state");
  if (token !== state.systemRequestToken) return null;
  state.system = system;
  renderSystemStatus();
  return system;
}

async function loadSystem() {
  const system = await refreshSystemStatus();
  if (!system) return;

  const cfg = system.settings || {};
  $("#crossref-email").value = cfg.crossref_email || "";
  $("#unpaywall-email").value = cfg.unpaywall_email || "";
  $("#endnote-library").value = cfg.endnote_library || "";
  $("#source-open-access").checked = (cfg.acquisition_sources || []).includes("open_access");
  $("#source-institution").checked = (cfg.acquisition_sources || []).includes("institution");
  $("#auto-institution").checked = cfg.auto_institution === true;
  $("#auto-commit").checked = cfg.auto_commit !== false;
  $("#login-wait-seconds").value = cfg.login_wait_seconds || 600;
  $("#ocr-enabled").checked = !!cfg.ocr_enabled;
  $("#ocr-languages").value = cfg.ocr_languages || "eng";
  $("#ocr-max-pages").value = cfg.ocr_max_pages || 2;

  fillInstitution(cfg.institution || {}, system.presets || []);

  const credentials = system.credentials || {};
  $("#institution-username").value = credentials.username || "";
  $("#institution-password").value = "";
  $("#credentials-status").textContent = credentials.password_saved
    ? `已保存 ${cfg.institution?.name || "机构"}账号${credentials.username ? ` · ${credentials.username}` : ""}`
    : "尚未保存密码";
}

function renderEnvironmentStatus() {
  const zotero = state.system?.zotero || {};
  const ocr = state.system?.ocr || {};
  const endnote = state.system?.endnote || {};
  const zoteroText = zotero.ready ? `可写入${zotero.version ? ` · ${zotero.version}` : ""}` : zotero.enabled ? "等待授权" : zotero.running ? "本地 API 未启用" : "未运行";
  const endnoteText = endnote.library ? (endnote.library_exists ? "库路径有效" : "库路径无效") : "可导出导入包";
  $("#environment-status").innerHTML = `
    <div class="status-card ${zotero.ready ? "ready" : "warn"}"><strong>Zotero</strong><span>${esc(zoteroText)}</span></div>
    <div class="status-card ${ocr.ready ? "ready" : "warn"}"><strong>OCR</strong><span>${ocr.ready ? "可用" : "未就绪"}</span></div>
    <div class="status-card ${endnote.library && !endnote.library_exists ? "warn" : "ready"}"><strong>EndNote</strong><span>${esc(endnoteText)}</span></div>`;
  const authorize = $("#authorize-zotero");
  authorize.textContent = zotero.ready ? "Zotero 已授权" : "授权 Zotero";
  authorize.disabled = !!zotero.ready;
}

function fillInstitution(institution, presets) {
  const select = $("#institution-preset");
  const current = institution.preset || institution.id || "";
  select.innerHTML = [`<option value="">自定义</option>`].concat(
    presets.map(preset => `<option value="${esc(preset.preset || preset.id)}">${esc(preset.name)}</option>`)
  ).join("");
  select.value = [...select.options].some(option => option.value === current) ? current : "";
  $("#institution-access-type").value = institution.access_type || "ezproxy";
  $("#institution-id").value = institution.id || "";
  $("#institution-name").value = institution.name || "";
  $("#institution-login-url").value = institution.login_url || "";
  $("#institution-login").value = institution.ezproxy_login || "";
  $("#institution-hosts").value = (institution.ezproxy_hosts || []).join(", ");
  $("#institution-openurl").value = institution.openurl || "";
  $("#institution-login-markers").value = (institution.login_url_markers || []).join(", ");
  $("#institution-school-aliases").value = (institution.school_aliases || []).join(", ");
  $("#institution-entity-id").value = institution.entity_id || "";
  $("#institution-publisher-login-urls").value = Object.entries(institution.publisher_login_urls || {})
    .map(([publisher, url]) => `${publisher}=${url}`)
    .join("\n");
  syncInstitutionFields();
}

function syncInstitutionFields() {
  const accessType = $("#institution-access-type").value;
  $$('[data-institution-types]').forEach(node => {
    node.hidden = !node.dataset.institutionTypes.split(/\s+/).includes(accessType);
  });
  $("#carsi-experimental").hidden = accessType !== "carsi_saml";
  const credentialFields = accessType === "ezproxy";
  $("#credentials-form").hidden = !credentialFields;
  $("#institution-username").disabled = !credentialFields;
  $("#institution-password").disabled = !credentialFields;
}

function parsePublisherLoginUrls(value) {
  const urls = {};
  for (const rawLine of value.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const separator = line.indexOf("=");
    if (separator < 1) throw new Error(`出版社登录链接格式错误：${line}`);
    const publisher = line.slice(0, separator).trim().toLowerCase();
    const url = line.slice(separator + 1).trim();
    if (!/^https?:\/\//i.test(url)) throw new Error(`${publisher} 必须填写完整的 http(s) URL`);
    urls[publisher] = url;
  }
  return urls;
}

function renderBatchList() {
  const list = $("#batch-list");
  if (!state.batches.length) {
    list.innerHTML = `<div class="empty-state compact"><span aria-hidden="true">♡</span> 尚无任务</div>`;
    updateBatchSelectionBar();
    return;
  }
  list.innerHTML = state.batches.map(batch => {
    const deleting = state.deletingBatchIds.has(batch.id) || ["pending", "stopping", "deleting"].includes(batch.deletion_status);
    const checked = state.selectedBatchIds.has(batch.id);
    const label = deleting ? statusLabel(batch.deletion_status || "deleting") : statusLabel(batch.status);
    return `<div class="batch-row ${state.selectedBatch === batch.id ? "active" : ""} ${deleting ? "deleting" : ""}">
      <label class="batch-select" aria-label="选择 ${esc(batch.name)}"><input type="checkbox" data-batch-select="${esc(batch.id)}" ${checked ? "checked" : ""} ${deleting ? "disabled" : ""}></label>
      <button class="batch-item" type="button" data-batch-open="${esc(batch.id)}" ${deleting ? "disabled" : ""}>
        <strong>${esc(batch.name)}</strong>
        <span class="batch-meta"><span>${batch.completed || 0}/${batch.total || 0}</span><span class="mini-dot"></span><span>${esc(label)}</span></span>
      </button>
    </div>`;
  }).join("");
  $$('[data-batch-open]').forEach(button => button.addEventListener("click", () => selectBatch(button.dataset.batchOpen)));
  $$('[data-batch-select]').forEach(input => input.addEventListener("change", () => {
    if (input.checked) state.selectedBatchIds.add(input.dataset.batchSelect);
    else state.selectedBatchIds.delete(input.dataset.batchSelect);
    updateBatchSelectionBar();
  }));
  updateBatchSelectionBar();
}

function updateBatchSelectionBar() {
  const available = state.batches.filter(batch => !state.deletingBatchIds.has(batch.id) && !["pending", "stopping", "deleting"].includes(batch.deletion_status));
  const selectedCount = available.filter(batch => state.selectedBatchIds.has(batch.id)).length;
  const selectAll = $("#select-all-batches");
  selectAll.disabled = available.length === 0;
  selectAll.checked = available.length > 0 && selectedCount === available.length;
  selectAll.indeterminate = selectedCount > 0 && selectedCount < available.length;
  $("#selected-batch-count").textContent = `已选 ${selectedCount}`;
  $("#delete-selected-batches").disabled = selectedCount === 0 || state.deletionActive;
}

async function refreshBatches({renderDetail = true, silent = false} = {}) {
  const requestToken = ++state.listRequestToken;
  if (!silent && !state.batches.length) {
    $("#batch-list").innerHTML = `<div class="loading-state"><span class="spinner" aria-hidden="true"></span> 正在加载批次…</div>`;
  }
  try {
    const batches = await api("/api/batches");
    if (requestToken !== state.listRequestToken) return;
    state.batches = batches;
    const existingIds = new Set(batches.map(batch => batch.id));
    state.selectedBatchIds = new Set([...state.selectedBatchIds].filter(id => existingIds.has(id)));
    if (state.selectedBatch && !existingIds.has(state.selectedBatch)) {
      state.selectedBatch = null;
      invalidateDetailRequest();
      emptyTaskDetail("批次已清理", "请选择其他批次");
    }
    renderBatchList();
    if (renderDetail && state.selectedBatch) await renderBatch(state.selectedBatch, {silent});
  } catch (error) {
    if (requestToken !== state.listRequestToken || silent) return;
    $("#batch-list").innerHTML = `<div class="empty-state compact"><strong>加载失败</strong><span>${esc(error.message)}</span><button type="button" data-retry-batches>重试</button></div>`;
    $("[data-retry-batches]")?.addEventListener("click", () => refreshBatches());
  }
}

async function selectBatch(id) {
  if (!id || state.deletingBatchIds.has(id)) return;
  state.selectedBatch = id;
  renderBatchList();
  $("#task-detail").classList.add("loading");
  $("#task-detail").innerHTML = `<div class="loading-state"><span class="spinner" aria-hidden="true"></span> 正在加载详情…</div>`;
  await renderBatch(id);
}

async function renderBatch(batchId = state.selectedBatch, {silent = false} = {}) {
  if (!batchId || batchId !== state.selectedBatch || state.deletingBatchIds.has(batchId)) return;
  const requestToken = ++state.detailRequestToken;
  state.detailController?.abort();
  const controller = new AbortController();
  state.detailController = controller;
  try {
    const batch = await api(`/api/batches/${encodeURIComponent(batchId)}`, {signal: controller.signal});
    if (requestToken !== state.detailRequestToken || state.selectedBatch !== batchId || state.deletingBatchIds.has(batchId)) return;
    renderBatchDetail(batch);
  } catch (error) {
    if (error.name === "AbortError" || requestToken !== state.detailRequestToken || state.selectedBatch !== batchId) return;
    if (error.status === 404) {
      state.selectedBatch = null;
      emptyTaskDetail("批次已不存在", "列表将自动刷新");
      await refreshBatches({renderDetail: false, silent: true});
      return;
    }
    if (!silent) emptyTaskDetail("详情加载失败", error.message);
  } finally {
    if (requestToken === state.detailRequestToken) {
      state.detailController = null;
      $("#task-detail").classList.remove("loading");
    }
  }
}

function renderBatchDetail(batch) {
  const currentMoreActions = $("#task-detail .more-actions");
  const keepMoreActionsOpen = Boolean(
    currentMoreActions?.open && currentMoreActions.dataset.batchId === batch.id
  );
  const papers = batch.papers || [];
  const counts = {complete: 0, needs: 0, pdf: 0, zotero: 0};
  papers.forEach(paper => {
    if (paper.status === "complete") counts.complete += 1;
    if (paper.needs_action) counts.needs += 1;
    if (["verified", "accepted"].includes(paper.pdf_status)) counts.pdf += 1;
    if (paper.endnote_status === "verified") counts.zotero += 1;
  });
  const pct = papers.length ? Math.round(counts.complete / papers.length * 100) : 0;
  const paused = batch.status !== "running";
  const exportPath = batch.endnote_export_path;
  const batchError = batch.error === "Zotero 本地 API 未启用" && state.system?.zotero?.ready
    ? "上次提交时 Zotero 本地 API 未启用；当前已连接，可重新提交。"
    : batch.error;
  $("#task-detail").innerHTML = `
    <div class="task-head">
      <div><h2>${esc(batch.name)}</h2><p class="task-subtitle">${statusChip(batch.status)}<span>Zotero · ${esc(batch.target_library || "未命名")}</span></p></div>
      <div class="task-actions">
        <button type="button" data-action="${paused ? "resume" : "pause"}">${paused ? "继续任务" : "暂停"}</button>
        <button class="primary" type="button" data-action="commit">提交 Zotero</button>
        <button type="button" data-action="institution-login">机构登录</button>
        <button type="button" data-action="export-endnote">导出 EndNote</button>
        <details class="more-actions" data-batch-id="${esc(batch.id)}" ${keepMoreActionsOpen ? "open" : ""}><summary>更多操作 ···</summary><div class="more-menu">
          <button type="button" data-action="rename-pdfs">重命名 PDF</button>
          <button type="button" data-action="export-pdfs">导出 PDF</button>
          ${exportPath ? `<a class="button-link" href="/api/batches/${encodeURIComponent(batch.id)}/endnote-export.zip">下载 EndNote ZIP</a><button type="button" data-action="open-endnote-export">打开导出文件夹</button>` : ""}
          <a class="button-link" href="/api/batches/${encodeURIComponent(batch.id)}/report.csv">下载 CSV 报告</a>
          <button class="danger" type="button" data-action="delete">删除批次</button>
        </div></details>
      </div>
    </div>
    <div class="progress-meta"><span>已完成 ${counts.complete} / ${papers.length}</span><strong>${pct}%</strong></div>
    <div class="progress" role="progressbar" aria-label="批次完成进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><span style="width:${pct}%"></span></div>
    <div class="summary-grid">
      <div class="summary-card"><b>${papers.length}</b><span>论文总数</span></div>
      <div class="summary-card"><b>${counts.pdf}</b><span>PDF 已验证</span></div>
      <div class="summary-card"><b>${counts.zotero}</b><span>Zotero 已核验</span></div>
      <div class="summary-card"><b>${counts.needs}</b><span>等待处理</span></div>
    </div>
    ${batchError ? `<div class="batch-alert" role="alert">${esc(batchError)}</div>` : ""}
    <div class="table-title"><h3>论文</h3>${counts.needs ? `<span class="status-pill warn">${counts.needs} 篇待处理</span>` : `<span class="status-pill">无需人工处理</span>`}</div>
    ${papers.length ? `<div class="table-wrap"><table><thead><tr><th>#</th><th>论文</th><th>题录</th><th>PDF</th><th>Zotero</th><th>状态与操作</th></tr></thead><tbody>${papers.map(paperRow).join("")}</tbody></table></div>` : `<div class="empty-state compact">批次中没有论文</div>`}`;

  $("[data-action='pause']")?.addEventListener("click", () => batchAction("pause"));
  $("[data-action='resume']")?.addEventListener("click", () => batchAction("resume"));
  $("[data-action='commit']")?.addEventListener("click", () => batchAction("commit"));
  $("#task-detail .more-actions")?.addEventListener("toggle", event => {
    if (event.currentTarget.open || state.activeTab !== "tasks" || state.selectedBatch !== batch.id) return;
    renderBatch(batch.id, {silent: true});
  });
  $("[data-action='open-endnote-export']")?.addEventListener("click", () => batchAction("open-endnote-export"));
  $("[data-action='delete']")?.addEventListener("click", () => openDeleteDialog([batch.id]));
  $("[data-action='institution-login']")?.addEventListener("click", event => (
    openInstitutionLogin(event, {batch_id: batch.id})
  ));
  $("[data-action='rename-pdfs']")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    setButtonBusy(button, true, "重命名中…");
    try {
      const result = await api("/api/tools/rename-pdfs", {method: "POST", body: JSON.stringify({source: "batch", batch_id: batch.id})});
      toast(`已重命名 ${result.renamed} 个 PDF，跳过 ${result.skipped} 个`);
      await renderBatch(batch.id);
    } catch (error) {
      toast(error.message);
      setButtonBusy(button, false);
    }
  });
  $("[data-action='export-pdfs']")?.addEventListener("click", async () => {
    await showTab("tools");
    $("#export-source").value = "batch";
    syncToolSource("export");
    $("#export-batch").value = batch.id;
    fillExportDestination();
    await loadExportItems();
    $("#export-destination").focus();
  });
  $("[data-action='export-endnote']")?.addEventListener("click", async () => {
    await showTab("tools");
    if (batch.target_library) $("#endnote-collection").value = batch.target_library;
    fillEndnoteDestination();
    $("#endnote-destination").focus();
  });
  bindPaperActions(papers);
}

function paperRow(paper) {
  const title = paper.title || paper.input_title || paper.input_text || "未命名论文";
  const metadata = translated(paper.metadata_status, METADATA_LABELS);
  const pdf = translated(paper.pdf_status, PDF_LABELS);
  const zotero = translated(paper.endnote_status, ZOTERO_LABELS);
  const version = paper.version ? translated(paper.version, VERSION_LABELS, "版本不明") : "";
  return `<tr>
    <td data-label="序号">${paper.position}</td>
    <td class="title" data-label="论文"><strong>${esc(title)}</strong><span class="paper-meta">${esc(paper.doi || "无 DOI")}${paper.year ? ` · ${esc(paper.year)}` : ""}</span>${paper.error ? `<div class="error-text">${esc(paper.error)}</div>` : ""}</td>
    <td data-label="题录">${statusChip(paper.metadata_status, metadata)}</td>
    <td data-label="PDF">${statusChip(paper.pdf_status, pdf)}${version ? `<span class="paper-meta">${esc(version)}</span>` : ""}</td>
    <td data-label="Zotero">${statusChip(paper.endnote_status, zotero)}</td>
    <td class="actions" data-label="状态与操作">${statusChip(paper.status)}${paperActions(paper)}</td>
  </tr>`;
}

function paperActions(paper) {
  const primary = [];
  const secondary = [];
  if (paper.needs_action === "confirm_metadata") primary.push(`<button type="button" data-paper-action="metadata" data-paper="${esc(paper.id)}">确认题录</button>`);
  if (paper.needs_action === "manual_institution") primary.push(`<button type="button" data-paper-action="continue-institution" data-paper="${esc(paper.id)}">继续机构访问</button>`);
  const needsPdf = ["manual_pdf", "confirm_pdf", "manual_institution"].includes(paper.needs_action) || paper.status === "institution_pending";
  if (needsPdf) {
    primary.push(`<button type="button" data-paper-action="open" data-paper="${esc(paper.id)}">获取 PDF</button>`);
    secondary.push(`<button type="button" data-paper-action="institution" data-paper="${esc(paper.id)}">机构自动获取</button>`);
    if (state.system?.settings?.institution?.openurl) secondary.push(`<button type="button" data-paper-action="resolver" data-paper="${esc(paper.id)}">机构馆藏</button>`);
    secondary.push(`<button type="button" data-paper-action="scholar" data-paper="${esc(paper.id)}">Google Scholar</button>`);
    secondary.push(`<label class="upload-label file-button">上传 PDF<input type="file" accept="application/pdf,.pdf" data-paper-upload="${esc(paper.id)}"></label>`);
  }
  if (paper.needs_action === "confirm_pdf") primary.push(`<button type="button" data-paper-action="pdf" data-paper="${esc(paper.id)}">确认 PDF</button>`);
  if (["retry", "reconcile_endnote"].includes(paper.needs_action)) primary.push(`<button type="button" data-paper-action="retry" data-paper="${esc(paper.id)}">重试 / 对账</button>`);
  if (!['complete', 'skipped'].includes(paper.status)) secondary.push(`<button class="danger" type="button" data-paper-action="skip" data-paper="${esc(paper.id)}">跳过</button>`);
  if (!primary.length && !secondary.length) return "";
  return `<div class="paper-actions">${primary.join("")}${secondary.length ? `<details class="paper-more-actions"><summary>更多操作</summary><div class="paper-more-menu"><div class="paper-more-menu-head"><strong>更多操作</strong><button class="paper-more-close" type="button" aria-label="关闭更多操作菜单">关闭</button></div><div class="paper-more-menu-items">${secondary.join("")}</div></div></details>` : ""}</div>`;
}

function bindPaperActions(papers) {
  $$(".paper-more-actions").forEach(menu => menu.addEventListener("toggle", () => {
    if (!menu.open) return;
    $$(".paper-more-actions[open]").forEach(other => {
      if (other !== menu) other.open = false;
    });
  }));
  $$(".paper-more-close").forEach(button => button.addEventListener("click", event => {
    event.preventDefault();
    const menu = button.closest(".paper-more-actions");
    if (!menu) return;
    menu.open = false;
    menu.querySelector("summary")?.focus();
  }));
  $$('[data-paper-action]').forEach(button => button.addEventListener("click", async () => {
    const paper = papers.find(item => item.id === button.dataset.paper);
    const action = button.dataset.paperAction;
    if (!paper) return;
    try {
      if (action === "metadata") return showCandidates(paper);
      if (action === "pdf") return showPdfReview(paper);
      setButtonBusy(button, true, ["institution", "continue-institution"].includes(action) ? "获取中…" : "处理中…");
      if (["open", "scholar", "resolver"].includes(action)) {
        await api(`/api/papers/${encodeURIComponent(paper.id)}/open?scholar=${action === "scholar"}&resolver=${action === "resolver"}`, {method: "POST", body: "{}"});
        toast("已打开获取页面");
      }
      if (action === "institution") {
        const result = await api(`/api/papers/${encodeURIComponent(paper.id)}/acquire-institution`, {method: "POST", body: "{}"});
        toast(result?.status === "verified" ? "机构 PDF 已下载并验证" : "已打开机构页面，请完成登录后继续检查");
      }
      if (action === "continue-institution") {
        const result = await api(`/api/papers/${encodeURIComponent(paper.id)}/continue-institution`, {method: "POST", body: "{}"});
        toast(result?.status === "verified" ? "机构 PDF 已下载并验证" : "仍需在机构页面完成登录或导航");
      }
      if (["retry", "skip"].includes(action)) await api(`/api/papers/${encodeURIComponent(paper.id)}/${action}`, {method: "POST", body: "{}"});
      await renderBatch();
    } catch (error) {
      toast(error.message);
      setButtonBusy(button, false);
    }
  }));
  $$('[data-paper-upload]').forEach(input => input.addEventListener("change", async () => {
    if (!input.files?.[0]) return;
    const data = new FormData();
    data.append("file", input.files[0]);
    input.disabled = true;
    try {
      await api(`/api/papers/${encodeURIComponent(input.dataset.paperUpload)}/upload-pdf`, {method: "POST", body: data});
      toast("PDF 已接收");
      await renderBatch();
    } catch (error) {
      input.disabled = false;
      toast(error.message);
    }
  }));
}

async function showCandidates(paper) {
  state.currentPaper = paper;
  const dialog = $("#candidate-dialog");
  $("#candidate-source").textContent = `输入：${paper.input_text}`;
  $("#manual-doi").value = paper.input_doi || paper.doi || "";
  $("#candidate-list").innerHTML = `<div class="loading-state"><span class="spinner" aria-hidden="true"></span> 正在查找候选…</div>`;
  dialog.showModal();
  try {
    const candidates = await api(`/api/papers/${encodeURIComponent(paper.id)}/candidates`);
    if (state.currentPaper?.id !== paper.id || !dialog.open) return;
    $("#candidate-list").innerHTML = candidates.length ? candidates.map(candidate => `<div class="candidate"><h3>${esc(candidate.metadata.title)}</h3><p>${esc((candidate.metadata.authors || []).join("；") || "作者未知")}</p><p>${esc(candidate.metadata.journal || "期刊未知")} · ${esc(candidate.metadata.year || "年份未知")} · ${esc(candidate.metadata.doi || "无 DOI")}</p><button class="primary" type="button" data-candidate="${esc(candidate.id)}">采用此题录</button></div>`).join("") : `<div class="empty-state compact"><strong>没有候选题录</strong><span>可补充 DOI 后重新解析</span></div>`;
    $$('[data-candidate]').forEach(button => button.addEventListener("click", async () => {
      setButtonBusy(button, true, "保存中…");
      try {
        await api(`/api/papers/${encodeURIComponent(paper.id)}/confirm-metadata`, {method: "POST", body: JSON.stringify({candidate_id: button.dataset.candidate})});
        dialog.close();
        await renderBatch();
      } catch (error) {
        setButtonBusy(button, false);
        toast(error.message);
      }
    }));
  } catch (error) {
    $("#candidate-list").innerHTML = `<div class="empty-state compact"><strong>候选加载失败</strong><span>${esc(error.message)}</span></div>`;
  }
}

function showPdfReview(paper) {
  state.currentPaper = paper;
  $("#pdf-review-text").textContent = paper.error || "请确认文件是论文主文，并选择版本。";
  $("#pdf-version").value = VERSION_LABELS[paper.version] ? paper.version : "published";
  $("#pdf-dialog").showModal();
}

async function batchAction(action) {
  const batchId = state.selectedBatch;
  const button = $(`[data-action='${action}']`);
  if (!batchId || state.deletingBatchIds.has(batchId)) return;
  const messages = {commit: "已启动 Zotero 提交", pause: "批次已暂停", resume: "批次已继续", "open-endnote-export": "已打开导出文件夹"};
  setButtonBusy(button, true, "处理中…");
  try {
    await api(`/api/batches/${encodeURIComponent(batchId)}/${action}`, {method: "POST", body: "{}"});
    toast(messages[action] || "状态已更新");
    if (state.selectedBatch === batchId) await renderBatch(batchId);
    await refreshBatches({renderDetail: false, silent: true});
  } catch (error) {
    setButtonBusy(button, false);
    toast(error.message);
  }
}

async function openInstitutionLogin(event, context = {}) {
  const button = event?.currentTarget;
  setButtonBusy(button, true, "打开中…");
  try {
    await api("/api/institution/login", {method: "POST", body: JSON.stringify(context)});
    toast("已打开机构登录页");
  } catch (error) {
    toast(error.message);
  } finally {
    setButtonBusy(button, false);
  }
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(pollTasks, 2600);
}

function stopPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
}

async function pollTasks() {
  if (state.pollBusy || document.hidden || state.activeTab !== "tasks") return;
  state.pollBusy = true;
  state.pollCount += 1;
  try {
    const batchId = state.selectedBatch;
    if (batchId && !$("#task-detail .more-actions[open], #task-detail .paper-more-actions[open]")) {
      await renderBatch(batchId, {silent: true});
    }
    if (state.pollCount % 2 === 0) await refreshBatches({renderDetail: false, silent: true});
  } finally {
    state.pollBusy = false;
  }
}

async function openDeleteDialog(batchIds) {
  const ids = [...new Set(batchIds.filter(Boolean))];
  if (!ids.length || state.deletionActive) return;
  state.pendingDeleteIds = ids;
  state.deletionTerminal = false;
  const token = ++state.deletePreviewToken;
  const dialog = $("#delete-dialog");
  $("#delete-summary").innerHTML = `<div class="loading-state"><span class="spinner" aria-hidden="true"></span> 正在计算可清理内容…</div>`;
  $("#delete-items").innerHTML = "";
  $("#delete-progress").hidden = true;
  $("#delete-progress").innerHTML = "";
  $("#confirm-delete").hidden = false;
  $("#confirm-delete").disabled = true;
  $("#confirm-delete").textContent = "确认删除";
  $("#cancel-delete").textContent = "取消";
  if (!dialog.open) dialog.showModal();
  try {
    const preview = await api("/api/batches/delete-preview", {method: "POST", body: JSON.stringify({batch_ids: ids})});
    if (token !== state.deletePreviewToken || !dialog.open) return;
    renderDeletePreview(preview);
  } catch (error) {
    if (token !== state.deletePreviewToken) return;
    $("#delete-summary").innerHTML = `<div class="batch-alert" role="alert">${esc(error.message)}</div>`;
    $("#delete-items").innerHTML = "";
    $("#confirm-delete").disabled = true;
  }
}

function renderDeletePreview(preview) {
  const batches = preview.batches || [];
  const totals = preview.totals || {
    batch_count: batches.length,
    paper_count: batches.reduce((sum, item) => sum + Number(item.paper_count || 0), 0),
    bytes: batches.reduce((sum, item) => sum + Number(item.bytes || 0), 0),
  };
  const runningCount = batches.filter(item => item.running).length;
  $("#delete-summary").innerHTML = `
    <div class="delete-metric"><b>${Number(totals.batch_count || batches.length)}</b><span>批次</span></div>
    <div class="delete-metric"><b>${Number(totals.paper_count || 0)}</b><span>论文</span></div>
    <div class="delete-metric"><b>${esc(formatBytes(totals.bytes))}</b><span>可清理文件</span></div>`;
  $("#delete-items").innerHTML = batches.map(item => `<div class="delete-item"><span>${esc(item.name || item.id)}</span><span class="delete-item-meta">${item.missing ? "已不存在" : `${Number(item.paper_count || 0)} 篇 · ${formatBytes(item.bytes)}`}${item.running ? " · 运行中" : ""}</span></div>`).join("");
  const confirm = $("#confirm-delete");
  confirm.disabled = batches.length === 0;
  confirm.textContent = runningCount ? `停止并删除 ${batches.length} 个批次` : `删除 ${batches.length} 个批次`;
}

async function beginDeletion() {
  if (state.deletionTerminal) {
    $("#delete-dialog").close();
    return;
  }
  if (state.deletionActive || !state.pendingDeleteIds.length) return;
  const ids = [...state.pendingDeleteIds];
  state.deletionActive = true;
  state.deletePollFailures = 0;
  $("#confirm-delete").disabled = true;
  $("#confirm-delete").textContent = "正在启动…";
  $("#cancel-delete").disabled = true;
  $("#delete-dialog .dialog-close").disabled = true;
  $("#delete-progress").hidden = false;
  $("#delete-progress").innerHTML = `<div class="loading-state"><span class="spinner" aria-hidden="true"></span> 正在停止任务并准备清理…</div>`;
  try {
    const operation = await api("/api/batches/delete", {method: "POST", body: JSON.stringify({batch_ids: ids})});
    state.deleteOperationId = operation.operation_id;
    const operationBatchIds = (operation.batches || []).map(item => item.batch_id || item.id).filter(Boolean);
    (operationBatchIds.length ? operationBatchIds : ids).forEach(id => state.deletingBatchIds.add(id));
    ids.forEach(id => state.selectedBatchIds.delete(id));
    if (state.selectedBatch && ids.includes(state.selectedBatch)) {
      state.selectedBatch = null;
      invalidateDetailRequest();
      emptyTaskDetail("正在删除批次", "清理进度显示在确认窗口中");
    }
    renderBatchList();
    renderDeletionProgress({id: operation.operation_id, status: operation.status || "pending", batches: operation.batches || []});
    if (!operation.operation_id) throw new Error("删除操作未返回操作编号");
    scheduleDeletionPoll(300);
  } catch (error) {
    state.deletionActive = false;
    $("#delete-progress").innerHTML = `<div class="batch-alert" role="alert">${esc(error.message)}</div>`;
    $("#confirm-delete").disabled = false;
    $("#confirm-delete").textContent = "重试删除";
    $("#cancel-delete").disabled = false;
    $("#delete-dialog .dialog-close").disabled = false;
  }
}

function scheduleDeletionPoll(delay = 1200) {
  clearTimeout(state.deletePollTimer);
  state.deletePollTimer = setTimeout(pollDeletion, delay);
}

async function pollDeletion() {
  if (!state.deleteOperationId) return;
  try {
    const operation = await api(`/api/batch-deletions/${encodeURIComponent(state.deleteOperationId)}`);
    state.deletePollFailures = 0;
    renderDeletionProgress(operation);
    if (["completed", "partial_failed", "failed"].includes(operation.status)) {
      await finishDeletion(operation);
    } else {
      scheduleDeletionPoll(1200);
    }
  } catch (error) {
    state.deletePollFailures += 1;
    $("#delete-progress").innerHTML = `<div class="batch-alert" role="alert">暂时无法读取清理进度：${esc(error.message)}。后台操作仍会继续。</div>`;
    scheduleDeletionPoll(Math.min(5000, 1000 * state.deletePollFailures));
  }
}

function renderDeletionProgress(operation) {
  const batches = operation.batches || [];
  const active = !["completed", "partial_failed", "failed"].includes(operation.status);
  $("#delete-progress").hidden = false;
  $("#delete-progress").innerHTML = `<div class="deletion-status-list">${batches.map(item => {
    const id = item.batch_id || item.id;
    const amount = item.status === "completed" || item.status === "missing"
      ? formatBytes(item.deleted_bytes)
      : formatBytes(item.estimated_bytes ?? item.bytes);
    return `<div class="deletion-row"><strong>${esc(item.name || id || "批次")}</strong>${statusChip(item.status, translated(item.status, DELETION_LABELS, "未知状态"))}<span class="paper-meta">${Number(item.paper_count || 0)} 篇 · ${esc(amount)}</span>${item.error ? `<small>${esc(item.error)}</small>` : ""}</div>`;
  }).join("") || `<div class="loading-state"><span class="spinner" aria-hidden="true"></span> ${active ? "等待清理状态…" : "清理已结束"}</div>`}</div>`;
}

async function finishDeletion(operation) {
  state.deletionActive = false;
  state.deletionTerminal = true;
  clearTimeout(state.deletePollTimer);
  const failed = (operation.batches || []).filter(item => !["completed", "missing"].includes(item.status));
  const removed = (operation.batches || []).filter(item => ["completed", "missing"].includes(item.status));
  (operation.batches || []).forEach(item => state.deletingBatchIds.delete(item.batch_id || item.id));
  removed.forEach(item => state.selectedBatchIds.delete(item.batch_id || item.id));
  $("#cancel-delete").disabled = false;
  $("#cancel-delete").textContent = "关闭";
  $("#delete-dialog .dialog-close").disabled = false;
  const confirm = $("#confirm-delete");
  if (failed.length) {
    state.pendingDeleteIds = failed.map(item => item.batch_id || item.id).filter(Boolean);
    state.deletionTerminal = false;
    confirm.hidden = false;
    confirm.disabled = false;
    confirm.textContent = `重试失败项（${failed.length}）`;
    toast(`已清理 ${removed.length} 个批次，${failed.length} 个需要重试`);
  } else {
    confirm.hidden = true;
    toast(`已清理 ${removed.length} 个批次`);
  }
  state.deleteOperationId = null;
  await refreshBatches({renderDetail: false, silent: true});
}

function closeDeleteDialog() {
  if (state.deletionActive) return;
  clearTimeout(state.deletePollTimer);
  state.deleteOperationId = null;
  state.pendingDeleteIds = [];
  state.deletionTerminal = false;
  $("#cancel-delete").disabled = false;
  $("#delete-dialog").close();
}

function optionList(items, valueKey, labelFn) {
  if (!items.length) return `<option value="">无可用项</option>`;
  return items.map(item => `<option value="${esc(item[valueKey])}">${esc(labelFn(item))}</option>`).join("");
}

function syncToolSource(prefix) {
  const source = $(`#${prefix}-source`).value;
  const zoteroLibraryWrap = $(`#${prefix}-zotero-library-wrap`);
  if (zoteroLibraryWrap) zoteroLibraryWrap.hidden = source !== "zotero";
  $(`#${prefix}-collection-wrap`).hidden = source !== "zotero";
  $(`#${prefix}-batch-wrap`).hidden = source !== "batch";
  const endnoteWrap = $(`#${prefix}-endnote-wrap`);
  if (endnoteWrap) endnoteWrap.hidden = source !== "endnote";
  const submit = $(`#${prefix}-pdfs-form button[type='submit']`);
  if (prefix === "rename" && submit) updateRenameSubmitState();
  if (prefix === "export") fillExportDestination();
}

function selectedZoteroScope(prefix) {
  const collection = $(`#${prefix}-collection`);
  const selected = collection?.selectedOptions?.[0];
  const library = $(`#${prefix}-zotero-library`);
  const selectedLibrary = library?.selectedOptions?.[0];
  const wholeLibrary = selected?.dataset.wholeLibrary === "true";
  const placeholder = selected?.dataset.scopePlaceholder === "true";
  return {
    zotero_library_id: library?.value || "user:0",
    zotero_library_name: selectedLibrary?.dataset.name || selectedLibrary?.textContent || "",
    collection_key: wholeLibrary || placeholder ? "" : collection?.value || "",
    collection: wholeLibrary || placeholder ? "" : selected?.dataset.name || "",
    collection_path: wholeLibrary || placeholder ? "" : selected?.dataset.path || selected?.dataset.name || "",
    whole_library: Boolean(collection && !collection.disabled && wholeLibrary),
  };
}

function toolSourceReady(prefix) {
  const source = $(`#${prefix}-source`).value;
  if (source === "batch") return Boolean($(`#${prefix}-batch`).value);
  if (source === "endnote") return Boolean($(`#${prefix}-endnote-library`).value.trim());
  if (source !== "zotero") return false;
  const library = $(`#${prefix}-zotero-library`);
  const collection = $(`#${prefix}-collection`);
  const selected = collection?.selectedOptions?.[0];
  return Boolean(
    library && !library.disabled && library.value &&
    collection && !collection.disabled && selected &&
    selected.dataset.scopePlaceholder !== "true"
  );
}

function updateRenameSubmitState() {
  const submit = $("#rename-pdfs-form button[type='submit']");
  if (!submit || submit.hasAttribute("aria-busy")) return;
  submit.disabled = !toolSourceReady("rename");
}

function exportSourcePayload() {
  return {
    source: $("#export-source").value,
    batch_id: $("#export-batch").value,
    endnote_library: $("#export-endnote-library").value,
    ...selectedZoteroScope("export"),
  };
}

function selectedExportItemIds() {
  return $$('[data-export-item]:checked').map(input => input.value);
}

function updateExportSelection() {
  const available = $$('[data-export-item]:not(:disabled)');
  const selected = available.filter(input => input.checked);
  const selectAll = $("#export-select-all");
  selectAll.disabled = available.length === 0;
  selectAll.checked = available.length > 0 && selected.length === available.length;
  selectAll.indeterminate = selected.length > 0 && selected.length < available.length;
  $("#export-selection-count").textContent = `${selected.length} / ${available.length}`;
  const submit = $("#export-pdfs-form button[type='submit']");
  if (!submit.hasAttribute("aria-busy")) submit.disabled = selected.length === 0;
}

function renderExportItems(items) {
  state.exportItems = items;
  const list = $("#export-items");
  if (!items.length) {
    list.innerHTML = '<div class="picker-state">没有可选择的论文</div>';
    updateExportSelection();
    return;
  }
  list.innerHTML = items.map(item => {
    const meta = [item.author, item.year, item.doi].filter(Boolean).join(" · ");
    const availability = item.available
      ? `${item.file_count || 1} 个 PDF · ${formatBytes(item.size)}`
      : "没有可导出的 PDF";
    return `<label class="export-item ${item.available ? "" : "unavailable"}">
      <input type="checkbox" data-export-item value="${esc(item.id)}" ${item.available ? "checked" : "disabled"}>
      <span class="export-item-copy"><strong>${esc(item.title || "未命名论文")}</strong><small>${esc(meta || availability)}</small></span>
      <span class="export-item-size">${esc(availability)}</span>
    </label>`;
  }).join("");
  $$('[data-export-item]').forEach(input => input.addEventListener("change", updateExportSelection));
  updateExportSelection();
}

async function loadExportItems() {
  const token = ++state.exportRequestToken;
  state.exportController?.abort();
  const controller = new AbortController();
  state.exportController = controller;
  const picker = $("#export-picker");
  picker.setAttribute("aria-busy", "true");
  $("#export-items").innerHTML = '<div class="picker-state"><span class="spinner" aria-hidden="true"></span><br>正在读取论文…</div>';
  $("#export-select-all").disabled = true;
  $("#export-pdfs-form button[type='submit']").disabled = true;
  if (!toolSourceReady("export")) {
    state.exportItems = [];
    $("#export-items").innerHTML = '<div class="picker-state">请选择有效的数据区和范围</div>';
    picker.setAttribute("aria-busy", "false");
    state.exportController = null;
    return;
  }
  try {
    const result = await api("/api/tools/export-pdfs/candidates", {
      method: "POST",
      body: JSON.stringify(exportSourcePayload()),
      signal: controller.signal,
    });
    if (token !== state.exportRequestToken) return;
    renderExportItems(result.items || []);
  } catch (error) {
    if (token !== state.exportRequestToken || error.name === "AbortError") return;
    state.exportItems = [];
    $("#export-items").innerHTML = `<div class="picker-state">${esc(error.message)}</div>`;
    updateExportSelection();
  } finally {
    if (token === state.exportRequestToken) {
      picker.setAttribute("aria-busy", "false");
      state.exportController = null;
    }
  }
}

function fillExportDestination() {
  const sources = state.toolSources || {};
  const downloads = sources.downloads_dir || "";
  const source = $("#export-source").value;
  let name = "";
  if (source === "zotero") {
    const scope = selectedZoteroScope("export");
    const library = $("#export-zotero-library")?.selectedOptions?.[0];
    name = scope.collection_path || scope.collection || library?.dataset.name || library?.textContent || "";
  }
  if (source === "batch") {
    const batch = (sources.batches || []).find(item => item.id === $("#export-batch").value);
    name = batch?.target_library || batch?.name || "";
  }
  if (source === "endnote" && $("#export-endnote-library").value) {
    const parts = String($("#export-endnote-library").value).split(/[/\\]/);
    name = (parts.at(-1) || "").replace(/\.enl$/i, "");
  }
  const safeName = String(name || "")
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, "_")
    .replace(/[ .]+$/g, "")
    .trim();
  if (downloads && safeName) $("#export-destination").placeholder = `${downloads}\\${safeName}`;
  if (downloads && safeName && !$("#export-destination").dataset.custom) $("#export-destination").value = `${downloads}\\${safeName}`;
}

function zoteroLibraryOptions(libraries) {
  if (!libraries.length) return '<option value="user:0" data-name="My Library">My Library</option>';
  return libraries.map(item => `<option value="${esc(item.id)}" data-name="${esc(item.name)}">${esc(item.name)}${item.type === "group" ? " · Group" : ""}</option>`).join("");
}

function zoteroCollectionOptions(collections) {
  const options = [
    '<option value="" data-scope-placeholder="true">请选择范围</option>',
    '<option value="__whole_library__" data-whole-library="true" data-name="">整个库</option>',
  ];
  options.push(...collections.map(item => {
    const label = item.path || item.name || item.key;
    const count = item.numItems == null ? "" : ` · ${item.numItems}`;
    return `<option value="${esc(item.key)}" data-name="${esc(item.name || label)}" data-path="${esc(label)}">${esc(label)}${esc(count)}</option>`;
  }));
  return options.join("");
}

async function loadZoteroCollections(prefix, supplied = null) {
  const token = ++state.zoteroCollectionTokens[prefix];
  const libraryId = $(`#${prefix}-zotero-library`).value || "user:0";
  const select = $(`#${prefix}-collection`);
  const submit = $(`#${prefix}-pdfs-form button[type='submit']`);
  select.disabled = true;
  select.innerHTML = '<option value="">正在读取…</option>';
  if (submit && !submit.hasAttribute("aria-busy")) submit.disabled = true;
  if (prefix === "export") {
    state.exportRequestToken += 1;
    state.exportController?.abort();
    state.exportController = null;
    state.exportItems = [];
    $("#export-picker").setAttribute("aria-busy", "true");
    $("#export-items").innerHTML = '<div class="picker-state"><span class="spinner" aria-hidden="true"></span><br>正在切换 Zotero 库…</div>';
    $("#export-select-all").disabled = true;
    $("#export-selection-count").textContent = "0 / 0";
  }
  try {
    const collections = supplied ?? (await api(`/api/tools/zotero-collections?library_id=${encodeURIComponent(libraryId)}`)).collections ?? [];
    if (token !== state.zoteroCollectionTokens[prefix]) return false;
    select.innerHTML = zoteroCollectionOptions(collections);
    if (collections.length) select.value = collections[0].key;
    select.disabled = false;
    if (prefix === "rename") updateRenameSubmitState();
    return true;
  } catch (error) {
    if (token !== state.zoteroCollectionTokens[prefix]) return false;
    select.innerHTML = '<option value="">无法读取库</option>';
    if (prefix === "export") $("#export-picker").setAttribute("aria-busy", "false");
    if (prefix === "rename") updateRenameSubmitState();
    toast(error.message);
    return false;
  }
}

async function pickEndnoteLibrary(prefix) {
  const input = $(`#${prefix}-endnote-library`);
  const button = $(`#pick-${prefix}-endnote-library`);
  setButtonBusy(button, true, "选择中…");
  try {
    const result = await api("/api/tools/pick-endnote-library", {
      method: "POST",
      body: JSON.stringify({initial_path: input.value}),
    });
    if (!result.path) return;
    input.value = result.path;
    const list = $("#endnote-library-paths");
    if (![...list.options].some(option => option.value === result.path)) {
      list.insertAdjacentHTML("beforeend", `<option value="${esc(result.path)}"></option>`);
    }
    if (prefix === "export") {
      fillExportDestination();
      await loadExportItems();
    } else {
      updateRenameSubmitState();
    }
  } catch (error) {
    toast(error.message);
  } finally {
    setButtonBusy(button, false);
  }
}

function fillEndnoteDestination() {
  const sources = state.toolSources || {};
  const downloads = sources.downloads_dir || "";
  const name = $("#endnote-collection")?.value || "";
  const input = $("#endnote-destination");
  if (!input) return;
  if (downloads && name) input.placeholder = `${downloads}\\${name}`;
  if (downloads && name && !input.dataset.custom) input.value = `${downloads}\\${name}`;
}

async function loadToolSources() {
  const token = ++state.toolSourcesRequestToken;
  state.toolSourcesController?.abort();
  const controller = new AbortController();
  state.toolSourcesController = controller;
  for (const prefix of ["rename", "export"]) {
    const library = $(`#${prefix}-zotero-library`);
    const collection = $(`#${prefix}-collection`);
    if (library) library.disabled = true;
    if (collection) {
      collection.disabled = true;
      collection.innerHTML = '<option value="">正在读取…</option>';
    }
    const submit = $(`#${prefix}-pdfs-form button[type='submit']`);
    if (submit && !submit.hasAttribute("aria-busy")) submit.disabled = true;
  }
  state.exportRequestToken += 1;
  state.exportController?.abort();
  state.exportController = null;
  state.exportItems = [];
  $("#export-select-all").disabled = true;
  $("#export-selection-count").textContent = "0 / 0";
  $("#export-items").innerHTML = '<div class="picker-state"><span class="spinner" aria-hidden="true"></span><br>正在读取数据区…</div>';
  try {
    const sources = await api("/api/tools/sources", {signal: controller.signal});
    if (token !== state.toolSourcesRequestToken) return;
    state.toolSources = sources;
    const batches = sources.batches || [];
    const collections = sources.collections || [];
    const zoteroLibraries = sources.zotero_libraries || [{id: "user:0", name: "My Library", type: "user"}];
    state.zoteroCollectionTokens.rename += 1;
    state.zoteroCollectionTokens.export += 1;
    const batchHtml = optionList(batches, "id", item => `${item.name} · ${item.total || 0} 篇`);
    const namedCollectionHtml = optionList(collections, "name", item => item.numItems == null ? item.name : `${item.name} · ${item.numItems}`);
    $("#rename-batch").innerHTML = batchHtml;
    $("#export-batch").innerHTML = batchHtml;
    $("#rename-zotero-library").innerHTML = zoteroLibraryOptions(zoteroLibraries);
    $("#export-zotero-library").innerHTML = zoteroLibraryOptions(zoteroLibraries);
    for (const prefix of ["rename", "export"]) {
      const library = $(`#${prefix}-zotero-library`);
      const collection = $(`#${prefix}-collection`);
      library.disabled = Boolean(sources.zotero_error);
      if (sources.zotero_error) {
        collection.innerHTML = '<option value="">无法读取 Zotero 范围</option>';
        collection.disabled = true;
      } else {
        collection.innerHTML = zoteroCollectionOptions(collections);
        if (collections.length) collection.value = collections[0].key;
        collection.disabled = false;
      }
    }
    $("#endnote-collection").innerHTML = namedCollectionHtml;
    $("#rename-endnote-library").value = sources.endnote_library || "";
    $("#export-endnote-library").value = sources.endnote_library || "";
    $("#endnote-library-paths").innerHTML = sources.endnote_library
      ? `<option value="${esc(sources.endnote_library)}"></option>`
      : "";
    $("#sync-endnote-library").value = sources.endnote_library || "";
    $("#sync-zotero-collections").innerHTML = collections
      .map(item => `<option value="${esc(item.name)}"></option>`)
      .join("");
    const syncCollection = $("#sync-zotero-collection");
    if (!syncCollection.dataset.custom) {
      const libraryName = String(sources.endnote_library || "").split(/[/\\]/).at(-1)?.replace(/\.enl$/i, "") || "";
      syncCollection.value = libraryName;
    }
    const syncButton = $("#sync-endnote-zotero-form button[type='submit']");
    syncButton.disabled = !sources.endnote_library || Boolean(sources.zotero_error);
    $("#sync-endnote-result").textContent = !sources.endnote_library
      ? "请先设置 EndNote 库"
      : sources.zotero_error ? `Zotero：${sources.zotero_error}` : "";
    syncToolSource("rename");
    syncToolSource("export");
    fillExportDestination();
    fillEndnoteDestination();
    await loadExportItems();
    if (sources.zotero_error) toast(`Zotero：${sources.zotero_error}`);
    else if (sources.zotero_library_error) toast(`Zotero 群组库：${sources.zotero_library_error}`);
  } catch (error) {
    if (token !== state.toolSourcesRequestToken || error.name === "AbortError") return;
    toast(error.message);
  } finally {
    if (token === state.toolSourcesRequestToken) state.toolSourcesController = null;
  }
}

async function submitToolForm(form, resultNode, busyText, request, successText) {
  const button = form.querySelector("button[type='submit']");
  resultNode.textContent = busyText;
  setButtonBusy(button, true, busyText);
  try {
    const result = await request();
    resultNode.textContent = successText(result);
    toast(resultNode.textContent);
  } catch (error) {
    resultNode.textContent = error.message;
    toast(error.message);
  } finally {
    setButtonBusy(button, false);
    if (form.id === "rename-pdfs-form") updateRenameSubmitState();
    if (form.id === "export-pdfs-form") updateExportSelection();
  }
}

function positionHelp(trigger) {
  const popover = $("#help-popover");
  if (!trigger || popover.hidden) return;
  const rect = trigger.getBoundingClientRect();
  const popoverRect = popover.getBoundingClientRect();
  let left = rect.left + rect.width / 2 - popoverRect.width / 2;
  left = Math.max(12, Math.min(left, window.innerWidth - popoverRect.width - 12));
  let top = rect.bottom + 8;
  if (top + popoverRect.height > window.innerHeight - 12) top = rect.top - popoverRect.height - 8;
  popover.style.left = `${left}px`;
  popover.style.top = `${Math.max(12, top)}px`;
}

function showHelp(trigger, pinned = false) {
  if (!trigger) return;
  if (state.helpTrigger && state.helpTrigger !== trigger) state.helpTrigger.setAttribute("aria-expanded", "false");
  state.helpTrigger = trigger;
  state.helpPinned = pinned;
  const popover = $("#help-popover");
  popover.textContent = trigger.dataset.help;
  popover.hidden = false;
  trigger.setAttribute("aria-expanded", "true");
  requestAnimationFrame(() => positionHelp(trigger));
}

function closeHelp() {
  state.helpTrigger?.setAttribute("aria-expanded", "false");
  state.helpTrigger = null;
  state.helpPinned = false;
  $("#help-popover").hidden = true;
}

function initHelpPopovers() {
  $$('.help-button').forEach(button => {
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-describedby", "help-popover");
    button.addEventListener("mouseenter", () => showHelp(button, false));
    button.addEventListener("mouseleave", () => { if (!state.helpPinned) closeHelp(); });
    button.addEventListener("focus", () => showHelp(button, false));
    button.addEventListener("blur", () => { if (!state.helpPinned) closeHelp(); });
    button.addEventListener("click", event => {
      event.stopPropagation();
      if (state.helpTrigger === button && state.helpPinned) closeHelp();
      else showHelp(button, true);
    });
  });
  document.addEventListener("click", event => {
    if (state.helpPinned && !event.target.closest(".help-button") && !event.target.closest("#help-popover")) closeHelp();
  });
  document.addEventListener("keydown", event => { if (event.key === "Escape" && state.helpTrigger) closeHelp(); });
  window.addEventListener("resize", () => positionHelp(state.helpTrigger));
  window.addEventListener("scroll", () => positionHelp(state.helpTrigger), true);
}

$$('.tab').forEach(button => button.addEventListener("click", () => {
  showTab(button.dataset.tab).catch(error => toast(error.message));
}));
$("#refresh-batches").addEventListener("click", () => refreshBatches());
$("#select-all-batches").addEventListener("change", event => {
  state.batches.forEach(batch => {
    if (state.deletingBatchIds.has(batch.id) || ["pending", "stopping", "deleting"].includes(batch.deletion_status)) return;
    if (event.target.checked) state.selectedBatchIds.add(batch.id);
    else state.selectedBatchIds.delete(batch.id);
  });
  renderBatchList();
});
$("#delete-selected-batches").addEventListener("click", () => openDeleteDialog([...state.selectedBatchIds]));

$("#list-file").addEventListener("change", async event => {
  const file = event.target.files?.[0];
  if (!file) return;
  $("#items-text").value = await file.text();
  const count = $("#items-text").value.split(/\r?\n/).filter(line => line.trim()).length;
  $("#parse-summary").textContent = `已读取 ${file.name} · ${count} 行`;
});

$("#batch-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  setButtonBusy(button, true, "创建中…");
  try {
    const result = await api("/api/batches", {method: "POST", body: JSON.stringify({
      name: $("#batch-name").value,
      items_text: $("#items-text").value,
      target_library: $("#target-library").value,
      library_mode: $("#library-mode").value,
      reference_manager: "zotero",
      start_immediately: true,
    })});
    state.selectedBatch = result.id;
    toast("批次已创建");
    await showTab("tasks");
  } catch (error) {
    toast(error.message);
  } finally {
    setButtonBusy(button, false);
  }
});

$("#settings-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  const sources = [];
  if ($("#source-open-access").checked) sources.push("open_access");
  if ($("#source-institution").checked) sources.push("institution");
  const split = value => value.split(",").map(item => item.trim()).filter(Boolean);
  const accessType = $("#institution-access-type").value;
  setButtonBusy(button, true, "保存中…");
  try {
    await api("/api/settings", {method: "POST", body: JSON.stringify({
      crossref_email: $("#crossref-email").value,
      unpaywall_email: $("#unpaywall-email").value,
      endnote_library: $("#endnote-library").value,
      acquisition_sources: sources,
      ocr_enabled: $("#ocr-enabled").checked,
      ocr_languages: $("#ocr-languages").value,
      ocr_max_pages: Number($("#ocr-max-pages").value || 2),
      auto_institution: $("#auto-institution").checked,
      auto_commit: $("#auto-commit").checked,
      login_wait_seconds: Number($("#login-wait-seconds").value || 600),
      institution: {
        preset: $("#institution-preset").value,
        access_type: accessType,
        id: $("#institution-id").value,
        name: $("#institution-name").value,
        login_url: $("#institution-login-url").value,
        ezproxy_login: accessType === "ezproxy" ? $("#institution-login").value : "",
        ezproxy_hosts: accessType === "ezproxy" ? split($("#institution-hosts").value) : [],
        openurl: $("#institution-openurl").value,
        login_url_markers: split($("#institution-login-markers").value),
        school_aliases: accessType === "carsi_saml" ? split($("#institution-school-aliases").value) : [],
        entity_id: accessType === "carsi_saml" ? $("#institution-entity-id").value : "",
        publisher_login_urls: accessType === "carsi_saml" ? parsePublisherLoginUrls($("#institution-publisher-login-urls").value) : {},
      },
    })});
    toast("设置已保存");
    await loadSystem();
  } catch (error) {
    toast(error.message);
  } finally {
    setButtonBusy(button, false);
  }
});

$("#institution-preset").addEventListener("change", () => {
  const selected = $("#institution-preset").value;
  const preset = (state.system?.presets || []).find(item => (item.preset || item.id) === selected);
  if (preset) {
    fillInstitution(preset, state.system.presets || []);
    $("#source-institution").checked = true;
    $("#auto-institution").checked = true;
  }
});

$("#institution-access-type").addEventListener("change", () => {
  syncInstitutionFields();
  $("#source-institution").checked = true;
  $("#auto-institution").checked = true;
});

$("#open-institution-login-settings").addEventListener("click", openInstitutionLogin);

$("#test-institution-access").addEventListener("click", async event => {
  const button = event.currentTarget;
  const resultNode = $("#institution-access-result");
  setButtonBusy(button, true, "检查中…");
  resultNode.textContent = "正在检查已保存的机构配置与登录状态…";
  try {
    const result = await api("/api/institution/test-access", {method: "POST", body: "{}"});
    resultNode.textContent = typeof result === "string"
      ? result
      : result?.message || result?.detail || result?.status || "访问检查已完成";
    toast("机构访问检查已完成");
  } catch (error) {
    resultNode.textContent = error.message;
    toast(error.message);
  } finally {
    setButtonBusy(button, false);
  }
});

$("#credentials-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  setButtonBusy(button, true, "保存中…");
  try {
    await api("/api/credentials", {method: "POST", body: JSON.stringify({username: $("#institution-username").value, password: $("#institution-password").value})});
    toast("凭据已保存");
    await loadSystem();
  } catch (error) {
    toast(error.message);
  } finally {
    setButtonBusy(button, false);
  }
});

$("#clear-credentials").addEventListener("click", async event => {
  setButtonBusy(event.currentTarget, true, "清除中…");
  try {
    await api("/api/credentials/clear", {method: "POST", body: "{}"});
    toast("已清除机构凭据");
    await loadSystem();
  } catch (error) {
    toast(error.message);
  } finally {
    setButtonBusy(event.currentTarget, false);
  }
});

$("#open-institution-login").addEventListener("click", openInstitutionLogin);
$("#rename-source").addEventListener("change", () => syncToolSource("rename"));
$("#export-source").addEventListener("change", async () => { syncToolSource("export"); await loadExportItems(); });
$("#rename-zotero-library").addEventListener("change", async () => { await loadZoteroCollections("rename"); });
$("#rename-collection").addEventListener("change", updateRenameSubmitState);
$("#rename-batch").addEventListener("change", updateRenameSubmitState);
$("#rename-endnote-library").addEventListener("input", updateRenameSubmitState);
$("#export-zotero-library").addEventListener("change", async () => {
  if (!await loadZoteroCollections("export")) return;
  fillExportDestination();
  await loadExportItems();
});
$("#export-collection").addEventListener("change", async () => { fillExportDestination(); await loadExportItems(); });
$("#export-batch").addEventListener("change", async () => { fillExportDestination(); await loadExportItems(); });
$("#export-endnote-library").addEventListener("change", async () => { fillExportDestination(); await loadExportItems(); });
$("#pick-rename-endnote-library").addEventListener("click", () => pickEndnoteLibrary("rename"));
$("#pick-export-endnote-library").addEventListener("click", () => pickEndnoteLibrary("export"));
$("#export-destination").addEventListener("input", () => { $("#export-destination").dataset.custom = "1"; });
$("#export-select-all").addEventListener("change", event => {
  $$('[data-export-item]:not(:disabled)').forEach(input => { input.checked = event.currentTarget.checked; });
  updateExportSelection();
});
$("#endnote-collection").addEventListener("change", fillEndnoteDestination);
$("#endnote-destination").addEventListener("input", () => { $("#endnote-destination").dataset.custom = "1"; });
$("#sync-zotero-collection").addEventListener("input", event => { event.currentTarget.dataset.custom = "1"; });

$("#rename-pdfs-form").addEventListener("submit", event => {
  event.preventDefault();
  submitToolForm(event.currentTarget, $("#rename-result"), "正在重命名…", () => api("/api/tools/rename-pdfs", {method: "POST", body: JSON.stringify({source: $("#rename-source").value, batch_id: $("#rename-batch").value, endnote_library: $("#rename-endnote-library").value, ...selectedZoteroScope("rename")})}), result => `已重命名 ${result.renamed} 个，跳过 ${result.skipped} 个${(result.warnings || []).length ? `，${result.warnings.length} 个已回查确认` : ""}`);
});

$("#export-pdfs-form").addEventListener("submit", event => {
  event.preventDefault();
  const itemIds = selectedExportItemIds();
  if (!itemIds.length) {
    toast("请至少选择一篇论文");
    $("#export-items").focus?.();
    return;
  }
  submitToolForm(event.currentTarget, $("#export-result"), "正在导出…", () => api("/api/tools/export-pdfs", {method: "POST", body: JSON.stringify({...exportSourcePayload(), destination: $("#export-destination").value, open_folder: $("#export-open-folder").checked, item_ids: itemIds})}), result => `已复制 ${result.copied} 个 PDF · ${result.destination}`);
});

$("#export-endnote-form").addEventListener("submit", event => {
  event.preventDefault();
  submitToolForm(event.currentTarget, $("#endnote-export-result"), "正在导出…", () => api("/api/tools/export-endnote", {method: "POST", body: JSON.stringify({collection: $("#endnote-collection").value, destination: $("#endnote-destination").value, open_folder: $("#endnote-open-folder").checked})}), result => `已导出 ${result.record_count} 篇题录、${result.pdf_count} 个 PDF`);
});

$("#sync-endnote-zotero-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type='submit']");
  const resultNode = $("#sync-endnote-result");
  const report = $("#sync-endnote-report");
  const collection = $("#sync-zotero-collection").value.trim();
  if (!collection) {
    toast("请填写目标 Zotero collection");
    $("#sync-zotero-collection").focus();
    return;
  }
  report.hidden = true;
  report.className = "sync-report";
  resultNode.textContent = "正在从 EndNote 同步…";
  setButtonBusy(button, true, "同步中…");
  try {
    const result = await api("/api/tools/sync-endnote-zotero", {
      method: "POST",
      body: JSON.stringify({collection}),
    });
    resultNode.textContent = `已同步 ${result.synced} 篇 · 新增 ${result.created} · 匹配 ${result.matched}`;
    const skippedDetails = (result.records || [])
      .filter(row => row.status === "skipped")
      .slice(0, 3)
      .map(row => `${row.title || row.id}${row.error ? `（${row.error}）` : ""}`)
      .join("、");
    report.hidden = false;
    report.innerHTML = `<strong>PDF 新增 ${result.pdf_attached}，已有 ${result.pdf_existing}</strong>${result.skipped ? `<br>跳过 ${result.skipped} 篇${skippedDetails ? `：${esc(skippedDetails)}` : ""}` : ""}${result.failed ? `<br>失败 ${result.failed} 篇：${esc((result.errors || []).slice(0, 3).map(item => item.title).join("、"))}` : ""}`;
    report.classList.toggle("error", result.failed > 0);
    toast(resultNode.textContent);
  } catch (error) {
    resultNode.textContent = error.message;
    report.hidden = false;
    report.className = "sync-report error";
    report.textContent = error.message;
    toast(error.message);
  } finally {
    setButtonBusy(button, false);
  }
});

$("#authorize-zotero").addEventListener("click", async event => {
  setButtonBusy(event.currentTarget, true, "等待授权…");
  try {
    await api("/api/zotero/authorize", {method: "POST", body: "{}"});
    toast("Zotero 已授权");
    await loadSystem();
  } catch (error) {
    toast(error.message);
    setButtonBusy(event.currentTarget, false);
  }
});

$("#resolve-doi").addEventListener("click", async event => {
  if (!state.currentPaper) return;
  setButtonBusy(event.currentTarget, true, "解析中…");
  try {
    await api(`/api/papers/${encodeURIComponent(state.currentPaper.id)}/resolve-doi`, {method: "POST", body: JSON.stringify({doi: $("#manual-doi").value})});
    $("#candidate-dialog").close();
    toast("已按 DOI 重新解析");
    await renderBatch();
  } catch (error) {
    toast(error.message);
  } finally {
    setButtonBusy(event.currentTarget, false);
  }
});

$("#accept-pdf").addEventListener("click", async event => {
  if (!state.currentPaper) return;
  setButtonBusy(event.currentTarget, true, "保存中…");
  try {
    await api(`/api/papers/${encodeURIComponent(state.currentPaper.id)}/confirm-pdf`, {method: "POST", body: JSON.stringify({accept: true, version: $("#pdf-version").value})});
    $("#pdf-dialog").close();
    await renderBatch();
  } catch (error) {
    toast(error.message);
    setButtonBusy(event.currentTarget, false);
  }
});

$("#reject-pdf").addEventListener("click", async event => {
  if (!state.currentPaper) return;
  setButtonBusy(event.currentTarget, true, "保存中…");
  try {
    await api(`/api/papers/${encodeURIComponent(state.currentPaper.id)}/confirm-pdf`, {method: "POST", body: JSON.stringify({accept: false, version: "unknown"})});
    $("#pdf-dialog").close();
    await renderBatch();
  } catch (error) {
    toast(error.message);
    setButtonBusy(event.currentTarget, false);
  }
});

$("#confirm-delete").addEventListener("click", beginDeletion);
$("#cancel-delete").addEventListener("click", closeDeleteDialog);
$("#delete-dialog").addEventListener("cancel", event => {
  event.preventDefault();
  if (!state.deletionActive) closeDeleteDialog();
});
$("#delete-dialog").addEventListener("close", () => {
  if (!state.deletionActive) {
    clearTimeout(state.deletePollTimer);
    state.deleteOperationId = null;
  }
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  if (state.activeTab === "tasks") pollTasks();
  if (state.activeTab === "settings") refreshSystemStatus().catch(error => toast(error.message));
});

initHelpPopovers();
loadSystem().then(() => refreshBatches({renderDetail: false})).catch(error => {
  toast(error.message);
  $("#system-badge").textContent = "环境检查失败";
  $("#system-badge").className = "badge error";
});
