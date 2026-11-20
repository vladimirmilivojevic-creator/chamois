"use strict";

// CHAMOIS operator console: plain JavaScript on top of the public API.
const WORKSPACE = "default";
const RESOURCE = "chamois";
const FINAL = new Set(["finished", "error", "cancelled"]);
const COLORS = { cool: "var(--cool)", r: "var(--r)", ms: "var(--ms)", ms_drive: "var(--drive)", detect: "var(--detect)" };
const KIND_NAMES = { cool: "cooling", r: "R pulse", ms: "MS leg", ms_drive: "MS drive", detect: "detection" };

const state = {
  token: null,
  user: null,
  presets: [],
  jobs: [],
  selected: null, // job id or "preview"
  circuit: 0,
  schedules: {}, // `${jobId}:${circuit}` -> compile result
  result: null,
  preview: null,
  calibration: null,
  timer: null,
};

const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const us = (ns) => (ns / 1000).toLocaleString("en-US", { maximumFractionDigits: 3 });

function storage(action, value) {
  try {
    if (action === "get") return localStorage.getItem("chamois-token");
    if (action === "set") localStorage.setItem("chamois-token", value);
    if (action === "clear") localStorage.removeItem("chamois-token");
  } catch {
    return null; // private mode: the console still works, it just forgets the role
  }
  return null;
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json" };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...options, headers });
  if (response.status === 204 || response.status === 208) return null;
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    const message = Array.isArray(detail)
      ? detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ")
      : detail || response.statusText;
    throw new Error(`${response.status} ${message}`);
  }
  return body;
}

// ---------- health & login ----------
async function checkHealth() {
  const pill = $("health");
  try {
    const health = await api("/health");
    const scheduler = health.scheduler === "rust" ? `Rust scheduler ${health.scheduler_version}` : "Python reference scheduler";
    pill.textContent = `API online · ${scheduler}`;
    pill.className = "pill pill-ok";
  } catch {
    pill.textContent = "API waking up...";
    pill.className = "pill pill-wait";
    setTimeout(checkHealth, 3000);
  }
}

async function login(token) {
  state.token = token;
  try {
    state.user = await api("/api/v1/me");
  } catch {
    logout();
    return;
  }
  storage("set", token);
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  $("who").textContent = state.user.name;
  $("role").textContent = state.user.role;
  state.schedules = {};
  state.selected = null;
  await Promise.all([loadPresets(), loadCalibration()]);
  refreshJobs();
}

function logout() {
  state.token = null;
  state.user = null;
  storage("clear");
  clearTimeout(state.timer);
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
}

// ---------- submit ----------
async function loadPresets() {
  if (!state.presets.length) state.presets = await (await fetch("/static/presets.json")).json();
  $("presets").innerHTML = state.presets
    .map((p) => `<button class="btn preset" data-preset="${esc(p.id)}"><strong>${esc(p.title)}</strong><span>${esc(p.hint)}</span></button>`)
    .join("");
  choosePreset(state.presets[0].id);
}

function choosePreset(id) {
  const preset = state.presets.find((p) => p.id === id);
  document.querySelectorAll(".preset").forEach((b) => b.classList.toggle("active", b.dataset.preset === id));
  $("label").value = preset.job.label;
  $("payload").value = JSON.stringify(preset.job.payload, null, 1);
}

function readPayload() {
  try {
    return JSON.parse($("payload").value);
  } catch (error) {
    throw new Error(`payload is not valid JSON: ${error.message}`);
  }
}

async function submitJob() {
  const msg = $("submit-msg");
  try {
    const body = { job_type: "quantum_circuit", label: $("label").value || null, payload: readPayload() };
    const response = await api(`/api/v1/submit/${WORKSPACE}/${RESOURCE}`, { method: "POST", body: JSON.stringify(body) });
    msg.textContent = `queued ${response.job.job_id.slice(0, 8)}`;
    msg.className = "small pass";
    selectJob(response.job.job_id);
    refreshJobs();
  } catch (error) {
    msg.textContent = error.message;
    msg.className = "small fail";
  }
}

async function previewSchedule() {
  const msg = $("submit-msg");
  try {
    const circuit = readPayload().circuits[0];
    state.preview = await api("/api/v1/schedule", { method: "POST", body: JSON.stringify(circuit) });
    state.selected = "preview";
    state.result = null;
    msg.textContent = state.preview.ok ? "schedule is valid" : "schedule rejected";
    msg.className = state.preview.ok ? "small pass" : "small fail";
    renderJobs();
    renderDetail();
  } catch (error) {
    msg.textContent = error.message;
    msg.className = "small fail";
  }
}

// ---------- queue ----------
async function refreshJobs() {
  clearTimeout(state.timer);
  if (!state.token) return;
  try {
    state.jobs = await api("/api/v1/jobs");
    renderJobs();
    if (state.selected && state.selected !== "preview") await loadDetail();
  } catch (error) {
    $("queue-info").textContent = error.message;
  }
  const busy = state.jobs.some((j) => !FINAL.has(j.status));
  state.timer = setTimeout(refreshJobs, busy ? 800 : 4000);
}

function renderJobs() {
  const rows = state.jobs.map((job) => {
    const pct = Math.round((100 * job.finished_count) / job.circuits);
    const cancel = FINAL.has(job.status) ? "" : `<button class="btn small danger" data-cancel="${job.job_id}">Cancel</button>`;
    return `<tr class="job ${job.job_id === state.selected ? "selected" : ""}" data-job="${job.job_id}">
      <td class="mono">${job.job_id.slice(0, 8)}</td>
      <td>${esc(job.label || "")}${job.source === "direct" ? ' <span class="chip">qiskit</span>' : ""}</td>
      <td><span class="chip status-${job.status}">${job.status}</span></td>
      <td><div class="progress" title="${job.finished_count}/${job.circuits} circuits"><div style="width:${pct}%"></div></div></td>
      <td class="small">${esc(job.owner)}</td><td>${cancel}</td></tr>`;
  });
  $("jobs").innerHTML = rows.join("") || '<tr><td colspan="6" class="muted">No jobs yet. Pick a preset and submit it.</td></tr>';
  const queued = state.jobs.filter((j) => j.status === "queued").length;
  $("queue-info").textContent = `${state.jobs.length} job${state.jobs.length === 1 ? "" : "s"} · ${queued} queued`;
}

function selectJob(id) {
  state.selected = id;
  state.circuit = 0;
  state.result = null;
  renderJobs();
  loadDetail();
}

async function cancelJob(id) {
  await api(`/api/v1/jobs/${id}`, { method: "DELETE" }).catch(() => null);
  refreshJobs();
}

// ---------- detail ----------
async function loadDetail() {
  const id = state.selected;
  if (!id || id === "preview") return;
  const result = await api(`/api/v1/result/${id}?include_timing_data=true`).catch(() => null);
  if (state.selected !== id || !result) return;
  state.result = result;
  const job = state.jobs.find((j) => j.job_id === id);
  const key = `${id}:${state.circuit}`;
  if (!state.schedules[key]) {
    const schedule = await api(`/api/v1/jobs/${id}/schedule?circuit=${state.circuit}`).catch(() => null);
    if (schedule) state.schedules[key] = schedule;
  }
  if (job) renderCircuitSelect(job.circuits);
  renderDetail();
}

function renderCircuitSelect(count) {
  const select = $("circuit-select");
  select.classList.toggle("hidden", count < 2);
  if (select.options.length !== count) {
    select.innerHTML = Array.from({ length: count }, (_, i) => `<option value="${i}">circuit ${i}</option>`).join("");
  }
  select.value = String(state.circuit);
}

function renderDetail() {
  const preview = state.selected === "preview";
  const schedule = preview ? state.preview : state.schedules[`${state.selected}:${state.circuit}`];
  const response = state.result?.response;
  $("detail-title").textContent = preview ? "· preview (not executed)" : state.result ? `· ${state.result.job.label || ""} ${state.selected.slice(0, 8)}` : "";
  $("steps").innerHTML = (response?.timing_data || [])
    .map((s) => `<span class="step"><span class="chip status-${s.new_status}">${s.new_status}</span> ${new Date(s.timestamp).toLocaleTimeString()}</span>`)
    .join('<span class="step">&rarr;</span>');

  const problems = [];
  if (response?.status === "error") problems.push(`<div class="violation"><strong>Job error:</strong> ${esc(response.message)}</div>`);
  else (schedule?.violations || []).forEach((v) => problems.push(`<div class="violation"><code>${esc(v.code)}</code> ${esc(v.message)}</div>`));
  $("violations").innerHTML = problems.join("");

  $("timeline").innerHTML = schedule?.timeline
    ? timelineSvg(schedule.timeline)
    : response && !FINAL.has(response.status)
      ? '<p class="muted">Waiting for the scheduler...</p>'
      : "";
  $("histogram").innerHTML = response?.status === "finished" ? histogram(response.result[String(state.circuit)]) : "";
}

function channelOrder(channel) {
  if (channel === "cool") return [0, 0];
  if (channel.startsWith("addr_q")) return [1, Number(channel.slice(6))];
  if (channel === "ms_drive") return [2, 0];
  return [3, 0];
}

function niceStep(span) {
  const raw = span / 7;
  const pow = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 5, 10].map((m) => m * pow).find((s) => s >= raw);
}

function timelineSvg(timeline) {
  const full = $("full-shot").checked;
  const gates = timeline.pulses.filter((p) => p.kind !== "cool" && p.kind !== "detect");
  let t0 = 0;
  let t1 = timeline.duration_ns;
  if (!full && gates.length) {
    t0 = Math.min(...gates.map((p) => p.start_ns));
    t1 = Math.max(...gates.map((p) => p.start_ns + p.duration_ns));
    const pad = (t1 - t0) * 0.04;
    t0 -= pad;
    t1 += pad;
  }
  const channels = [...new Set(timeline.pulses.map((p) => p.channel))].sort((a, b) => {
    const [x, y] = [channelOrder(a), channelOrder(b)];
    return x[0] - y[0] || x[1] - y[1];
  });
  const width = Math.max($("timeline").clientWidth || 900, 720);
  const left = 78;
  const right = 12;
  const rowH = 24;
  const top = 8;
  const height = top + channels.length * rowH + 28;
  const x = (t) => left + ((t - t0) / (t1 - t0)) * (width - left - right);
  const parts = [`<svg width="${width}" height="${height}" role="img" aria-label="pulse timeline"><defs><clipPath id="plot"><rect x="${left}" y="0" width="${width - left - right}" height="${height}"/></clipPath></defs>`];

  const step = niceStep((t1 - t0) / 1000) * 1000;
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
    parts.push(`<line class="axis" x1="${x(t)}" x2="${x(t)}" y1="${top}" y2="${height - 22}"/>`);
    parts.push(`<text x="${x(t)}" y="${height - 8}" text-anchor="middle">${us(t)} µs</text>`);
  }
  const budget = state.calibration?.max_sequence_ns;
  if (budget && budget >= t0 && budget <= t1) {
    parts.push(`<line x1="${x(budget)}" x2="${x(budget)}" y1="${top}" y2="${height - 22}" stroke="var(--bad)" stroke-width="2" stroke-dasharray="5 4"><title>coherence budget ${us(budget)} µs</title></line>`);
    parts.push(`<text x="${x(budget) - 4}" y="${top + 10}" text-anchor="end" style="fill:var(--bad)">coherence budget</text>`);
  }
  channels.forEach((channel, row) => {
    const y = top + row * rowH;
    parts.push(`<text class="label" x="4" y="${y + 16}">${channel}</text>`);
    timeline.pulses
      .filter((p) => p.channel === channel)
      .forEach((p) => {
        const x0 = x(p.start_ns);
        const w = Math.max(x(p.start_ns + p.duration_ns) - x0, 1.5);
        const phase = p.kind === "r" || p.kind === "ms" ? `, phase ${p.phase.toFixed(3)}π` : "";
        const gate = p.gate === null ? "" : ` (op ${p.gate})`;
        const tip = `${KIND_NAMES[p.kind]}${gate}: ${us(p.duration_ns)} µs at ${us(p.start_ns)} µs${phase}`;
        parts.push(`<rect clip-path="url(#plot)" x="${x0}" y="${y + 4}" width="${w}" height="${rowH - 8}" rx="3" fill="${COLORS[p.kind]}"><title>${tip}</title></rect>`);
      });
  });
  parts.push("</svg>");
  const legend = Object.entries(KIND_NAMES)
    .map(([kind, name]) => `<span><i style="background:${COLORS[kind]}"></i>${name}</span>`)
    .join("");
  const summary = `${timeline.pulses.length} pulses on ${channels.length} channels · one shot ${us(timeline.duration_ns)} µs · 8 ns RTIO grid · hover a pulse for details`;
  return `${parts.join("")}<div class="legend">${legend}<span>${summary}</span></div>`;
}

function histogram(shots) {
  if (!shots?.length) return "";
  const counts = {};
  shots.forEach((bits) => {
    const key = bits.join("");
    counts[key] = (counts[key] || 0) + 1;
  });
  const top = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 12);
  const max = top[0][1];
  const rows = top
    .map(([key, n]) => `<div class="bar-row"><span class="mono">${key}</span><div class="bar" style="width:${(100 * n) / max}%"></div><span>${((100 * n) / shots.length).toFixed(1)}%</span></div>`)
    .join("");
  const distinct = Object.keys(counts).length;
  return `<h3>Measured outcomes (${shots.length} shots, ${distinct} distinct; bit order q0 q1 ...)</h3><div class="bars">${rows}</div>`;
}

// ---------- calibration ----------
async function loadCalibration() {
  state.calibration = await api("/api/v1/calibration");
  renderCalibration();
}

const PARAMS = [
  ["ms_gate_ns", "MS gate (µs)", 1000],
  ["max_sequence_ns", "Coherence budget (µs)", 1000],
  ["cooling_ns", "Cooling (µs)", 1000],
  ["detection_ns", "Detection (µs)", 1000],
  ["settle_ns", "AOM settle (ns)", 1],
  ["clock_ns", "RTIO clock (ns)", 1],
];

function renderCalibration() {
  const cal = state.calibration;
  const operator = state.user.role === "operator";
  const disabled = operator ? "" : "disabled";
  $("cal-meta").textContent = `v${cal.version} · ${cal.updated_by} · ${new Date(cal.updated_at).toLocaleString()}`;
  $("cal-params").innerHTML = PARAMS.map(
    ([key, label, scale]) => `<label>${label}<input type="number" step="any" data-param="${key}" value="${cal[key] / scale}" ${disabled}></label>`,
  ).join("");
  $("cal-qubits").innerHTML = cal.qubits
    .map(
      (q, i) => `<tr><td>q${i}</td><td><input type="number" step="0.001" data-pi="${i}" value="${q.pi_time_ns / 1000}" ${disabled}></td>
      <td><input type="checkbox" data-enabled="${i}" ${q.enabled ? "checked" : ""} ${disabled}></td></tr>`,
    )
    .join("");
  $("cal-save").disabled = !operator;
  $("cal-msg").textContent = operator ? "Changes apply to jobs that start after saving." : "Read-only: log in as Operator to edit.";
  $("cal-msg").className = "small muted";
}

async function saveCalibration() {
  const cal = structuredClone(state.calibration);
  PARAMS.forEach(([key, , scale]) => {
    const value = Number(document.querySelector(`[data-param="${key}"]`).value) * scale;
    cal[key] = key.endsWith("_ns") && key !== "ms_gate_ns" ? Math.round(value) : value;
  });
  cal.qubits.forEach((q, i) => {
    q.pi_time_ns = Number(document.querySelector(`[data-pi="${i}"]`).value) * 1000;
    q.enabled = document.querySelector(`[data-enabled="${i}"]`).checked;
  });
  try {
    state.calibration = await api("/api/v1/calibration", { method: "PUT", body: JSON.stringify(cal) });
    renderCalibration();
    $("cal-msg").textContent = `saved as v${state.calibration.version}`;
    $("cal-msg").className = "small pass";
  } catch (error) {
    $("cal-msg").textContent = error.message;
    $("cal-msg").className = "small fail";
  }
}

// ---------- cross-check ----------
async function runCrosscheck() {
  const button = $("crosscheck");
  button.disabled = true;
  $("cc-msg").textContent = "running (the first run loads Qiskit, ~10 s)...";
  $("cc-msg").className = "small muted";
  try {
    const rows = await api("/api/v1/crosscheck");
    $("cc-table").innerHTML = `<div class="table-wrap"><table><thead><tr><th>Circuit</th><th>Pulses</th><th>Shot</th><th>TVD vs statevector</th><th>TVD vs AQT offline</th><th></th></tr></thead><tbody>${rows
      .map(
        (r) => `<tr><td>${esc(r.circuit)}</td><td>${r.pulses}</td><td>${r.shot_duration_us.toFixed(1)} µs</td>
        <td class="mono">${r.tvd_exact.toFixed(6)}</td><td class="mono">${r.tvd_sampled.toFixed(4)}</td>
        <td class="${r.passed ? "pass" : "fail"}">${r.passed ? "pass" : "fail"}</td></tr>`,
      )
      .join("")}</tbody></table></div>`;
    $("cc-msg").textContent = "TVD = total variation distance (0 = identical distributions).";
  } catch (error) {
    $("cc-msg").textContent = error.message;
    $("cc-msg").className = "small fail";
  } finally {
    button.disabled = false;
  }
}

// ---------- events ----------
document.addEventListener("click", (event) => {
  const target = event.target.closest("button, tr.job");
  if (!target) return;
  if (target.dataset.login) login(target.dataset.login);
  else if (target.dataset.preset) choosePreset(target.dataset.preset);
  else if (target.dataset.cancel) {
    event.stopPropagation();
    cancelJob(target.dataset.cancel);
  } else if (target.dataset.job) selectJob(target.dataset.job);
});
$("submit").addEventListener("click", submitJob);
$("preview").addEventListener("click", previewSchedule);
$("switch-role").addEventListener("click", logout);
$("cal-save").addEventListener("click", saveCalibration);
$("crosscheck").addEventListener("click", runCrosscheck);
$("full-shot").addEventListener("change", renderDetail);
$("circuit-select").addEventListener("change", (event) => {
  state.circuit = Number(event.target.value);
  loadDetail();
});
window.addEventListener("resize", () => state.selected && renderDetail());

checkHealth();
const saved = storage("get");
if (saved) login(saved);
else $("login").classList.remove("hidden");
