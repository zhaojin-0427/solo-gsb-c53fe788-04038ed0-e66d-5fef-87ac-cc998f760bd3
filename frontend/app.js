/* 样本链路与隔离处置台 —— 原生 JS 前端 */
"use strict";

const state = {
  token: localStorage.getItem("sc_token") || "",
  user: JSON.parse(localStorage.getItem("sc_user") || "null"),
  batches: [],
  handoverKey: "",   // 幂等键：查询样本时生成，提交成功后才轮换
  importId: "",      // 温度导入幂等键
};

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const shortHash = (h) => (h ? `${h.slice(0, 10)}…${h.slice(-6)}` : "-");

/* ---------------------------------------------------------------- 基础 */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: {
      "Content-Type": "application/json",
      ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}),
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) {
    logout();
    throw new Error("登录已过期，请重新登录");
  }
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch { /* 非 JSON */ }
  if (!res.ok) {
    const d = data && data.detail;
    const e = new Error((d && d.message) || (typeof d === "string" ? d : "") || `请求失败 (${res.status})`);
    e.code = d && d.code;
    e.extra = d || {};
    e.status = res.status;
    throw e;
  }
  return data;
}

let toastTimer = null;
function toast(msg, type = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3500);
}

function alertHtml(kind, msg) {
  return `<div class="alert alert-${kind}">${esc(msg)}</div>`;
}

function tableHtml(headers, rows, emptyText = "暂无数据") {
  if (!rows.length) return `<tbody><tr><td style="color:#94a3b8">${esc(emptyText)}</td></tr></tbody>`;
  return `<thead><tr>${headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody>`;
}

/* ---------------------------------------------------------------- 登录 */

function showLogin() {
  $("#login-view").classList.remove("hidden");
  $("#app-view").classList.add("hidden");
  $("#user-box").classList.add("hidden");
}

function showApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  $("#user-box").classList.remove("hidden");
  const roleName = state.user.role === "supervisor" ? "授权主管" : "工作人员";
  $("#user-label").innerHTML = `${esc(state.user.username)} <span class="role-tag">${roleName}</span>`;
  refreshAll();
}

function logout() {
  state.token = "";
  state.user = null;
  localStorage.removeItem("sc_token");
  localStorage.removeItem("sc_user");
  showLogin();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: { username: $("#login-username").value.trim(), password: $("#login-password").value },
    });
    state.token = data.token;
    state.user = data.user;
    localStorage.setItem("sc_token", data.token);
    localStorage.setItem("sc_user", JSON.stringify(data.user));
    showApp();
    toast(`欢迎，${data.user.username}`, "ok");
  } catch (err) {
    toast(err.message, "error");
  }
});

$("#btn-logout").addEventListener("click", logout);

/* ---------------------------------------------------------------- 标签页 */

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.add("hidden"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.remove("hidden");
    loadTab(btn.dataset.tab);
  });
});

function loadTab(name) {
  if (name === "batches") loadBatches();
  if (name === "handover") loadRecentHandovers();
  if (name === "temperature") loadReadings();
  if (name === "events") loadEvents();
  if (name === "audit") loadAudit();
}

async function refreshAll() {
  await loadBatches();
  loadRecentHandovers();
  loadReadings();
  loadEvents();
  loadAudit();
}

/* ---------------------------------------------------------------- 批次与样本 */

async function loadBatches() {
  try {
    const data = await api("/api/batches");
    state.batches = data.batches;
    const opts = data.batches.map((b) => `<option value="${esc(b.batch_code)}">${esc(b.batch_code)}（${esc(b.name)}）</option>`).join("");
    $("#samples-batch-select").innerHTML = opts || `<option value="">（请先创建批次）</option>`;
    $("#temp-batch-select").innerHTML = opts || `<option value="">（请先创建批次）</option>`;
    $("#batches-table").innerHTML = tableHtml(
      ["批次编码", "名称", "温度阈值", "样本数", "待处置异常", "状态", "创建人", "创建时间", "操作"],
      data.batches.map((b) => `<tr>
        <td class="mono">${esc(b.batch_code)}</td>
        <td>${esc(b.name)}</td>
        <td>${b.temp_min} ~ ${b.temp_max} ℃</td>
        <td>${b.sample_count}</td>
        <td>${b.open_events || "-"}</td>
        <td>${b.frozen ? '<span class="tag tag-frozen">已冻结</span>' : '<span class="tag tag-active">正常</span>'}</td>
        <td>${esc(b.created_by)}</td>
        <td class="mono">${esc(b.created_at)}</td>
        <td><button class="btn btn-sm" data-samples-of="${esc(b.batch_code)}" type="button">查看样本</button></td>
      </tr>`)
    );
  } catch (err) { toast(err.message, "error"); }
}

$("#batch-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/batches", {
      method: "POST",
      body: {
        batch_code: $("#batch-code").value,
        name: $("#batch-name").value,
        description: $("#batch-desc").value,
        temp_min: parseFloat($("#batch-tmin").value),
        temp_max: parseFloat($("#batch-tmax").value),
      },
    });
    toast("批次已创建", "ok");
    e.target.reset();
    $("#batch-tmin").value = "2";
    $("#batch-tmax").value = "8";
    loadBatches();
  } catch (err) { toast(err.message, "error"); }
});

$("#samples-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const batchCode = $("#samples-batch-select").value;
  const barcodes = $("#samples-barcodes").value.split("\n").map((s) => s.trim()).filter(Boolean);
  if (!batchCode || !barcodes.length) return;
  try {
    const data = await api(`/api/batches/${encodeURIComponent(batchCode)}/samples`, {
      method: "POST",
      body: { barcodes },
    });
    $("#samples-result").innerHTML =
      alertHtml("ok", `新登记 ${data.added.length} 个条码${data.existing.length ? `，${data.existing.length} 个已存在（幂等跳过）` : ""}`) +
      (data.added.length ? `<p class="hint">登记人已成为这些样本的第一任持有人。</p>` : "");
    $("#samples-barcodes").value = "";
    loadBatches();
  } catch (err) { toast(err.message, "error"); }
});

$("#batches-table").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-samples-of]");
  if (!btn) return;
  const code = btn.dataset.samplesOf;
  try {
    const data = await api(`/api/batches/${encodeURIComponent(code)}/samples`);
    $("#batch-samples-card").classList.remove("hidden");
    $("#batch-samples-title").textContent = `批次 ${code} 的样本`;
    $("#batch-samples-table").innerHTML = tableHtml(
      ["条码", "当前持有人", "链长", "登记时间"],
      data.samples.map((s) => `<tr>
        <td class="mono">${esc(s.barcode)}</td>
        <td>${esc(s.current_holder ?? "-")}</td>
        <td>${s.seq ?? "-"}</td>
        <td class="mono">${esc(s.created_at)}</td>
      </tr>`)
    );
  } catch (err) { toast(err.message, "error"); }
});

/* ---------------------------------------------------------------- 交接扫描 */

async function lookupSample() {
  const barcode = $("#ho-barcode").value.trim();
  if (!barcode) return;
  $("#ho-result").innerHTML = "";
  try {
    const s = await api(`/api/samples/${encodeURIComponent(barcode)}`);
    state.handoverKey = crypto.randomUUID();
    $("#ho-state").innerHTML =
      (s.frozen
        ? alertHtml("bad", `批次 ${s.batch_code} 已冻结（${s.open_events} 个待处置异常），交接已被阻止，需授权人员解除隔离。`)
        : alertHtml("ok", `批次 ${s.batch_code}（阈值 ${s.temp_min}~${s.temp_max}℃），当前链长 ${s.seq}`)) +
      `<p>当前持有人：<b>${esc(s.current_holder)}</b>　最近地点：${esc(s.last_location || "-")}　最近时间：<span class="mono">${esc(s.last_scanned_at || "-")}</span></p>`;
    $("#ho-from").value = s.current_holder || "";
    $("#ho-time").value = new Date().toISOString().slice(0, 16);
    $("#ho-form").classList.remove("hidden");
    $("#ho-to").focus();
  } catch (err) {
    $("#ho-state").innerHTML = alertHtml("bad", err.message);
    $("#ho-form").classList.add("hidden");
  }
}

$("#ho-lookup").addEventListener("click", lookupSample);
$("#ho-barcode").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); lookupSample(); } });

$("#ho-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const barcode = $("#ho-barcode").value.trim();
  const body = {
    barcode,
    from_holder: $("#ho-from").value.trim(),
    to_holder: $("#ho-to").value.trim(),
    location: $("#ho-location").value.trim(),
    scanned_at: $("#ho-time").value ? new Date($("#ho-time").value).toISOString() : null,
    idempotency_key: state.handoverKey,
  };
  try {
    const data = await api("/api/handovers", { method: "POST", body });
    const link = data.link;
    $("#ho-result").innerHTML = data.replay
      ? alertHtml("info", `幂等重放：该交接已登记（第 ${link.seq} 环），未产生重复链路。`)
      : alertHtml("ok", `交接成功：${esc(link.from_holder)} → ${esc(link.to_holder)}（第 ${link.seq} 环，哈希 ${shortHash(link.hash)}）`);
    if (!data.replay) {
      state.handoverKey = crypto.randomUUID(); // 成功后轮换幂等键
      $("#ho-to").value = "";
      lookupSampleQuiet(barcode);
      loadRecentHandovers();
    }
  } catch (err) {
    let msg = err.message;
    if (err.code === "HOLDER_MISMATCH") msg += "，请重新查询后再试";
    $("#ho-result").innerHTML = alertHtml("bad", msg);
    if (err.code === "HOLDER_MISMATCH" || err.code === "BATCH_FROZEN") lookupSampleQuiet(barcode);
  }
});

async function lookupSampleQuiet(barcode) {
  try {
    const s = await api(`/api/samples/${encodeURIComponent(barcode)}`);
    $("#ho-from").value = s.current_holder || "";
    state.handoverKey = state.handoverKey || crypto.randomUUID();
  } catch { /* 忽略 */ }
}

async function loadRecentHandovers() {
  try {
    const data = await api("/api/handovers/recent");
    $("#ho-recent").innerHTML = tableHtml(
      ["条码", "环节", "交出方", "接收方", "地点", "扫描时间", "哈希"],
      data.links.map((l) => `<tr>
        <td class="mono">${esc(l.barcode)}</td>
        <td>${l.seq}</td>
        <td>${esc(l.from_holder)}</td>
        <td>${esc(l.to_holder)}</td>
        <td>${esc(l.location)}</td>
        <td class="mono">${esc(l.scanned_at)}</td>
        <td class="mono" title="${esc(l.hash)}">${shortHash(l.hash)}</td>
      </tr>`)
    );
  } catch (err) { toast(err.message, "error"); }
}

/* ---------------------------------------------------------------- 温度导入 */

$("#temp-file").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => { $("#temp-csv").value = String(reader.result || ""); };
  reader.readAsText(file);
});

function parseCsv(text) {
  const readings = [];
  const errors = [];
  text.split("\n").forEach((line, i) => {
    const t = line.trim();
    if (!t) return;
    const parts = t.split(",").map((s) => s.trim());
    if (parts.length > 3) { errors.push(`第 ${i + 1} 行：列数过多`); return; }
    const temp = parseFloat(parts[1]);
    if (Number.isNaN(temp)) { errors.push(`第 ${i + 1} 行：温度无效`); return; }
    readings.push({ barcode: parts[0] || null, temp, recorded_at: parts[2] || null });
  });
  return { readings, errors };
}

$("#temp-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const batchCode = $("#temp-batch-select").value;
  const { readings, errors } = parseCsv($("#temp-csv").value);
  if (!batchCode) return;
  if (!readings.length) { $("#temp-result").innerHTML = alertHtml("warn", errors[0] || "没有可导入的记录"); return; }
  if (!state.importId) state.importId = crypto.randomUUID();
  try {
    const data = await api("/api/temperature/import", {
      method: "POST",
      body: { batch_code: batchCode, import_id: state.importId, readings },
    });
    state.importId = crypto.randomUUID();
    let html = alertHtml(
      data.events_generated ? "bad" : "ok",
      `导入完成：新增 ${data.inserted} 条，幂等跳过 ${data.skipped} 条` +
      (data.events_generated ? `；⚠️ 生成 ${data.events_generated} 个超限事件，批次已冻结，后续交接被阻止` : "")
    );
    (data.errors || []).concat(errors.map((m) => ({ reason: m }))).forEach((er) => {
      html += alertHtml("warn", er.line ? `第 ${er.line} 行（${er.barcode || "无条码"}）：${er.reason}` : er.reason);
    });
    $("#temp-result").innerHTML = html;
    loadReadings();
    loadEvents();
    loadBatches();
  } catch (err) { toast(err.message, "error"); }
});

async function loadReadings() {
  try {
    const data = await api("/api/temperature/readings");
    const thresholds = {};
    state.batches.forEach((b) => { thresholds[b.batch_code] = b; });
    $("#temp-table").innerHTML = tableHtml(
      ["批次", "条码", "温度 ℃", "记录时间", "导入批次号"],
      data.readings.map((r) => {
        const b = thresholds[r.batch_code];
        const oor = b && (r.temp < b.temp_min || r.temp > b.temp_max);
        return `<tr class="${oor ? "oor" : ""}">
          <td class="mono">${esc(r.batch_code)}</td>
          <td class="mono">${esc(r.barcode || "（批次级）")}</td>
          <td>${r.temp}${oor ? " ⚠️" : ""}</td>
          <td class="mono">${esc(r.recorded_at)}</td>
          <td class="mono">${esc(r.import_id.slice(0, 8))}…</td>
        </tr>`;
      })
    );
  } catch (err) { toast(err.message, "error"); }
}

/* ---------------------------------------------------------------- 异常处置 */

function openReasonModal(title, onOk) {
  $("#modal-title").textContent = title;
  $("#modal-reason").value = "";
  $("#modal").classList.remove("hidden");
  $("#modal-reason").focus();
  const ok = $("#modal-ok");
  const cancel = $("#modal-cancel");
  const cleanup = () => {
    $("#modal").classList.add("hidden");
    ok.onclick = null;
    cancel.onclick = null;
  };
  ok.onclick = () => {
    const reason = $("#modal-reason").value.trim();
    if (reason.length < 2) { toast("请填写理由（至少 2 个字符）", "error"); return; }
    cleanup();
    onOk(reason);
  };
  cancel.onclick = cleanup;
}

async function loadEvents() {
  try {
    const [open, history] = await Promise.all([
      api("/api/events?status=OPEN"),
      api("/api/events?status=ALL"),
    ]);
    const badge = $("#events-badge");
    if (open.events.length) {
      badge.textContent = open.events.length;
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
    const isSupervisor = state.user && state.user.role === "supervisor";
    $("#events-open-table").innerHTML = tableHtml(
      ["ID", "批次", "条码", "实测温度", "阈值", "记录时间", "上报人", "操作"],
      open.events.map((ev) => `<tr>
        <td>${ev.id}</td>
        <td class="mono">${esc(ev.batch_code)}</td>
        <td class="mono">${esc(ev.reading_barcode || "（批次级）")}</td>
        <td><b style="color:var(--bad)">${ev.reading_temp} ℃</b></td>
        <td>${ev.temp_min} ~ ${ev.temp_max} ℃</td>
        <td class="mono">${esc(ev.reading_recorded_at || "-")}</td>
        <td>${esc(ev.created_by)}</td>
        <td>${isSupervisor
          ? `<button class="btn btn-sm btn-primary" data-release="${ev.id}" type="button">解除隔离</button>
             <button class="btn btn-sm" data-correct="${ev.id}" type="button">纠正记录</button>`
          : '<span class="hint">需授权主管处置</span>'}</td>
      </tr>`),
      "没有待处置异常 🎉"
    );
    const historyRows = history.events
      .filter((ev) => ev.type !== "OUT_OF_RANGE" || ev.status !== "OPEN")
      .map((ev) => `<tr>
        <td>${ev.id}</td>
        <td><span class="tag tag-type">${esc({ OUT_OF_RANGE: "超限", RELEASE: "解除隔离", CORRECTION: "纠正" }[ev.type] || ev.type)}</span></td>
        <td class="mono">${esc(ev.batch_code)}</td>
        <td>${ev.type === "OUT_OF_RANGE" ? `<span class="tag tag-resolved">已解除</span>` : `<span class="tag tag-resolved">已记录</span>`}</td>
        <td>${esc(ev.reason || "-")}</td>
        <td>${esc(ev.created_by)}</td>
        <td class="mono">${esc(ev.created_at)}</td>
      </tr>`);
    $("#events-history-table").innerHTML = tableHtml(
      ["ID", "类型", "批次", "状态", "理由", "操作人", "时间"], historyRows
    );
  } catch (err) { toast(err.message, "error"); }
}

$("#events-open-table").addEventListener("click", (e) => {
  const rel = e.target.closest("[data-release]");
  const cor = e.target.closest("[data-correct]");
  if (rel) {
    openReasonModal(`解除隔离（事件 #${rel.dataset.release}）`, async (reason) => {
      try {
        const data = await api(`/api/events/${rel.dataset.release}/release`, { method: "POST", body: { reason } });
        toast(data.unfrozen ? "已解除隔离，批次恢复交接" : `已解除，仍有 ${data.open_events} 个异常待处置`, "ok");
        loadEvents(); loadBatches();
      } catch (err) { toast(err.message, "error"); loadEvents(); }
    });
  }
  if (cor) {
    openReasonModal(`登记纠正事件（事件 #${cor.dataset.correct}）`, async (reason) => {
      try {
        await api(`/api/events/${cor.dataset.correct}/corrections`, { method: "POST", body: { reason } });
        toast("纠正事件已记录", "ok");
        loadEvents();
      } catch (err) { toast(err.message, "error"); }
    });
  }
});

/* ---------------------------------------------------------------- 保管时间线 */

async function loadTimeline() {
  const barcode = $("#tl-barcode").value.trim();
  if (!barcode) return;
  $("#tl-verify-result").innerHTML = "";
  try {
    const data = await api(`/api/samples/${encodeURIComponent(barcode)}/timeline`);
    $("#tl-summary").innerHTML =
      `<p>条码 <b class="mono">${esc(data.barcode)}</b> ｜ 批次 <b>${esc(data.batch_code)}</b> ｜ ` +
      (data.frozen ? '<span class="tag tag-frozen">已冻结</span>' : '<span class="tag tag-active">正常</span>') +
      ` ｜ 共 ${data.links.length} 环</p>`;
    $("#tl-chain").innerHTML = data.links.map((l) => `
      <div class="link-card ${l.seq === 0 ? "genesis" : ""}">
        <div class="link-head">
          <span class="link-holders">#${l.seq} ${esc(l.from_holder)} → ${esc(l.to_holder)}</span>
          <span class="link-meta">${esc(l.location)} ｜ <span class="mono">${esc(l.scanned_at)}</span></span>
        </div>
        <div class="hash-line">hash: ${esc(l.hash)}</div>
        <div class="hash-line">prev: ${esc(l.prev_hash)}</div>
      </div>`).join("");
    $("#tl-verify").classList.remove("hidden");
    const evRows = data.events.map((ev) => `<tr>
      <td><span class="tag tag-type">${esc({ OUT_OF_RANGE: "超限", RELEASE: "解除隔离", CORRECTION: "纠正" }[ev.type] || ev.type)}</span></td>
      <td>${ev.status === "OPEN" ? '<span class="tag tag-open">待处置</span>' : '<span class="tag tag-resolved">已处置</span>'}</td>
      <td>${esc(ev.reading_temp != null && ev.type === "OUT_OF_RANGE" ? `${ev.reading_temp} ℃` : (ev.reason || "-"))}</td>
      <td>${esc(ev.created_by)}</td>
      <td class="mono">${esc(ev.created_at)}</td>
    </tr>`);
    $("#tl-events-title").classList.toggle("hidden", !evRows.length);
    const tbl = $("#tl-events");
    tbl.classList.toggle("hidden", !evRows.length);
    tbl.innerHTML = tableHtml(["类型", "状态", "详情/理由", "操作人", "时间"], evRows);
  } catch (err) {
    $("#tl-summary").innerHTML = alertHtml("bad", err.message);
    $("#tl-chain").innerHTML = "";
    $("#tl-verify").classList.add("hidden");
  }
}

$("#tl-lookup").addEventListener("click", loadTimeline);
$("#tl-barcode").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); loadTimeline(); } });
$("#tl-verify").addEventListener("click", async () => {
  const barcode = $("#tl-barcode").value.trim();
  try {
    const data = await api(`/api/samples/${encodeURIComponent(barcode)}/verify`);
    $("#tl-verify-result").innerHTML = alertHtml(data.valid ? "ok" : "bad",
      data.valid ? `✔ ${data.message}` : `✘ ${data.message}`);
  } catch (err) { toast(err.message, "error"); }
});

/* ---------------------------------------------------------------- 审计 */

async function loadAudit() {
  try {
    const data = await api("/api/audit?limit=200");
    $("#audit-table").innerHTML = tableHtml(
      ["#", "时间", "操作人", "动作", "对象", "内容", "哈希"],
      data.entries.map((r) => `<tr>
        <td>${r.seq}</td>
        <td class="mono">${esc(r.created_at)}</td>
        <td>${esc(r.actor)}</td>
        <td><span class="tag tag-type">${esc(r.action)}</span></td>
        <td class="mono">${esc(r.entity)}:${esc(r.entity_id)}</td>
        <td class="mono" title="${esc(r.payload)}">${esc(r.payload.length > 60 ? r.payload.slice(0, 60) + "…" : r.payload)}</td>
        <td class="mono" title="${esc(r.hash)}">${shortHash(r.hash)}</td>
      </tr>`)
    );
  } catch (err) { toast(err.message, "error"); }
}

$("#audit-refresh").addEventListener("click", loadAudit);
$("#audit-verify").addEventListener("click", async () => {
  try {
    const data = await api("/api/audit/verify");
    $("#audit-verify-result").innerHTML = alertHtml(data.valid ? "ok" : "bad",
      data.valid ? `✔ ${data.message}` : `✘ ${data.message}`);
  } catch (err) { toast(err.message, "error"); }
});

async function downloadAudit(format) {
  try {
    const res = await fetch(`/api/audit/export?format=${format}`, {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    if (!res.ok) throw new Error(`导出失败 (${res.status})`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `audit_log.${format}`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) { toast(err.message, "error"); }
}

$("#audit-export-csv").addEventListener("click", () => downloadAudit("csv"));
$("#audit-export-json").addEventListener("click", () => downloadAudit("json"));

/* ---------------------------------------------------------------- 启动 */

(async function boot() {
  if (!state.token || !state.user) { showLogin(); return; }
  try {
    const data = await api("/api/auth/me");
    state.user = data.user;
    localStorage.setItem("sc_user", JSON.stringify(data.user));
    showApp();
  } catch { showLogin(); }
})();
