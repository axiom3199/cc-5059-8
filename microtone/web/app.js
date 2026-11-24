// 微分音编曲试听界面前端。
// 铁律：前端不自行计算音高、不做半音量化；
// 每个发声频率都取自后端 /api/note-on、/api/gliss、/api/render-score 的返回值。

const $ = (id) => document.getElementById(id);
let audioCtx = null;
const liveOsc = new Map();   // degree -> {osc, gain}
let currentTuning = null;
let tuningCache = {};

const SAMPLE_SCORES = {
  "纯律合唱": `bpm 100
tuning just
policy hold_ratio
chord 0,2,4 3.0
rest 0.5
note 0 1.0
note 1 1.0
note 2 1.0
note 4 1.5
rest 0.5
chord 4,6,8 2.0
rest 0.25
chord 0,2,4 2.5`,
  "四分音与拍频": `bpm 120
tuning 24-TET
policy hold_freq
note 0 0.75
note 1 0.75
note 2 0.75
note 3 0.75
note 4 0.75
rest 0.25
chord 0,1 3.0
rest 0.5
gliss 0 6 3.0 steps 24
rest 0.5
note 12 1.5`,
  "Bohlen-Pierce 非八度": `bpm 110
tuning bohlen-pierce
policy hold_ratio
note 0 1.0
note 2 1.0
note 4 1.0
note 6 1.0
rest 0.5
chord 0,4,10 3.0
rest 0.5
gliss 0 13 3.0 steps 26
note 13 1.5`,
  "调律对比通用谱": `bpm 96
tuning 12-TET
policy hold_ratio
chord 0,4,7 3.0
rest 0.5
note 0 1.0
note 2 1.0
note 4 1.0
note 7 1.5
rest 0.5
chord 0,2,4 2.5
gliss 0 7 2.5 steps 12`,
};

async function api(path, payload) {
  const opt = payload === undefined
    ? {}
    : { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload) };
  const resp = await fetch(path, opt);
  const data = await resp.json();
  if (data.error) throw new Error(data.error);
  return data;
}

function ensureAudio() {
  if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  if (audioCtx.state === "suspended") audioCtx.resume();
  return audioCtx;
}

// 简单可验证音色：基波 + 两个谐波，与后端渲染器保持一致
function startOscillator(freq, velocity) {
  const ctx = ensureAudio();
  const master = ctx.createGain();
  const t = ctx.currentTime;
  master.gain.setValueAtTime(0.0001, t);
  master.gain.exponentialRampToValueAtTime(0.22 * velocity, t + 0.01);
  master.connect(ctx.destination);
  const partials = [[1, 1.0], [2, 0.35], [3, 0.12]];
  const nodes = partials.map(([h, amp]) => {
    const o = ctx.createOscillator();
    o.type = "sine";
    o.frequency.value = freq * h;   // 直接使用后端给定的真实频率
    const g = ctx.createGain();
    g.gain.value = amp / h;
    o.connect(g).connect(master);
    o.start();
    return o;
  });
  return { nodes, master };
}

function stopOscillator(voice) {
  const ctx = audioCtx;
  const t = ctx.currentTime;
  voice.master.gain.cancelScheduledValues(t);
  voice.master.gain.setValueAtTime(voice.master.gain.value, t);
  voice.master.gain.exponentialRampToValueAtTime(0.0001, t + 0.12);
  voice.nodes.forEach((o) => o.stop(t + 0.13));
}

// ---- 调律编辑 -------------------------------------------------------------

async function loadTunings() {
  tuningCache = await api("/api/tunings");
  const sel = $("tuning-select");
  const selB = $("tuning-b");
  sel.innerHTML = "";
  selB.innerHTML = "";
  for (const name of Object.keys(tuningCache)) {
    sel.add(new Option(name, name));
    selB.add(new Option(name, name));
  }
  selB.value = "19-TET";
  await selectTuning("12-TET");
}

async function selectTuning(name) {
  const data = await api("/api/select-tuning", { name });
  currentTuning = data.tuning;
  renderTuningEditor(currentTuning);
  buildKeyboard();
  if (data.voice_continuation && data.voice_continuation.length) {
    showContinuation(data.voice_continuation);
  }
}

function renderTuningEditor(t) {
  $("t-name").value = t.name;
  $("t-ref").value = t.ref_freq;
  $("t-refdeg").value = t.ref_degree;
  $("t-steps").value = t.period_steps;
  $("t-period").value = t.period_ratio;
  const se = $("scale-editor");
  const le = $("label-editor");
  se.innerHTML = "";
  le.innerHTML = "";
  t.scale.forEach((r, i) => {
    se.appendChild(makeRow(i, "scale", r));
    le.appendChild(makeRow(i, "label", t.labels[i] ?? `deg${i}`));
  });
}

function makeRow(i, kind, val) {
  const div = document.createElement("div");
  div.className = "edit-row";
  const span = document.createElement("span");
  span.textContent = `级 ${i}`;
  const inp = document.createElement("input");
  inp.dataset.kind = kind;
  inp.dataset.index = i;
  inp.value = val;
  div.append(span, inp);
  return div;
}

function collectTuningForm() {
  const scale = [...document.querySelectorAll('#scale-editor input')]
    .map((i) => i.value.trim());
  const labels = [...document.querySelectorAll('#label-editor input')]
    .map((i) => i.value.trim());
  return {
    name: $("t-name").value.trim() || "custom",
    ref_freq: parseFloat($("t-ref").value),
    ref_degree: parseInt($("t-refdeg").value, 10),
    period_steps: parseInt($("t-steps").value, 10),
    period_ratio: $("t-period").value.trim(),
    scale,
    labels,
  };
}

function showContinuation(report) {
  const box = $("continuation");
  box.classList.remove("hidden");
  box.innerHTML = "<h3>调律切换后已发声音的续接记录</h3>" + report.map((r) =>
    `<div>级数 ${r.degree} · <b>${r.policy}</b> · ` +
    `${r.old_tuning} ${r.old_freq_hz.toFixed(3)}Hz → ` +
    `${r.new_tuning} ${r.new_freq_hz.toFixed(3)}Hz ` +
    `(Δ ${r.freq_delta_hz >= 0 ? "+" : ""}${r.freq_delta_hz.toFixed(3)}Hz)</div>`
  ).join("");
}

// ---- 琴键 -----------------------------------------------------------------

async function buildKeyboard() {
  const lo = parseInt($("key-lo").value, 10);
  const hi = parseInt($("key-hi").value, 10);
  const data = await api(
    `/api/table?name=${encodeURIComponent($("tuning-select").value)}&lo=${lo}&hi=${hi}`);
  const kb = $("keyboard");
  kb.innerHTML = "";
  for (const row of data.freq_table) {
    const btn = document.createElement("button");
    btn.className = "key";
    btn.innerHTML =
      `<span class="k-label">${row.label}</span>` +
      `<span class="k-deg">deg ${row.degree}</span>` +
      `<span class="k-freq">${row.freq_hz.toFixed(2)}Hz</span>` +
      `<span class="k-ratio">${row.ratio}</span>`;
    btn.onmousedown = () => pressKey(row.degree, btn);
    btn.onmouseup = btn.onmouseleave = () => releaseKey(row.degree, btn);
    btn.dataset.degree = row.degree;
    kb.appendChild(btn);
  }
}

async function pressKey(degree, btn) {
  const data = await api("/api/note-on", { degree, velocity: 1.0 });
  if (data.muted) {
    logKey(`级数 ${degree} 频率 ${data.freq_hz.toFixed(2)}Hz 超出有效范围，已静音`);
    return;
  }
  if (liveOsc.has(degree)) return;
  const voice = startOscillator(data.freq_hz, 1.0);
  liveOsc.set(degree, voice);
  btn.classList.add("active");
  logKey(`按下 级数 ${degree}（${data.label}）真实频率 ` +
    `<b>${data.freq_hz.toFixed(4)}Hz</b> 比例 ${data.ratio}`);
}

async function releaseKey(degree, btn) {
  const voice = liveOsc.get(degree);
  if (!voice) return;
  stopOscillator(voice);
  liveOsc.delete(degree);
  btn.classList.remove("active");
  await api("/api/note-off", { degree });
}

function logKey(html) {
  const div = document.createElement("div");
  div.innerHTML = html;
  $("key-log").prepend(div);
}

async function playGlissando() {
  const d0 = parseInt($("gliss-target").value, 10) - 4; // 从目标前四级起滑
  const d1 = parseInt($("gliss-target").value, 10);
  const dur = parseFloat($("gliss-dur").value);
  const data = await api("/api/gliss",
    { degree_from: d0, degree_to: d1, duration: dur });
  if (!data.ok) {
    logKey("滑音被拒绝（超出有效范围）");
    return;
  }
  // 前端按后端给定的起止频率做指数滑音（对数频率轴），不经半音量化
  const ctx = ensureAudio();
  const voice = startOscillator(data.freq_from, 1.0);
  const t0 = ctx.currentTime + 0.05;
  voice.nodes.forEach((o, idx) => {
    const h = idx + 1;
    o.frequency.cancelScheduledValues(t0);
    o.frequency.setValueAtTime(data.freq_from * h, t0);
    o.frequency.exponentialRampToValueAtTime(data.freq_to * h, t0 + dur * 0.7);
  });
  logKey(`滑音 deg ${d0}→${d1}：${data.freq_from.toFixed(3)}Hz → ` +
    `${data.freq_to.toFixed(3)}Hz，末端比例 ${data.ratio_to}`);
  setTimeout(() => stopOscillator(voice), dur * 1000 + 200);
}

// ---- 谱面渲染与结果 -------------------------------------------------------

async function renderScore() {
  const scoreText = $("score-text").value;
  const tuning = $("tuning-select").value;
  const data = await api("/api/render-score",
    { score_text: scoreText, tuning });
  showRenderResult(data, tuning);
}

function showRenderResult(data, tuningName) {
  const rows = data.mapping.map((m) => {
    const check = data.checks.find((c) => c.degree === m.degree
      && Math.abs(c.expected_hz - m.freq_hz) < 1e-9);
    const est = check ? check.estimated_hz.toFixed(3) : "—";
    const err = check ? `${check.error_cents >= 0 ? "+" : ""}${check.error_cents.toFixed(2)}c` : "—";
    const flag = check ? (check.pass ? "OK" : "FAIL") : "";
    const gliss = m.gliss_to_freq_hz
      ? ` → ${m.gliss_to_freq_hz.toFixed(2)}Hz (deg ${m.gliss_to_degree})` : "";
    return `<tr><td>${m.time.toFixed(3)}</td><td>${m.degree}</td>` +
      `<td>${m.label ?? ""}</td><td>${m.freq_hz.toFixed(4)}</td>` +
      `<td>${m.ratio}</td><td>${est}</td><td>${err}</td>` +
      `<td class="${flag === "FAIL" ? "fail" : "pass"}">${flag}</td>` +
      `<td>${gliss}</td></tr>`;
  }).join("");

  const chords = data.chords.map((c) => {
    const voices = c.voices.map((v) =>
      `${v.freq_hz.toFixed(3)}Hz=${v.approx_simple_ratio} ` +
      `(${v.cents_vs_lowest >= 0 ? "+" : ""}${v.cents_vs_lowest.toFixed(1)}c)`
    ).join("<br>");
    const beats = c.pair_beats.map((p) =>
      `${p.pair[0].toFixed(1)}/${p.pair[1].toFixed(1)} → 拍频 ${p.beat_hz.toFixed(3)}Hz`
    ).join("<br>");
    return `<div class="chord"><b>t=${c.time.toFixed(2)}s 和弦</b><br>` +
      `各音：${voices}<br>拍频：${beats}</div>`;
  }).join("");

  const oor = data.out_of_range.length
    ? `<div class="warn"><h3>超出音源有效范围（已静音）</h3>` +
      data.out_of_range.map((e) => `<div>${JSON.stringify(e)}</div>`).join("") +
      `</div>` : "";

  $("result").innerHTML =
    `<h3>调律：${tuningName}（${data.tuning.period_steps} 级 / 周期比 ${data.tuning.period_ratio}，参考 ${data.tuning.ref_freq}Hz）</h3>` +
    `<table><thead><tr><th>时间s</th><th>级数</th><th>标签</th><th>映射频率Hz</th>` +
    `<th>精确比例</th><th>实测主频Hz</th><th>误差</th><th>核对</th><th>滑音</th></tr></thead>` +
    `<tbody>${rows}</tbody></table>` +
    `<h3>持续和声：和弦频率比与拍频</h3><div class="chords">${chords || "（无和弦）"}</div>` +
    oor;
}

async function renderCompare() {
  const scoreText = $("score-text").value;
  const nameA = $("tuning-select").value;
  const nameB = $("tuning-b").value;
  const [a, b] = await Promise.all([
    api("/api/render-score", { score_text: scoreText, tuning: nameA }),
    api("/api/render-score", { score_text: scoreText, tuning: nameB }),
  ]);
  const html = a.mapping.map((ma, i) => {
    const mb = b.mapping[i];
    const dHz = mb.freq_hz - ma.freq_hz;
    const dC = 1200 * Math.log2(mb.freq_hz / ma.freq_hz);
    return `<tr><td>${ma.time.toFixed(3)}</td><td>${ma.degree}</td>` +
      `<td>${ma.freq_hz.toFixed(4)}</td><td>${mb.freq_hz.toFixed(4)}</td>` +
      `<td>${dHz >= 0 ? "+" : ""}${dHz.toFixed(4)}</td>` +
      `<td class="${Math.abs(dC) > 5 ? "diff" : ""}">${dC >= 0 ? "+" : ""}${dC.toFixed(3)}c</td></tr>`;
  }).join("");
  $("result").innerHTML =
    `<h3>同一谱面调律对比：A=${nameA} vs B=${nameB}</h3>` +
    `<table><thead><tr><th>时间s</th><th>级数</th><th>A 频率Hz</th>` +
    `<th>B 频率Hz</th><th>ΔHz</th><th>Δ音分</th></tr></thead><tbody>${html}</tbody></table>` +
    `<p>两侧频率均为各自调律下的真实映射；Δ 音分超过 5c 的单元格高亮，差异可闻而非界面数字变化。</p>`;
}

async function exportWav() {
  const score = encodeURIComponent($("score-text").value);
  const tuning = encodeURIComponent($("tuning-select").value);
  const url = `/api/export-wav?name=microtone_export&tuning=${tuning}&score=${score}`;
  const a = document.createElement("a");
  a.href = url;
  a.download = `microtone_${tuning}.wav`;
  a.click();
  logKey(`已请求导出 WAV（调律 ${tuning}）`);
}

// ---- 调律资料迁移（旧弯音 -> 显式比值，全程调用后端新实现） ---------------

const LEGACY_SAMPLES = {
  quarter: {
    tuning: `{
  "format_version": "legacy-bend/1",
  "name": "旧四分音弯音律（24TET 偏移）",
  "ref_freq": 261.625565,
  "ref_midi_note": 60,
  "bend_range_cents": 200.0,
  "channel_count": 4,
  "offsets_cents": {
    "60": 0.0, "61": 50.0, "62": 0.0, "63": 50.0,
    "64": 0.0, "65": 50.0, "66": 0.0, "67": 50.0,
    "68": 0.0, "69": 50.0, "70": 0.0, "71": 50.0, "72": 0.0
  }
}`,
    score: `bpm 120
legacy-chord 60,61 2.0
legacy-rest 0.25
legacy-note 62 1.0
legacy-note 63 1.0
legacy-rest 0.5
legacy-chord 60,64 1.5
legacy-rest 0.5
legacy-note 72 1.0
`,
  },
  narrow: {
    tuning: `{
  "format_version": "legacy-bend/1",
  "name": "旧纯律偏移表（窄弯音硬件）",
  "ref_freq": 261.625565,
  "ref_midi_note": 60,
  "bend_range_cents": 100.0,
  "channel_count": 2,
  "offsets_cents": {
    "60": 0.0, "62": -13.686, "64": 1.955, "65": -15.641,
    "67": 3.91, "69": -17.594, "71": -11.731, "72": 0.0, "76": 150.0
  }
}`,
    score: `bpm 100
legacy-chord 60,64 2.0
legacy-rest 0.5
legacy-note 62 1.0
legacy-rest 0.5
legacy-chord 60,67,76 1.5
`,
  },
};

function showMigrationError(msg) {
  const box = $("migration-error");
  box.classList.remove("hidden");
  box.textContent = `迁移被拒绝：${msg}`;
}

function showMigrationResult(data) {
  $("migration-error").classList.add("hidden");
  const box = $("migration-result");
  box.classList.remove("hidden");

  const noteRows = data.notes.map((n) => {
    const tag = !n.representable_on_legacy ? "旧通道不可表达"
      : (n.held_pending ? "曾 pending" : `ch${n.channel}`);
    return `<tr><td>L${n.legacy_midi_note}</td><td>${n.new_degree}</td>` +
      `<td>${n.label}</td><td>${n.legacy_target_hz.toFixed(4)}</td>` +
      `<td>${n.new_mapped_hz.toFixed(4)}</td><td>${n.ratio_vs_ref}</td>` +
      `<td>${n.offset_cents.toFixed(2)}c</td><td>${tag}</td>` +
      `<td>${n.note}</td></tr>`;
  }).join("");

  const checks = data.verification.map((v) => {
    const flag = v.pass ? "OK" : "FAIL";
    return `<tr class="${flag === "FAIL" ? "fail" : "pass"}">` +
      `<td>L${v.legacy_midi_note}→deg${v.new_degree}</td>` +
      `<td>${v.legacy_target_hz.toFixed(4)}</td>` +
      `<td>${v.new_mapped_hz.toFixed(4)}</td>` +
      `<td>${v.measured_hz.toFixed(4)}</td>` +
      `<td>${v.error_vs_legacy_cents.toFixed(3)}c</td>` +
      `<td>${v.error_vs_new_cents.toFixed(3)}c</td><td>${flag}</td></tr>`;
  }).join("");

  const timeline = data.channel_timeline.map((e) =>
    `<div>t=${e.time.toFixed(3)}s <b>${e.kind}</b> 通道${e.channel} ` +
    `音${e.midi_note ?? "—"} 弯音${e.bend_cents.toFixed(2)}c ` +
    `${e.target_freq_hz ? e.target_freq_hz.toFixed(2) + "Hz" : ""} ` +
    `${e.detail}</div>`).join("");

  const bad = data.unrepresentable.map((u) =>
    `<div>L${u.legacy_midi_note}：偏移 ${u.offset_cents.toFixed(2)}c 超出 ` +
    `±${u.bend_range_cents.toFixed(0)}c，真实目标 ${u.target_freq_hz.toFixed(3)}Hz ` +
    `已由新映射保留（${u.reason}）</div>`).join("");

  const warnings = data.warnings.map((w) => `<div>${w}</div>`).join("");

  const cmp = data.strategy_comparison;
  const cmpRows = cmp.hold_ratio.map((r, i) => {
    const hf = cmp.hold_freq[i];
    const ss = cmp.safe_switch[i];
    return `<tr><td>L${r.legacy_midi_note}</td>` +
      `<td>${hf.old_freq_hz.toFixed(3)} → ${hf.new_freq_hz.toFixed(3)} ` +
      `(Δ${hf.delta_cents.toFixed(2)}c)</td>` +
      `<td>${r.old_freq_hz.toFixed(3)} → ${r.new_freq_hz.toFixed(3)} ` +
      `(Δ${r.delta_cents.toFixed(2)}c)</td>` +
      `<td>${ss.old_freq_hz.toFixed(3)} → ${ss.new_freq_hz.toFixed(3)} ` +
      `(Δ${ss.delta_cents.toFixed(2)}c)</td></tr>`;
  }).join("");
  const safePts = cmp.safe_points.map((p) =>
    `t=${p.time.toFixed(3)}s（${p.reason}）`).join("；");

  box.innerHTML =
    `<h3>迁移格式版本 ${data.format_version}（旧 ${data.legacy_format_version}）` +
    `· 频率契约 <b>${data.frequency_contract_ok ? "通过" : "失败"}</b>` +
    ` · 已注册为调律 <b>${data.registered_name}</b></h3>` +
    (bad ? `<div class="warn"><h3>旧通道无法表达的具体音</h3>${bad}</div>` : "") +
    (warnings ? `<div class="warn"><h3>和弦标记</h3>${warnings}</div>` : "") +
    `<h3>逐音迁移：旧级 → 新级数 / 真实频率 / 比值 / 通道事件</h3>` +
    `<table><thead><tr><th>旧级</th><th>新级数</th><th>标签</th>` +
    `<th>旧目标Hz</th><th>新映射Hz</th><th>精确比例</th><th>旧偏移</th>` +
    `<th>通道</th><th>说明</th></tr></thead><tbody>${noteRows}</tbody></table>` +
    `<h3>每音独听：后端渲染 PCM 后实测主频，同时核对旧目标与新映射</h3>` +
    `<table><thead><tr><th>音</th><th>旧目标Hz</th><th>新映射Hz</th>` +
    `<th>实测Hz</th><th>对旧误差</th><th>对新误差</th><th>核对</th></tr></thead>` +
    `<tbody>${checks}</tbody></table>` +
    `<h3>实际通道事件时间线（含释放占用与 pending 补发）</h3>` +
    `<div class="timeline">${timeline}</div>` +
    `<h3>切换策略对比：按原音程比保留持续音 vs 安全换气点切换</h3>` +
    `<p>${cmp.explanation}</p>` +
    `<table><thead><tr><th>持续音</th><th>hold_freq（保物理频率）</th>` +
    `<th>hold_ratio（随新映射）</th><th>安全换气点（推迟到无在发音）</th></tr></thead>` +
    `<tbody>${cmpRows}</tbody></table><p>安全换气点：${safePts}</p>` +
    `<h3>迁移后谱面（走新映射发声，可直接在③渲染）</h3>` +
    `<pre>${data.migrated_score_text}</pre>`;
}

async function runMigration() {
  let legacyTuning;
  try {
    legacyTuning = JSON.parse($("legacy-tuning").value);
  } catch (err) {
    showMigrationError(`旧调律 JSON 解析失败：${err.message}`);
    return;
  }
  try {
    const data = await api("/api/migrate-legacy", {
      legacy_tuning: legacyTuning,
      legacy_score_text: $("legacy-score").value,
      release_seconds: parseFloat($("migrate-release").value),
    });
    showMigrationResult(data);
    // 注册到调律选择器并切换，使琴键/渲染立即使用新实现发声
    const sel = $("tuning-select");
    if (![...sel.options].some((o) => o.value === data.registered_name)) {
      sel.add(new Option(data.registered_name, data.registered_name));
    }
    tuningCache[data.registered_name] = data.tuning;
    await selectTuning(data.registered_name);
    $("score-text").value = data.migrated_score_text;
    logKey(`调律资料迁移完成：${data.registered_name}，` +
      `频率契约${data.frequency_contract_ok ? "通过" : "失败"}`);
  } catch (err) {
    showMigrationError(err.message);
  }
}

function fillLegacySample(kind) {
  const s = LEGACY_SAMPLES[kind];
  $("legacy-tuning").value = s.tuning;
  $("legacy-score").value = s.score;
  $("migration-error").classList.add("hidden");
}

// ---- 初始化 ----------------------------------------------------------------

function init() {
  for (const [name, text] of Object.entries(SAMPLE_SCORES)) {
    $("score-select").add(new Option(name, name));
  }
  $("score-select").onchange = (e) => {
    const v = e.target.value;
    if (v) $("score-text").value = SAMPLE_SCORES[v];
  };
  $("score-text").value = SAMPLE_SCORES["四分音与拍频"];

  $("tuning-select").onchange = (e) => selectTuning(e.target.value);
  $("policy-select").onchange = async (e) => {
    await api("/api/set-policy", { policy: e.target.value });
    logKey(`续接策略切换为 ${e.target.value}`);
  };
  $("apply-policy").onclick = async () => {
    const p = $("policy-select").value;
    await api("/api/set-policy", { policy: p });
    logKey(`已应用策略 ${p}`);
  };
  $("build-keys").onclick = buildKeyboard;
  $("save-tuning").onclick = async () => {
    const payload = {
      tuning: collectTuningForm(),
      name: $("t-saveas").value.trim() || collectTuningForm().name,
    };
    const data = await api("/api/update-tuning", payload);
    tuningCache[payload.name] = data.tuning;
    const sel = $("tuning-select");
    if (![...sel.options].some((o) => o.value === payload.name)) {
      sel.add(new Option(payload.name, payload.name));
    }
    sel.value = payload.name;
    currentTuning = data.tuning;
    renderTuningEditor(currentTuning);
    buildKeyboard();
    showContinuation(data.voice_continuation);
    logKey(`调律已保存：${payload.name}`);
  };
  $("reload-tuning").onclick = () => renderTuningEditor(currentTuning);
  $("render-score").onclick = renderScore;
  $("export-wav").onclick = exportWav;
  $("render-compare").onclick = renderCompare;

  $("run-migrate").onclick = runMigration;
  $("load-legacy-sample").onclick = () => fillLegacySample("quarter");
  $("load-narrow-sample").onclick = () => fillLegacySample("narrow");
  fillLegacySample("quarter");

  // 独立的滑音按钮（放在琴键区底部）
  const glissBtn = document.createElement("button");
  glissBtn.textContent = "触发滑音（从目标级前 4 级滑入）";
  glissBtn.className = "primary";
  glissBtn.onclick = playGlissando;
  $("panel-keys").appendChild(glissBtn);

  loadTunings();
}

document.addEventListener("DOMContentLoaded", init);
