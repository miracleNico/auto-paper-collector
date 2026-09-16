const state = { batches: [], selectedBatch: null, pollTimer: null, currentPaper: null, system: null };
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: { ...(options.body instanceof FormData ? {} : {"Content-Type":"application/json"}), ...(options.headers || {}) } });
  if (!response.ok) {
    let message = await response.text();
    try { message = JSON.parse(message).detail || message; } catch (_) {}
    throw new Error(message || `HTTP ${response.status}`);
  }
  return response.headers.get("content-type")?.includes("json") ? response.json() : response.text();
}

function toast(message) {
  const node = $("#toast"); node.textContent = message; node.classList.add("show");
  clearTimeout(node.timer); node.timer = setTimeout(() => node.classList.remove("show"), 3500);
}

function showTab(name) {
  document.querySelectorAll(".tab,.tab-panel").forEach(n => n.classList.remove("active"));
  document.querySelector(`.tab[data-tab="${name}"]`).classList.add("active");
  $(`#tab-${name}`).classList.add("active");
  if (name === "tasks") refreshBatches();
  if (name === "tools") return loadToolSources();
}

async function loadSystem() {
  state.system = await api("/api/state");
  const probe = state.system.zotero;
  const badge = $("#system-badge");
  badge.textContent = probe.ready ? "Zotero API 可写" : (probe.enabled ? "Zotero 待授权" : "Zotero API 未启用");
  badge.className = `badge ${probe.ready ? "" : "warn"}`;
  $("#system-info").textContent = JSON.stringify(state.system, null, 2);
  const cfg = state.system.settings;
  $("#crossref-email").value = cfg.crossref_email || "";
  $("#unpaywall-email").value = cfg.unpaywall_email || "";
  $("#endnote-library").value = cfg.endnote_library || "";
  $("#source-open-access").checked = (cfg.acquisition_sources || []).includes("open_access");
  $("#source-institution").checked = (cfg.acquisition_sources || []).includes("institution");
  $("#auto-institution").checked = cfg.auto_institution !== false;
  $("#auto-commit").checked = cfg.auto_commit !== false;
  $("#login-wait-seconds").value = cfg.login_wait_seconds || 600;
  $("#ocr-enabled").checked = !!cfg.ocr_enabled;
  $("#ocr-languages").value = cfg.ocr_languages || "eng";
  $("#ocr-max-pages").value = cfg.ocr_max_pages || 2;
  const ocr = state.system.ocr || {};
  $("#ocr-status").textContent = ocr.ready
    ? `Tesseract ${ocr.tesseract_version} 可用，扫描件可用 OCR 匹配题名。`
    : "未检测到 Tesseract。扫描件仍会进入人工确认；安装 Tesseract 后可自动匹配题名。";
  fillInstitution(cfg.institution || {}, state.system.presets || []);
  const cred = state.system.credentials || {};
  $("#institution-username").value = cred.username || "";
  $("#institution-password").value = "";
  $("#credentials-status").textContent = cred.password_saved
    ? `已在 Windows 凭据管理器保存 ${cfg.institution?.name || "机构"} 账号 ${cred.username}。`
    : "尚未保存机构密码。";
}

function fillInstitution(institution, presets) {
  const select = $("#institution-preset");
  const current = institution.preset || institution.id || "";
  select.innerHTML = [`<option value="">自定义</option>`].concat(
    presets.map(preset => `<option value="${esc(preset.preset || preset.id)}">${esc(preset.name)}</option>`)
  ).join("");
  select.value = [...select.options].some(option => option.value === current) ? current : "";
  $("#institution-id").value = institution.id || "";
  $("#institution-name").value = institution.name || "";
  $("#institution-login").value = institution.ezproxy_login || "";
  $("#institution-hosts").value = (institution.ezproxy_hosts || []).join(", ");
  $("#institution-openurl").value = institution.openurl || "";
  $("#institution-login-markers").value = (institution.login_url_markers || []).join(", ");
}

async function refreshBatches() {
  state.batches = await api("/api/batches");
  $("#batch-list").innerHTML = state.batches.length ? state.batches.map(batch => `
    <button class="batch-item ${state.selectedBatch === batch.id ? "active" : ""}" data-batch="${batch.id}">
      <strong>${esc(batch.name)}</strong><span>${batch.completed || 0}/${batch.total || 0} 完成 · ${statusLabel(batch.status)}</span>
    </button>`).join("") : `<p class="muted">尚无任务</p>`;
  document.querySelectorAll("[data-batch]").forEach(button => button.onclick = () => selectBatch(button.dataset.batch));
  if (state.selectedBatch) await renderBatch();
}

async function selectBatch(id) {
  state.selectedBatch = id;
  await refreshBatches();
  startPolling();
}

function statusLabel(value) {
  return ({draft:"未开始",running:"处理中",paused:"已暂停",completed:"阶段完成",failed:"失败",queued:"排队",matching:"匹配中",needs_match:"待确认题录",ready:"题录就绪",looking_for_pdf:"查找全文",institution_pending:"机构获取中",needs_pdf:"待补全文",needs_pdf_review:"待确认PDF",endnote_pending:"待写入文献库",complete:"已完成",skipped:"已跳过"})[value] || value || "未知";
}

async function renderBatch() {
  const batch = await api(`/api/batches/${state.selectedBatch}`);
  const counts = {complete:0, needs:0, pdf:0, endnote:0};
  batch.papers.forEach(p => { if (p.status === "complete") counts.complete++; if (p.needs_action) counts.needs++; if (["verified","accepted"].includes(p.pdf_status)) counts.pdf++; if (p.endnote_status === "verified") counts.endnote++; });
  const pct = batch.papers.length ? Math.round(counts.complete / batch.papers.length * 100) : 0;
  const exportPath = batch.endnote_export_path;
  $("#task-detail").innerHTML = `
    <div class="task-head"><div><h2>${esc(batch.name)}</h2><p class="muted">Zotero · ${esc(batch.target_library)}</p></div>
      <div class="task-actions">
        ${batch.status === "running" ? `<button data-action="pause">暂停</button>` : `<button data-action="resume">继续匹配/获取</button>`}
        <button data-action="institution-login">机构登录</button>
        <button data-action="rename-pdfs">按题录重命名 PDF</button>
        <button data-action="export-pdfs">导出 PDF</button>
        <button class="primary" data-action="commit">提交到 Zotero</button>
        <button data-action="export-endnote">导出 EndNote 导入包</button>
        ${exportPath ? `<a href="/api/batches/${batch.id}/endnote-export.zip"><button>下载 ZIP</button></a><button data-action="open-endnote-export">打开文件夹</button>` : ""}
        <a href="/api/batches/${batch.id}/report.csv"><button>下载报告</button></a>
      </div></div>
    <div class="progress"><span style="width:${pct}%"></span></div>
    <div class="summary-grid">
      <div class="summary-card"><b>${batch.papers.length}</b><span>论文</span></div>
      <div class="summary-card"><b>${counts.pdf}</b><span>PDF 已验证</span></div>
      <div class="summary-card"><b>${counts.endnote}</b><span>Zotero 已核验</span></div>
      <div class="summary-card"><b>${counts.needs}</b><span>需要处理</span></div>
    </div>
    ${exportPath ? `<p class="muted">EndNote 导入包：${esc(exportPath)}。推荐导入 records.ris：File → Import → File，Import Option 选 Reference Manager (RIS)，Text Translation 选 Unicode (UTF-8)。不要导入 ZIP。</p>` : `<p class="muted">登录一次后，批次会自动走机构获取；完成后可自动提交 Zotero。EndNote 导入包请到「工具」页手动导出，默认写入 Downloads\\[库名]。2FA、验证码和出版社 403 仍需人工。</p>`}
    ${batch.error ? `<p class="error-text">${esc(batch.error)}</p>` : ""}
    <div class="table-wrap"><table><thead><tr><th>#</th><th>论文</th><th>题录</th><th>PDF</th><th>Zotero</th><th>状态/操作</th></tr></thead>
    <tbody>${batch.papers.map(paperRow).join("")}</tbody></table></div>`;
  $("[data-action='pause']")?.addEventListener("click", () => batchAction("pause"));
  $("[data-action='resume']")?.addEventListener("click", () => batchAction("resume"));
  $("[data-action='institution-login']")?.addEventListener("click", async () => {
    try { await api("/api/institution/login", {method:"POST",body:"{}"}); toast("已打开机构登录页，请在 Chrome 中完成登录或 2FA"); }
    catch (error) { toast(error.message); }
  });
  $("[data-action='rename-pdfs']")?.addEventListener("click", async () => {
    try {
      const result = await api("/api/tools/rename-pdfs", {method:"POST",body:JSON.stringify({source:"batch",batch_id:batch.id})});
      toast(`已按题录重命名 ${result.renamed} 个 PDF，跳过 ${result.skipped} 篇`);
      await renderBatch();
    } catch (error) { toast(error.message); }
  });
  $("[data-action='export-pdfs']")?.addEventListener("click", async () => {
    await showTab("tools");
    await loadToolSources();
    $("#export-source").value = "batch";
    syncToolSource("export");
    $("#export-batch").value = batch.id;
    toast("填写目标文件夹后点击导出");
  });
  $("[data-action='commit']")?.addEventListener("click", () => batchAction("commit"));
  $("[data-action='export-endnote']")?.addEventListener("click", async () => {
    await showTab("tools");
    await loadToolSources();
    if (batch.target_library) $("#endnote-collection").value = batch.target_library;
    fillEndnoteDestination();
    toast("确认目标文件夹后点击导出导入包，默认写入 Downloads\\[库名]");
  });
  $("[data-action='open-endnote-export']")?.addEventListener("click", () => batchAction("open-endnote-export"));
  bindPaperActions(batch.papers);
}

function paperRow(p) {
  const paperTitle = p.title || p.input_title || p.input_text;
  return `<tr>
    <td>${p.position}</td>
    <td class="title"><strong>${esc(paperTitle)}</strong><br><small>${esc(p.doi || "无 DOI")} ${p.year ? `· ${p.year}` : ""}</small>${p.error ? `<div class="error-text">${esc(p.error)}</div>` : ""}</td>
    <td>${esc(p.metadata_status)}</td><td>${esc(p.pdf_status)}${p.version ? `<br><small>${esc(p.version)}</small>` : ""}</td><td>${esc(p.endnote_status)}</td>
    <td class="actions"><span class="status ${esc(p.status)}">${statusLabel(p.status)}</span><br>
      ${paperActions(p)}</td></tr>`;
}

function paperActions(p) {
  const parts = [];
  if (p.needs_action === "confirm_metadata") parts.push(`<button data-paper-action="metadata" data-paper="${p.id}">确认题录</button>`);
  if (["manual_pdf","confirm_pdf"].includes(p.needs_action) || p.status === "institution_pending") {
    parts.push(`<button data-paper-action="open" data-paper="${p.id}">打开获取页</button>`);
    parts.push(`<button data-paper-action="institution" data-paper="${p.id}">机构自动获取</button>`);
    if (state.system?.settings?.institution?.openurl) {
      parts.push(`<button data-paper-action="resolver" data-paper="${p.id}">机构馆藏</button>`);
    }
    parts.push(`<button data-paper-action="scholar" data-paper="${p.id}">Scholar</button>`);
    parts.push(`<label class="upload-label file-button">提供 PDF<input type="file" accept="application/pdf,.pdf" data-paper-upload="${p.id}"></label>`);
  }
  if (p.needs_action === "confirm_pdf") parts.push(`<button data-paper-action="pdf" data-paper="${p.id}">确认 PDF</button>`);
  if (["retry","reconcile_endnote"].includes(p.needs_action)) parts.push(`<button data-paper-action="retry" data-paper="${p.id}">重试/对账</button>`);
  if (p.status !== "complete" && p.status !== "skipped") parts.push(`<button data-paper-action="skip" data-paper="${p.id}">跳过</button>`);
  return parts.join("");
}

function bindPaperActions(papers) {
  document.querySelectorAll("[data-paper-action]").forEach(button => button.onclick = async () => {
    const paper = papers.find(p => p.id === button.dataset.paper); const action = button.dataset.paperAction;
    try {
      if (action === "metadata") return showCandidates(paper);
      if (action === "pdf") return showPdfReview(paper);
      if (["open", "scholar", "resolver"].includes(action)) await api(`/api/papers/${paper.id}/open?scholar=${action === "scholar"}&resolver=${action === "resolver"}`, {method:"POST",body:"{}"});
      if (action === "institution") { toast("正在通过已登录的 Chrome 会话获取单篇 PDF…"); await api(`/api/papers/${paper.id}/acquire-institution`, {method:"POST",body:"{}"}); toast("机构 PDF 已下载并验证"); }
      if (action === "retry" || action === "skip") await api(`/api/papers/${paper.id}/${action}`, {method:"POST",body:"{}"});
      await renderBatch();
    } catch (error) { toast(error.message); }
  });
  document.querySelectorAll("[data-paper-upload]").forEach(input => input.onchange = async () => {
    if (!input.files[0]) return; const data = new FormData(); data.append("file", input.files[0]);
    try { await api(`/api/papers/${input.dataset.paperUpload}/upload-pdf`, {method:"POST",body:data}); toast("PDF 已接收"); await renderBatch(); }
    catch (error) { toast(error.message); }
  });
}

async function showCandidates(paper) {
  const candidates = await api(`/api/papers/${paper.id}/candidates`); state.currentPaper = paper;
  $("#candidate-source").textContent = `输入：${paper.input_text}`;
  $("#manual-doi").value = paper.input_doi || paper.doi || "";
  $("#candidate-list").innerHTML = candidates.length ? candidates.map(c => `<div class="candidate"><h3>${esc(c.metadata.title)}</h3><p>${esc((c.metadata.authors || []).join("; "))}</p><p>${esc(c.metadata.journal)} · ${esc(c.metadata.year || "年份未知")} · ${esc(c.metadata.doi || "无 DOI")}</p><button class="primary" data-candidate="${c.id}">采用此题录</button></div>`).join("") : `<p>Crossref 没有返回候选。请在 Scholar 核对后重试，或先跳过。</p>`;
  document.querySelectorAll("[data-candidate]").forEach(button => button.onclick = async () => {
    try { await api(`/api/papers/${paper.id}/confirm-metadata`, {method:"POST",body:JSON.stringify({candidate_id:button.dataset.candidate})}); $("#candidate-dialog").close(); await renderBatch(); }
    catch (error) { toast(error.message); }
  });
  $("#candidate-dialog").showModal();
}

$("#resolve-doi").onclick = async () => {
  try {
    await api(`/api/papers/${state.currentPaper.id}/resolve-doi`, {method:"POST", body:JSON.stringify({doi:$("#manual-doi").value})});
    $("#candidate-dialog").close(); toast("已按 DOI 重新解析"); await renderBatch();
  } catch (error) { toast(error.message); }
};

function showPdfReview(paper) { state.currentPaper = paper; $("#pdf-review-text").textContent = paper.error || "请确认此文件是论文主文，并选择版本。"; $("#pdf-dialog").showModal(); }

async function batchAction(action) {
  const messages = {
    commit: "已启动 Zotero 提交",
    "open-endnote-export": "已打开导出文件夹",
  };
  try {
    await api(`/api/batches/${state.selectedBatch}/${action}`, {method:"POST",body:"{}"});
    toast(messages[action] || "状态已更新");
    await renderBatch();
  } catch (error) { toast(error.message); }
}

function startPolling() { clearInterval(state.pollTimer); state.pollTimer = setInterval(() => { if (state.selectedBatch && !document.hidden) renderBatch().catch(()=>{}); }, 2500); }

document.querySelectorAll(".tab").forEach(button => button.onclick = () => showTab(button.dataset.tab));
$("#refresh-batches").onclick = refreshBatches;
$("#list-file").onchange = async event => { const file = event.target.files[0]; if (file) { $("#items-text").value = await file.text(); $("#parse-summary").textContent = `已读取 ${file.name}`; } };
$("#batch-form").onsubmit = async event => {
  event.preventDefault();
  try {
    const result = await api("/api/batches", {method:"POST",body:JSON.stringify({name:$("#batch-name").value,items_text:$("#items-text").value,target_library:$("#target-library").value,library_mode:$("#library-mode").value,reference_manager:"zotero",start_immediately:true})});
    state.selectedBatch = result.id; showTab("tasks"); toast("批次已创建");
  } catch (error) { toast(error.message); }
};
$("#settings-form").onsubmit = async event => {
  event.preventDefault();
  const sources = [];
  if ($("#source-open-access").checked) sources.push("open_access");
  if ($("#source-institution").checked) sources.push("institution");
  const split = value => value.split(",").map(item => item.trim()).filter(Boolean);
  try {
    await api("/api/settings", {method:"POST",body:JSON.stringify({
      crossref_email:$("#crossref-email").value,
      unpaywall_email:$("#unpaywall-email").value,
      endnote_library:$("#endnote-library").value,
      acquisition_sources: sources,
      ocr_enabled: $("#ocr-enabled").checked,
      ocr_languages: $("#ocr-languages").value,
      ocr_max_pages: Number($("#ocr-max-pages").value || 2),
      auto_institution: $("#auto-institution").checked,
      auto_commit: $("#auto-commit").checked,
      login_wait_seconds: Number($("#login-wait-seconds").value || 600),
      institution: {
        preset: $("#institution-preset").value,
        id: $("#institution-id").value,
        name: $("#institution-name").value,
        ezproxy_login: $("#institution-login").value,
        ezproxy_hosts: split($("#institution-hosts").value),
        openurl: $("#institution-openurl").value,
        login_url_markers: split($("#institution-login-markers").value),
      }
    })});
    toast("设置已保存");
    await loadSystem();
  } catch (error) { toast(error.message); }
};
$("#institution-preset").onchange = () => {
  const selected = $("#institution-preset").value;
  const preset = (state.system?.presets || []).find(item => (item.preset || item.id) === selected);
  if (preset) fillInstitution(preset, state.system.presets || []);
};
$("#credentials-form").onsubmit = async event => {
  event.preventDefault();
  try {
    await api("/api/credentials", {method:"POST",body:JSON.stringify({
      username: $("#institution-username").value,
      password: $("#institution-password").value,
    })});
    toast("凭据已保存到 Windows 凭据管理器");
    await loadSystem();
  } catch (error) { toast(error.message); }
};
$("#clear-credentials").onclick = async () => {
  try {
    await api("/api/credentials/clear", {method:"POST",body:"{}"});
    toast("已清除机构凭据");
    await loadSystem();
  } catch (error) { toast(error.message); }
};
$("#open-institution-login").onclick = async () => {
  try {
    await api("/api/institution/login", {method:"POST",body:"{}"});
    toast("已打开机构登录页，请在 Chrome 中完成登录或 2FA");
  } catch (error) { toast(error.message); }
};

function optionList(items, valueKey, labelFn) {
  if (!items.length) return `<option value="">无可用项</option>`;
  return items.map(item => `<option value="${esc(item[valueKey])}">${esc(labelFn(item))}</option>`).join("");
}

function syncToolSource(prefix) {
  const source = $(`#${prefix}-source`).value;
  $(`#${prefix}-collection-wrap`).hidden = source !== "zotero";
  $(`#${prefix}-batch-wrap`).hidden = source !== "batch";
  const endnoteWrap = $(`#${prefix}-endnote-wrap`);
  if (endnoteWrap) endnoteWrap.hidden = source !== "endnote";
  if (prefix === "export") fillExportDestination();
}

function fillExportDestination() {
  const sources = state.toolSources || {};
  const downloads = sources.downloads_dir || "";
  const source = $("#export-source").value;
  let name = "";
  if (source === "zotero") name = $("#export-collection").value;
  if (source === "batch") {
    const batch = (sources.batches || []).find(item => item.id === $("#export-batch").value);
    name = batch?.target_library || batch?.name || "";
  }
  if (source === "endnote" && sources.endnote_library) {
    const parts = String(sources.endnote_library).split(/[/\\]/);
    name = (parts[parts.length - 1] || "").replace(/\.enl$/i, "");
  }
  if (downloads && name) $("#export-destination").placeholder = `${downloads}\\${name}`;
  if (downloads && name && !$("#export-destination").dataset.custom) {
    $("#export-destination").value = `${downloads}\\${name}`;
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
  const sources = await api("/api/tools/sources");
  state.toolSources = sources;
  const batches = sources.batches || [];
  const collections = sources.collections || [];
  const batchHtml = optionList(batches, "id", item => `${item.name} · ${item.total || 0} 篇`);
  const collectionHtml = optionList(collections, "name", item => item.numItems == null ? item.name : `${item.name} · ${item.numItems}`);
  $("#rename-batch").innerHTML = batchHtml;
  $("#export-batch").innerHTML = batchHtml;
  $("#rename-collection").innerHTML = collectionHtml;
  $("#export-collection").innerHTML = collectionHtml;
  $("#endnote-collection").innerHTML = collectionHtml;
  if ($("#rename-endnote-library")) $("#rename-endnote-library").value = sources.endnote_library || "";
  syncToolSource("rename");
  syncToolSource("export");
  fillExportDestination();
  fillEndnoteDestination();
}

$("#rename-source").onchange = () => syncToolSource("rename");
$("#export-source").onchange = () => syncToolSource("export");
$("#export-collection").onchange = fillExportDestination;
$("#export-batch").onchange = fillExportDestination;
$("#export-destination").oninput = () => { $("#export-destination").dataset.custom = "1"; };
$("#endnote-collection").onchange = fillEndnoteDestination;
$("#endnote-destination").oninput = () => { $("#endnote-destination").dataset.custom = "1"; };

$("#rename-pdfs-form").onsubmit = async event => {
  event.preventDefault();
  $("#rename-result").textContent = "正在重命名…";
  try {
    const source = $("#rename-source").value;
    const result = await api("/api/tools/rename-pdfs", {method:"POST",body:JSON.stringify({
      source,
      batch_id: $("#rename-batch").value,
      collection: $("#rename-collection").value,
    })});
    $("#rename-result").textContent = `完成：重命名 ${result.renamed} 个，跳过 ${result.skipped} 个。`;
    toast($("#rename-result").textContent);
  } catch (error) {
    $("#rename-result").textContent = error.message;
    toast(error.message);
  }
};
$("#export-pdfs-form").onsubmit = async event => {
  event.preventDefault();
  $("#export-result").textContent = "正在导出…";
  try {
    const source = $("#export-source").value;
    const result = await api("/api/tools/export-pdfs", {method:"POST",body:JSON.stringify({
      source,
      batch_id: $("#export-batch").value,
      collection: $("#export-collection").value,
      destination: $("#export-destination").value,
      open_folder: $("#export-open-folder").checked,
    })});
    $("#export-result").textContent = `完成：复制 ${result.copied} 个 PDF 到 ${result.destination}。`;
    toast($("#export-result").textContent);
  } catch (error) {
    $("#export-result").textContent = error.message;
    toast(error.message);
  }
};
$("#export-endnote-form").onsubmit = async event => {
  event.preventDefault();
  $("#endnote-export-result").textContent = "正在导出 EndNote 导入包…";
  try {
    const result = await api("/api/tools/export-endnote", {method:"POST",body:JSON.stringify({
      collection: $("#endnote-collection").value,
      destination: $("#endnote-destination").value,
      open_folder: $("#endnote-open-folder").checked,
    })});
    $("#endnote-export-result").textContent = `完成：${result.record_count} 篇题录、${result.pdf_count} 个 PDF，写入 ${result.destination}。`;
    toast($("#endnote-export-result").textContent);
  } catch (error) {
    $("#endnote-export-result").textContent = error.message;
    toast(error.message);
  }
};
$("#authorize-zotero").onclick = async () => { try { toast("请在 Zotero 弹窗中选择 Always Allow"); await api("/api/zotero/authorize", {method:"POST",body:"{}"}); toast("Zotero 已授权"); await loadSystem(); } catch (error) { toast(error.message); } };
$("#accept-pdf").onclick = async () => { try { await api(`/api/papers/${state.currentPaper.id}/confirm-pdf`, {method:"POST",body:JSON.stringify({accept:true,version:$("#pdf-version").value})}); $("#pdf-dialog").close(); await renderBatch(); } catch (error) { toast(error.message); } };
$("#reject-pdf").onclick = async () => { try { await api(`/api/papers/${state.currentPaper.id}/confirm-pdf`, {method:"POST",body:JSON.stringify({accept:false,version:"unknown"})}); $("#pdf-dialog").close(); await renderBatch(); } catch (error) { toast(error.message); } };

loadSystem().then(refreshBatches).catch(error => toast(error.message));
