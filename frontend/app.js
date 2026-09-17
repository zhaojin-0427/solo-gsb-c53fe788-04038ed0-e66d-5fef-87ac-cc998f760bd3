/* 样本链路与隔离处置台 — 前端逻辑（原生 JS） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  token: localStorage.getItem("token") || "",
  user: JSON.parse(localStorage.getItem("user") || "null"),
};

let currentSample = null;   // 交接页当前查询到的样本
let transferKey = "";       // 当前交接表单的幂等键
let timelineSampleId = null;

/* ---------------------------------------------------------------- 工具 */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function shortHash(h) { return h ? esc(h.slice(0, 12)) + "…" : ""; }

function newKey() {
  return crypto.randomUUID ? crypto.randomUUID()
    : "k-" + Date.now() + "-" + Math.random().toString(16).slice(2);
}

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch("/api" + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(state.token ? { Authorization: "Bearer " + state.token } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* 空响应 */ }
  if (res.status === 401) { showLogin(); throw new Error("未登录或会话过期"); }
  return { status: res.status, ok: res.ok, data };
}

function detailText(data) {
  const d = data && data.detail;
  if (!d) return "请求失败";
  if (typeof d === "string") return d;
  return d.message || JSON.stringify(d);
}

function resultBox(el, kind, html) {
  el.className = "result-box " + kind;
  el.innerHTML = html;
}

/* ---------------------------------------------------------------- 登录 */

function showLogin() {
  state.token = ""; state.user = null;
  localStorage.removeItem("token"); localStorage.removeItem("user");
  $("#login-view").classList.remove("hidden");
  $("#tabs").classList.add("hidden");
  $("#main").classList.add("hidden");
  $("#btn-logout").classList.add("hidden");
  $("#user-info").textContent = "";
}

function showApp() {
  $("#login-view").classList.add("hidden");
  $("#tabs").classList.remove("hidden");
  $("#main").classList.remove("hidden");
  $("#btn-logout").classList.remove("hidden");
  $("#user-info").textContent =
    `${state.user.username}（${{ staff: "员工", approver: "授权人员", admin: "管理员" }[state.user.role] || state.user.role}）`;
  loadBatches();
  refreshExcBadge();
}

$("#btn-login").addEventListener("click", async () => {
  $("#login-error").textContent = "";
  const res = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: $("#login-username").value.trim(),
      password: $("#login-password").value,
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { $("#login-error").textContent = data.detail || "登录失败"; return; }
  state.token = data.token;
  state.user = data.user;
  localStorage.setItem("token", data.token);
  localStorage.setItem("user", JSON.stringify(data.user));
  showApp();
});
$("#login-password").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#btn-login").click(); });

$("#btn-logout").addEventListener("click", async () => {
  try { await api("/logout", { method: "POST" }); } catch (e) { /* 忽略 */ }
  showLogin();
});

/* ---------------------------------------------------------------- 标签页 */

$$(".tab").forEach((btn) => btn.addEventListener("click", () => {
  $$(".tab").forEach((b) => b.classList.toggle("active", b === btn));
  $$(".view").forEach((v) => v.classList.add("hidden"));
  $("#view-" + btn.dataset.tab).classList.remove("hidden");
  if (btn.dataset.tab === "register") loadBatches();
  if (btn.dataset.tab === "exceptions") loadExceptions();
  if (btn.dataset.tab === "audit") loadAudit();
  if (btn.dataset.tab === "temps") loadBatchOptions($("#temp-batch"));
}));

/* ---------------------------------------------------------------- 批次与样本 */

async function loadBatches() {
  const res = await api("/batches");
  if (!res.ok) return;
  const tbody = $("#batch-table tbody");
  tbody.innerHTML = res.data.map((b) => `
    <tr>
      <td class="mono">${esc(b.code)}</td><td>${esc(b.name)}</td>
      <td>${b.temp_min} ~ ${b.temp_max} °C</td><td>${b.sample_count}</td>
      <td>${b.open_excursions > 0 ? `<span class="tag open">${b.open_excursions}</span>` : "0"}</td>
      <td>${esc(b.created_by)}</td><td class="mono">${esc(b.created_at)}</td>
    </tr>`).join("");
  loadBatchOptions($("#sample-batch"));
}

async function loadBatchOptions(select) {
  const res = await api("/batches");
  if (!res.ok) return;
  select.innerHTML = res.data.map((b) =>
    `<option value="${esc(b.code)}">${esc(b.code)} — ${esc(b.name)}（${b.temp_min}~${b.temp_max}°C）</option>`).join("");
}

$("#btn-create-batch").addEventListener("click", async () => {
  const res = await api("/batches", {
    method: "POST",
    body: {
      code: $("#batch-code").value.trim(),
      name: $("#batch-name").value.trim(),
      temp_min: parseFloat($("#batch-tmin").value),
      temp_max: parseFloat($("#batch-tmax").value),
    },
  });
  const el = $("#batch-result");
  if (res.ok) {
    el.className = "ok"; el.textContent = `批次 ${res.data.code} 创建成功`;
    loadBatches();
  } else { el.className = "error"; el.textContent = detailText(res.data); }
});

$("#btn-register-samples").addEventListener("click", async () => {
  const barcodes = $("#sample-barcodes").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const res = await api("/samples", {
    method: "POST",
    body: {
      batch_code: $("#sample-batch").value,
      barcodes,
      initial_holder: $("#sample-holder").value.trim(),
      location: $("#sample-location").value.trim(),
    },
  });
  const el = $("#sample-result");
  if (res.ok) {
    el.className = "ok";
    el.textContent = `新增 ${res.data.added.length} 个：${res.data.added.join("、") || "无"}` +
      (res.data.duplicates.length ? `；重复跳过 ${res.data.duplicates.length} 个：${res.data.duplicates.join("、")}` : "");
    loadBatches();
  } else { el.className = "error"; el.textContent = detailText(res.data); }
});

/* ---------------------------------------------------------------- 交接扫描 */

function resetTransferKey() {
  transferKey = newKey();
  $("#transfer-key").value = transferKey;
}

async function scanSample() {
  const barcode = $("#scan-barcode").value.trim();
  if (!barcode) return;
  const res = await api("/samples/by-barcode/" + encodeURIComponent(barcode));
  const panel = $("#transfer-panel");
  if (!res.ok) {
    panel.classList.add("hidden");
    alert(detailText(res.data));
    return;
  }
  currentSample = res.data;
  const s = currentSample.sample;
  const frozen = currentSample.frozen;
  $("#sample-info").innerHTML = `
    <p>
      条码 <b class="mono">${esc(s.barcode)}</b> ｜ 批次 <b>${esc(s.batch_code)}</b>（${esc(s.batch_name)}，阈值 ${s.temp_min}~${s.temp_max}°C）｜
      当前持有人 <b>${esc(currentSample.custody ? currentSample.custody.current_holder : "-")}</b> ｜
      状态 ${frozen ? `<span class="tag frozen">已冻结（${currentSample.open_excursions.length} 个未处置超限）</span>`
                     : '<span class="tag ok">正常</span>'}
    </p>`;
  $("#transfer-from").value = currentSample.custody ? currentSample.custody.current_holder : "";
  $("#transfer-to").value = "";
  $("#transfer-location").value = "";
  $("#transfer-result").innerHTML = "";
  resetTransferKey();
  panel.classList.remove("hidden");
  $("#transfer-to").focus();
}

$("#btn-scan").addEventListener("click", scanSample);
$("#scan-barcode").addEventListener("keydown", (e) => { if (e.key === "Enter") scanSample(); });

// 修改表单内容即视为新请求，重新生成幂等键；未修改的重试沿用原键
["transfer-from", "transfer-to", "transfer-location"].forEach((id) =>
  $("#" + id).addEventListener("input", resetTransferKey));

async function submitTransfer() {
  if (!currentSample) return;
  const res = await api("/transfers", {
    method: "POST",
    body: {
      barcode: currentSample.sample.barcode,
      expected_from_holder: $("#transfer-from").value.trim(),
      to_holder: $("#transfer-to").value.trim(),
      location: $("#transfer-location").value.trim(),
      idempotency_key: transferKey,
      scanned_at: new Date().toISOString(),
    },
  });
  const el = $("#transfer-result");
  if (res.ok) {
    const ev = res.data.event;
    if (res.data.replay) {
      resultBox(el, "info", `重复提交已被幂等去重：该请求已记录为第 ${ev.seq} 环（未产生双重链路）。`);
    } else {
      resultBox(el, "ok", `交接成功：第 ${ev.seq} 环，${esc(ev.from_holder)} → ${esc(ev.to_holder)} @ ${esc(ev.location)}，哈希 <span class="mono">${shortHash(ev.hash)}</span>`);
    }
    await scanSample();  // 刷新当前持有人与状态，并为下一次交接生成新幂等键
  } else {
    const d = res.data.detail || {};
    let html = `<b>交接被拒绝：</b>${esc(detailText(res.data))}`;
    if (d.expected !== undefined) html += `<br>期望前一持有人：${esc(d.expected)}；实际当前持有人：${esc(d.actual)}`;
    if (d.excursion_ids) html += `<br>未处置超限事件：${d.excursion_ids.join("、")}（请到「异常处置」处理）`;
    if (d.conflict_id) html += `<br>该冲突已记录（#${d.conflict_id}），刷新/重启后仍可追溯。`;
    resultBox(el, "err", html);
  }
}

$("#btn-transfer").addEventListener("click", submitTransfer);
$("#btn-transfer-retry").addEventListener("click", submitTransfer);

/* ---------------------------------------------------------------- 温度导入 */

$("#temp-file").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = () => { $("#temp-csv").value = reader.result; };
  reader.readAsText(f);
});

function parseCsv(text) {
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  const readings = [];
  lines.forEach((line, i) => {
    const parts = line.split(",").map((p) => p.trim());
    if (i === 0 && isNaN(parseFloat(parts[1]))) return; // 表头
    if (parts.length < 2) return;
    readings.push({ recorded_at: parts[0], temperature: parseFloat(parts[1]), barcode: parts[2] || null });
  });
  return readings;
}

$("#btn-import-temps").addEventListener("click", async () => {
  const readings = parseCsv($("#temp-csv").value);
  if (!readings.length) { alert("没有可导入的有效数据行"); return; }
  const res = await api("/temperature/import", {
    method: "POST",
    body: { batch_code: $("#temp-batch").value, readings, filename: $("#temp-file").files[0]?.name || null },
  });
  const el = $("#temp-result");
  if (res.ok) {
    const d = res.data;
    let html = `导入完成：共 ${d.total} 行，接受 ${d.accepted}，重复去重 ${d.duplicates}，拒绝 ${d.rejected}。`;
    if (d.excursions_created.length) {
      html += `<br><span class="warn">生成 ${d.excursions_created.length} 个超限事件（#${d.excursions_created.join("、#")}），相关样本已冻结后续交接。</span>`;
    }
    if (d.rejected_rows.length) {
      html += "<br>拒绝明细：" + d.rejected_rows.map((r) => `第${r.row}行（${esc(r.reason)}）`).join("；");
    }
    resultBox(el, d.excursions_created.length ? "info" : "ok", html);
    refreshExcBadge();
  } else {
    resultBox(el, "err", esc(detailText(res.data)));
  }
});

/* ---------------------------------------------------------------- 异常处置 */

async function refreshExcBadge() {
  try {
    const res = await api("/exceptions");
    if (!res.ok) return;
    const n = res.data.excursions.length + res.data.conflicts.length;
    const badge = $("#exc-badge");
    badge.textContent = n;
    badge.classList.toggle("hidden", n === 0);
  } catch (e) { /* 未登录时忽略 */ }
}

const EXC_STATUS = { open: "待处置", released: "已解除隔离", corrected: "已纠正" };
const CONFLICT_KIND = { holder_mismatch: "持有人不符", frozen: "冻结期交接", unknown_sample: "条码未登记" };

async function loadExceptions() {
  const all = $("#exc-show-all").checked;
  const res = await api("/exceptions" + (all ? "?all=true" : ""));
  if (!res.ok) return;
  const canDispose = ["approver", "admin"].includes(state.user.role);

  $("#exc-table tbody").innerHTML = res.data.excursions.map((e) => {
    const statusTag = e.status === "open"
      ? '<span class="tag open">待处置</span>' : `<span class="tag done">${EXC_STATUS[e.status]}</span>`;
    let disp = "-";
    if (e.status === "open" && canDispose) {
      disp = `<div class="disp-controls">
        <input id="reason-${e.id}" placeholder="处置理由（必填）">
        <button class="btn" onclick="dispose(${e.id}, 'release')">解除隔离</button>
        <button class="btn" onclick="dispose(${e.id}, 'correct')">纠正</button>
      </div>`;
    } else if (e.dispositions.length) {
      const d = e.dispositions[e.dispositions.length - 1];
      disp = `${EXC_STATUS[e.status]} by ${esc(d.actor)}<br><span class="hint">${esc(d.reason)}</span>`;
    }
    return `<tr>
      <td>${e.id}</td><td class="mono">${esc(e.batch_code)}</td><td class="mono">${esc(e.barcode || "（整批）")}</td>
      <td><b class="${e.temperature < e.temp_min || e.temperature > e.temp_max ? "error" : ""}">${e.temperature}°C</b></td>
      <td>${e.temp_min} ~ ${e.temp_max}°C</td><td class="mono">${esc(e.recorded_at)}</td><td>${statusTag}</td><td>${disp}</td>
    </tr>`;
  }).join("") || '<tr><td colspan="8" class="hint">暂无超限事件</td></tr>';

  $("#conflict-table tbody").innerHTML = res.data.conflicts.map((c) => {
    const statusTag = c.status === "open" ? '<span class="tag open">待处理</span>' : '<span class="tag done">已确认</span>';
    let action = "-";
    if (c.status === "open" && canDispose) {
      action = `<div class="disp-controls">
        <input id="note-${c.id}" placeholder="处理说明（可选）">
        <button class="btn" onclick="ackConflict(${c.id})">确认</button>
      </div>`;
    } else if (c.status !== "open") {
      action = `${esc(c.resolved_by || "")}<br><span class="hint">${esc(c.resolution_note || "")}</span>`;
    }
    return `<tr>
      <td>${c.id}</td><td class="mono">${esc(c.barcode)}</td><td>${CONFLICT_KIND[c.kind] || esc(c.kind)}</td>
      <td>${esc(c.expected_holder ?? "-")} / ${esc(c.actual_holder ?? "-")}</td>
      <td>${esc(c.attempted_to || "-")}</td><td>${esc(c.location || "-")}</td>
      <td>${esc(c.actor)}</td><td class="mono">${esc(c.created_at)}</td><td>${statusTag}</td><td>${action}</td>
    </tr>`;
  }).join("") || '<tr><td colspan="10" class="hint">暂无交接冲突</td></tr>';

  refreshExcBadge();
}

$("#exc-show-all").addEventListener("change", loadExceptions);

window.dispose = async (id, action) => {
  const reason = $("#reason-" + id).value.trim();
  if (!reason) { alert("必须填写处置理由"); return; }
  const res = await api(`/exceptions/${id}/dispositions`, { method: "POST", body: { action, reason } });
  if (!res.ok) alert(detailText(res.data));
  loadExceptions();
};

window.ackConflict = async (id) => {
  const note = $("#note-" + id).value.trim();
  const res = await api(`/conflicts/${id}/acknowledge`, { method: "POST", body: { note } });
  if (!res.ok) alert(detailText(res.data));
  loadExceptions();
};

/* ---------------------------------------------------------------- 时间线 */

async function loadTimeline() {
  const barcode = $("#timeline-barcode").value.trim();
  if (!barcode) return;
  const res = await api("/samples/by-barcode/" + encodeURIComponent(barcode));
  if (!res.ok) { alert(detailText(res.data)); return; }
  timelineSampleId = res.data.sample.id;
  const t = await api(`/samples/${timelineSampleId}/timeline`);
  $("#timeline-info").innerHTML = `
    <p>条码 <b class="mono">${esc(res.data.sample.barcode)}</b> ｜ 批次 ${esc(res.data.sample.batch_code)} ｜
    当前持有人 <b>${esc(res.data.custody ? res.data.custody.current_holder : "-")}</b> ｜ 共 ${t.data.length} 环</p>`;
  $("#timeline-table").classList.remove("hidden");
  $("#btn-verify").classList.remove("hidden");
  $("#verify-result").textContent = "";
  $("#timeline-table tbody").innerHTML = t.data.map((e) => `
    <tr>
      <td>${e.seq}</td><td>${esc(e.from_holder)}</td><td>${esc(e.to_holder)}</td>
      <td>${esc(e.location)}</td><td class="mono">${esc(e.scanned_at)}</td>
      <td>${esc(e.actor)}</td><td class="mono" title="${esc(e.hash)}">${shortHash(e.hash)}</td>
    </tr>`).join("");
}

$("#btn-timeline").addEventListener("click", loadTimeline);
$("#timeline-barcode").addEventListener("keydown", (e) => { if (e.key === "Enter") loadTimeline(); });

$("#btn-verify").addEventListener("click", async () => {
  if (!timelineSampleId) return;
  const res = await api(`/samples/${timelineSampleId}/verify`);
  const el = $("#verify-result");
  if (res.ok && res.data.ok) {
    el.className = "ok";
    el.textContent = `✓ 链完整：已校验 ${res.data.checked} 个事件，链头哈希 ${res.data.head.slice(0, 16)}…`;
  } else {
    el.className = "error";
    el.textContent = `✗ 校验失败：${res.data.reason || "未知错误"}${res.data.broken_at ? "（第 " + res.data.broken_at + " 环）" : ""}`;
  }
});

/* ---------------------------------------------------------------- 审计 */

async function loadAudit() {
  const action = $("#audit-action").value.trim();
  const res = await api("/audit?limit=300" + (action ? "&action=" + encodeURIComponent(action) : ""));
  if (!res.ok) return;
  $("#audit-table tbody").innerHTML = res.data.map((r) => `
    <tr>
      <td>${r.id}</td><td class="mono">${esc(r.ts)}</td><td>${esc(r.actor)}</td>
      <td class="mono">${esc(r.action)}</td><td>${esc(r.entity)}#${esc(r.entity_id)}</td>
      <td class="mono">${esc(r.detail)}</td>
      <td class="mono" title="${esc(r.hash)}">${shortHash(r.hash)}</td>
    </tr>`).join("") || '<tr><td colspan="7" class="hint">暂无审计记录</td></tr>';
}

$("#btn-audit-refresh").addEventListener("click", loadAudit);

$("#btn-audit-verify").addEventListener("click", async () => {
  const res = await api("/audit/verify");
  const el = $("#audit-verify-result");
  if (res.ok && res.data.ok) {
    el.className = "ok";
    el.textContent = `✓ 审计链完整：已校验 ${res.data.checked} 条记录`;
  } else {
    el.className = "error";
    el.textContent = `✗ 审计链校验失败：${res.data.reason || ""}`;
  }
});

async function downloadAudit(format) {
  const res = await fetch("/api/audit/export?format=" + format, {
    headers: { Authorization: "Bearer " + state.token },
  });
  const blob = await res.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "audit_export." + format;
  a.click();
  URL.revokeObjectURL(a.href);
}

$("#btn-audit-csv").addEventListener("click", () => downloadAudit("csv"));
$("#btn-audit-json").addEventListener("click", () => downloadAudit("json"));

/* ---------------------------------------------------------------- 启动 */

if (state.token && state.user) {
  api("/me").then((res) => { if (res.ok) showApp(); else showLogin(); }).catch(() => showLogin());
} else {
  showLogin();
}
