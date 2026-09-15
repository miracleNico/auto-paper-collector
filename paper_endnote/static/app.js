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
}

async function loadSystem() {
  state.system = await api("/api/state");
  const probe = state.system.zotero;
  const badge = $("#system-badge");
  badge.textContent = probe.ready ? "Zotero API 可写" : (probe.enabled ? "Zotero 待授权" : "Zotero API 未启用");
  badge.className = `badge ${probe.ready ? "" : "warn"}`;
  $("#system-info").textContent = JSON.stringify(state.system, null, 2);
  $("#crossref-email").value = state.system.settings.crossref_email || "";
  $("#unpaywall-email").value = state.system.settings.unpaywall_email || "";
  $("#endnote-library").value = state.system.settings.endnote_library || "";
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
  return ({draft:"未开始",running:"处理中",paused:"已暂停",completed:"阶段完成",failed:"失败",queued:"排队",matching:"匹配中",needs_match:"待确认题录",ready:"题录就绪",looking_for_pdf:"查找全文",needs_pdf:"待补全文",needs_pdf_review:"待确认PDF",endnote_pending:"待写入文献库",complete:"已完成",skipped:"已跳过"})[value] || value || "未知";
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
    ${exportPath ? `<p class="muted">EndNote 导入包：${esc(exportPath)}。在 EndNote 中 File → Import → File，选择 records.xml，Import Option 选 EndNote Generated XML。</p>` : `<p class="muted">提交到 Zotero 后会自动从 Zotero 导出 EndNote XML/PDF 包。</p>`}
    ${batch.error ? `<p class="error-text">${esc(batch.error)}</p>` : ""}
    <div class="table-wrap"><table><thead><tr><th>#</th><th>论文</th><th>题录</th><th>PDF</th><th>Zotero</th><th>状态/操作</th></tr></thead>
    <tbody>${batch.papers.map(paperRow).join("")}</tbody></table></div>`;
  $("[data-action='pause']")?.addEventListener("click", () => batchAction("pause"));
  $("[data-action='resume']")?.addEventListener("click", () => batchAction("resume"));
  $("[data-action='commit']")?.addEventListener("click", () => batchAction("commit"));
  $("[data-action='export-endnote']")?.addEventListener("click", () => batchAction("export-endnote"));
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
  if (["manual_pdf","confirm_pdf"].includes(p.needs_action)) {
    parts.push(`<button data-paper-action="open" data-paper="${p.id}">打开获取页</button>`);
    parts.push(`<button data-paper-action="institution" data-paper="${p.id}">机构自动获取</button>`);
    parts.push(`<button data-paper-action="resolver" data-paper="${p.id}">McGill 馆藏</button>`);
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
    commit: "已启动 Zotero 提交，完成后会导出 EndNote 导入包",
    "export-endnote": "正在从 Zotero 导出 EndNote 导入包",
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
$("#settings-form").onsubmit = async event => { event.preventDefault(); try { await api("/api/settings", {method:"POST",body:JSON.stringify({crossref_email:$("#crossref-email").value,unpaywall_email:$("#unpaywall-email").value,endnote_library:$("#endnote-library").value})}); toast("设置已保存"); await loadSystem(); } catch (error) { toast(error.message); } };
$("#authorize-zotero").onclick = async () => { try { toast("请在 Zotero 弹窗中选择 Always Allow"); await api("/api/zotero/authorize", {method:"POST",body:"{}"}); toast("Zotero 已授权"); await loadSystem(); } catch (error) { toast(error.message); } };
$("#accept-pdf").onclick = async () => { try { await api(`/api/papers/${state.currentPaper.id}/confirm-pdf`, {method:"POST",body:JSON.stringify({accept:true,version:$("#pdf-version").value})}); $("#pdf-dialog").close(); await renderBatch(); } catch (error) { toast(error.message); } };
$("#reject-pdf").onclick = async () => { try { await api(`/api/papers/${state.currentPaper.id}/confirm-pdf`, {method:"POST",body:JSON.stringify({accept:false,version:"unknown"})}); $("#pdf-dialog").close(); await renderBatch(); } catch (error) { toast(error.message); } };

loadSystem().then(refreshBatches).catch(error => toast(error.message));
