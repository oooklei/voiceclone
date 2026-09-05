/* Voice Clone 管理页面逻辑（无构建链，原生 JS）。
 * 契约：/api/v1/voices（multipart 创建、JSON patch、retrain、status、delete）
 *       /api/v1/tts/speak（JSON {voice_id, text, params?} → audio/wav）
 * 注意：SpeakParams 字段名为 speed_ratio / loudness_ratio / pitch_ratio。
 */
"use strict";

const $ = (id) => document.getElementById(id);

const els = {
  voiceName: $("voiceName"),
  providerSel: $("providerSel"),
  micBtn: $("micBtn"),
  sysBtn: $("sysBtn"),
  timer: $("timer"),
  recStatus: $("recStatus"),
  fileInput: $("fileInput"),
  refText: $("refText"),
  refTextField: $("refTextField"),
  createBtn: $("createBtn"),
  log: $("log"),
  voiceList: $("voiceList"),
  refreshBtn: $("refreshBtn"),
  previewText: $("previewText"),
  speedRatio: $("speedRatio"),
  loudnessRatio: $("loudnessRatio"),
  pitchRatio: $("pitchRatio"),
  speedVal: $("speedVal"),
  loudnessVal: $("loudnessVal"),
  pitchVal: $("pitchVal"),
  previewBtn: $("previewBtn"),
  stopBtn: $("stopBtn"),
  previewStatus: $("previewStatus"),
  player: $("player"),
};

const MAX_RECORD_MS = 15000; // 15 秒自动停止
const POLL_MS = 10000; // 训练状态轮询间隔
const SOURCE_LABEL = { mic: "麦克风", system: "系统音频" };

let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;
let recordingSource = null; // 'mic' | 'system'
let selectedSource = null; // 'mic' | 'system' — 互斥选择的录音来源
let timerId = null;
let recSeconds = 0;
let pollTimers = new Map(); // voiceId -> intervalId
let selectedVoiceId = null;
let audioCtx = null;

/* ---------------------------------------------------------------- utils */

function log(msg) {
  const t = new Date().toLocaleTimeString();
  els.log.textContent = `[${t}] ${msg}\n` + els.log.textContent;
}

function fmtTime(s) {
  const m = Math.floor(s / 60);
  const ss = s % 60;
  return String(m).padStart(2, "0") + ":" + String(ss).padStart(2, "0");
}

function setStatus(el, text, isErr) {
  el.textContent = text;
  el.classList.toggle("err", !!isErr);
}

async function api(path, options) {
  const resp = await fetch(path, options);
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      if (body && body.message) detail = body.message;
      else if (body && body.detail) {
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      }
    } catch (_) {
      /* non-JSON error body */
    }
    const err = new Error(detail);
    err.status = resp.status;
    throw err;
  }
  return resp;
}

function statusBadge(status) {
  const cls = {
    draft: "badge-draft",
    training: "badge-training",
    active: "badge-active",
    disabled: "badge-disabled",
    failed: "badge-failed",
  }[status] || "badge-draft";
  const label = {
    draft: "草稿",
    training: "训练中",
    active: "可用",
    disabled: "已停用",
    failed: "失败",
  }[status] || status;
  return `<span class="badge ${cls}">${label}</span>`;
}

/* ------------------------------------------------------------ recording */

async function startRecording(source) {
  if (isRecording) return; // 防重入：双击/快速点击不会泄漏第二个 interval
  if (!window.isSecureContext) {
    setStatus(
      els.recStatus,
      "麦克风/系统音频仅在 HTTPS 下可用：请改用 https://192.168.1.9:8021 访问（首次需接受自签名证书警告），或改用文件上传",
      true,
    );
    log("非安全上下文（HTTP），浏览器拒绝录音");
    return;
  }
  try {
    let stream;
    if (source === "system") {
      setStatus(els.recStatus, "请在弹出的窗口中选择要录制的音频来源（标签页/窗口/系统声音）", false);
      stream = await getSystemAudioStream();
    } else {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
    }
    mediaRecorder = new MediaRecorder(stream);
    audioChunks = [];
    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) audioChunks.push(e.data);
    };
    mediaRecorder.onstop = () => {
      clearInterval(timerId); // 兜底：无论从哪条路径停止都清掉计时器
      timerId = null;
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(audioChunks, { type: mediaRecorder.mimeType || "audio/webm" });
      els.fileInput.files = null;
      els.fileInput._recordedBlob = blob;
      setStatus(els.recStatus, `已录制 ${fmtTime(recSeconds)}（${SOURCE_LABEL[source]}），点击「新建音色」提交`, false);
      log(`录音完成（${SOURCE_LABEL[source]}）：${(blob.size / 1024).toFixed(1)} KB`);
      // 系统音频自动识别文字稿（本地引擎必填）；话筒不触发
      if (source === "system") autoTranscribe(blob);
    };
    mediaRecorder.start();
    isRecording = true;
    recordingSource = source;
    els.micBtn.classList.add("recording");
    els.sysBtn.classList.add("recording");
    recSeconds = 0;
    els.timer.textContent = fmtTime(0);
    setStatus(els.recStatus, `${SOURCE_LABEL[source]}录制中... 15 秒自动停止`, false);
    timerId = setInterval(() => {
      recSeconds++;
      els.timer.textContent = fmtTime(recSeconds);
      if (recSeconds * 1000 >= MAX_RECORD_MS) stopRecording();
    }, 1000);
    log(`开始录制（${SOURCE_LABEL[source]}）`);
  } catch (err) {
    setStatus(els.recStatus, `${SOURCE_LABEL[source]}录制失败：${err.message}`, true);
    log(`${SOURCE_LABEL[source]}录制错误: ${err.message}`);
  }
}

async function getSystemAudioStream() {
  // Chrome 124+：仅音频的屏幕捕获选择器（displaySurface: 'audio'）
  try {
    const stream = await navigator.mediaDevices.getDisplayMedia({
      video: { displaySurface: "audio" },
      audio: true,
    });
    stream.getVideoTracks().forEach((t) => t.stop());
    return stream;
  } catch (err) {
    // 用户取消选择器：直接抛出，不弹第二个选择器
    if (err.name === "NotAllowedError" || err.name === "AbortError") throw err;
    // 旧浏览器回退：完整屏幕捕获后丢弃视频轨
    const stream = await navigator.mediaDevices.getDisplayMedia({
      video: true,
      audio: true,
    });
    stream.getVideoTracks().forEach((t) => t.stop());
    return stream;
  }
}

function stopRecording() {
  if (!isRecording || !mediaRecorder) return;
  isRecording = false;
  recordingSource = null;
  clearInterval(timerId);
  timerId = null;
  els.micBtn.classList.remove("recording");
  els.sysBtn.classList.remove("recording");
  try {
    mediaRecorder.stop();
  } catch (_) {
    /* already stopped */
  }
}

/* 系统音频录制完成后自动识别文字稿（本地引擎必填；话筒不触发） */
async function autoTranscribe(blob) {
  setStatus(els.recStatus, "系统音频已录制，正在自动识别文字稿...", false);
  const form = new FormData();
  form.append("file", blob, "recording.webm");
  try {
    const resp = await api("/api/v1/asr", { method: "POST", body: form });
    const data = await resp.json();
    const text = (data.text || "").trim();
    if (text) {
      els.refText.value = text;
      const conf = Math.round((data.confidence || 0) * 100);
      setStatus(els.recStatus, `已自动识别文字稿（${data.engine}，置信度 ${conf}%），请核对后提交`, false);
      log(`ASR 识别成功：${text}`);
    } else {
      setStatus(els.recStatus, "未识别到语音内容，请手动输入文字稿或重新录制", true);
      log("ASR 返回空文本");
    }
  } catch (err) {
    setStatus(els.recStatus, "自动识别失败：" + err.message + "，请手动输入文字稿", true);
    log("ASR 识别失败: " + err.message);
  }
}

function selectSource(source) {
  selectedSource = source;
  els.micBtn.classList.toggle("selected", source === "mic");
  els.sysBtn.classList.toggle("selected", source === "system");
}

els.micBtn.addEventListener("click", () => {
  if (isRecording) stopRecording();
  selectSource("mic");
  startRecording("mic");
});
els.sysBtn.addEventListener("click", () => {
  if (isRecording) stopRecording();
  selectSource("system");
  startRecording("system");
});

/* -------------------------------------------------------------- create */

async function createVoice() {
  const name = els.voiceName.value.trim();
  if (!name) {
    setStatus(els.recStatus, "请先填写音色名称", true);
    return;
  }
  const provider = els.providerSel.value;
  const recorded = els.fileInput._recordedBlob;
  const file = els.fileInput.files && els.fileInput.files[0];
  const audio = recorded || file;
  if (!audio) {
    setStatus(els.recStatus, "请先录音或选择音频文件", true);
    return;
  }
  if (audio.size === 0) {
    setStatus(els.recStatus, "音频文件为空，请重新录音或选择文件", true);
    return;
  }
  const referenceText = els.refText.value.trim();
  if (provider === "local" && !referenceText) {
    setStatus(els.recStatus, "本地引擎需要填写参考音频文字稿", true);
    return;
  }

  const form = new FormData();
  form.append("name", name);
  form.append("provider", provider);
  form.append("file", audio, recorded ? "recording.webm" : file.name);
  if (referenceText) form.append("reference_text", referenceText);

  els.createBtn.disabled = true;
  setStatus(els.recStatus, "上传中...", false);
  try {
    const resp = await api("/api/v1/voices", { method: "POST", body: form });
    const voice = await resp.json();
    log(`新建成功：${voice.name} (${voice.id}) status=${voice.status}`);
    setStatus(els.recStatus, `已新建音色「${voice.name}」，训练中...`, false);
    els.voiceName.value = "";
    els.fileInput.value = "";
    delete els.fileInput._recordedBlob;
    await loadVoices();
    startPolling(voice.id);
  } catch (err) {
    setStatus(els.recStatus, "创建失败：" + err.message, true);
    log("创建失败: " + err.message);
  } finally {
    els.createBtn.disabled = false;
  }
}

els.createBtn.addEventListener("click", createVoice);

/* 本地引擎需要参考音频文字稿：切换引擎时显示/隐藏输入框 */
function syncRefTextField() {
  const isLocal = els.providerSel.value === "local";
  els.refTextField.style.display = isLocal ? "" : "none";
}
els.providerSel.addEventListener("change", syncRefTextField);

/* ---------------------------------------------------------------- list */

async function loadVoices() {
  try {
    const resp = await api("/api/v1/voices");
    const voices = await resp.json();
    renderVoices(voices);
  } catch (err) {
    setStatus(els.previewStatus, "加载音色列表失败：" + err.message, true);
    log("加载列表失败: " + err.message);
  }
}

function renderVoices(voices) {
  if (!voices.length) {
    els.voiceList.innerHTML = '<div class="empty">暂无音色，请先在左侧创建</div>';
    return;
  }
  els.voiceList.innerHTML = voices
    .map((v) => {
      const needsReview = v.needs_review
        ? '<span class="review-flag" title="参考音频头部过静，已自动偏移切片">需复核</span>'
        : "";
      const isLocal = v.provider === "local";
      return `<div class="voice-card ${v.id === selectedVoiceId ? "selected" : ""}" data-id="${v.id}">
        <div class="voice-head">
          <span class="voice-name">${escapeHtml(v.name)}</span>
          ${statusBadge(v.status)}
          ${needsReview}
        </div>
        <div class="voice-meta">
          <span>${isLocal ? "本地 ZipVoice" : "云端 ICL"}</span>
          <span>语速 ${v.speed_ratio ?? 1.0}</span>
          <span>音量 ${v.loudness_ratio ?? 1.0}</span>
          <span>音调 ${v.pitch_ratio ?? 1.0}</span>
        </div>
        <div class="voice-actions">
          <button class="btn-select" data-action="select">选用</button>
          <button class="btn-retrain" data-action="retrain" ${v.status === "training" ? "disabled" : ""}>重新训练</button>
          <button class="btn-toggle" data-action="toggle">${v.status === "disabled" ? "启用" : "停用"}</button>
          <button class="btn-delete" data-action="delete">删除</button>
        </div>
      </div>`;
    })
    .join("");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

els.voiceList.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const card = btn.closest(".voice-card");
  const id = card.dataset.id;
  const action = btn.dataset.action;

  if (action === "select") {
    selectedVoiceId = id;
    document.querySelectorAll(".voice-card").forEach((c) => c.classList.toggle("selected", c.dataset.id === id));
    const voice = await fetchVoice(id);
    if (voice) applyVoiceToSliders(voice);
    return;
  }
  if (action === "retrain") {
    btn.disabled = true;
    btn.textContent = "训练中...";
    try {
      await api(`/api/v1/voices/${id}/retrain`, { method: "POST" });
      log(`触发重新训练：${id}`);
      setStatus(els.recStatus, "重新训练中...", false);
      startPolling(id);
      await loadVoices();
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "重新训练";
      log("重新训练失败: " + err.message);
      setStatus(els.recStatus, "重新训练失败：" + err.message, true);
    }
    return;
  }
  if (action === "toggle") {
    try {
      const voice = await fetchVoice(id);
      const next = voice.status === "disabled" ? "active" : "disabled";
      await api(`/api/v1/voices/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: next }),
      });
      log(`${id} -> ${next}`);
      await loadVoices();
    } catch (err) {
      log("切换状态失败: " + err.message);
    }
    return;
  }
  if (action === "delete") {
    if (!confirm("确定删除该音色？此操作不可恢复。")) return;
    try {
      await api(`/api/v1/voices/${id}`, { method: "DELETE" });
      stopPolling(id);
      if (selectedVoiceId === id) selectedVoiceId = null;
      log(`已删除：${id}`);
      await loadVoices();
    } catch (err) {
      log("删除失败: " + err.message);
    }
  }
});

async function fetchVoice(id) {
  try {
    const resp = await api(`/api/v1/voices/${id}`);
    return await resp.json();
  } catch (err) {
    log("获取音色详情失败: " + err.message);
    return null;
  }
}

/* -------------------------------------------------------------- polling */

function startPolling(voiceId) {
  stopPolling(voiceId);
  const iv = setInterval(async () => {
    try {
      const resp = await api(`/api/v1/voices/${voiceId}/status`);
      const { status } = await resp.json();
      log(`轮询 ${voiceId}: ${status}`);
      // 实时更新对应卡片的状态徽章，无需整表重绘
      const card = document.querySelector(`.voice-card[data-id="${voiceId}"]`);
      if (card) {
        const badge = card.querySelector(".badge");
        if (badge) badge.outerHTML = statusBadge(status);
        const retrainBtn = card.querySelector('button[data-action="retrain"]');
        if (retrainBtn) retrainBtn.disabled = status === "training";
      }
      if (status === "active" || status === "failed") {
        stopPolling(voiceId);
        await loadVoices();
        if (status === "failed") {
          setStatus(els.recStatus, "训练失败，可点击「重新训练」重试", true);
        } else {
          setStatus(els.recStatus, "训练完成，音色可用", false);
        }
      }
    } catch (err) {
      log(`轮询失败 ${voiceId}: ${err.message}`);
      stopPolling(voiceId);
    }
  }, POLL_MS);
  pollTimers.set(voiceId, iv);
}

function stopPolling(voiceId) {
  const iv = pollTimers.get(voiceId);
  if (iv) {
    clearInterval(iv);
    pollTimers.delete(voiceId);
  }
}

/* -------------------------------------------------------------- preview */

function applyVoiceToSliders(voice) {
  const isLocal = voice.provider === "local";
  els.speedRatio.value = voice.speed_ratio ?? 1.0;
  els.loudnessRatio.value = voice.loudness_ratio ?? 1.0;
  els.pitchRatio.value = voice.pitch_ratio ?? 1.0;
  updateSliderLabels();
  // 本地引擎不支持音量/音调，禁用并标注「仅云端」
  els.loudnessRatio.disabled = isLocal;
  els.pitchRatio.disabled = isLocal;
  els.loudnessRatio.closest(".slider-row").classList.toggle("cloud-only", isLocal);
  els.pitchRatio.closest(".slider-row").classList.toggle("cloud-only", isLocal);
  setStatus(els.previewStatus, isLocal ? "已选用本地音色（音量/音调仅云端支持）" : `已选用音色：${voice.name}`, false);
}

function updateSliderLabels() {
  els.speedVal.textContent = Number(els.speedRatio.value).toFixed(2);
  els.loudnessVal.textContent = Number(els.loudnessRatio.value).toFixed(2);
  els.pitchVal.textContent = Number(els.pitchRatio.value).toFixed(2);
}

[els.speedRatio, els.loudnessRatio, els.pitchRatio].forEach((s) =>
  s.addEventListener("input", updateSliderLabels)
);

async function previewSpeak() {
  if (!selectedVoiceId) {
    setStatus(els.previewStatus, "请先在列表中选择一个音色", true);
    return;
  }
  const text = els.previewText.value.trim();
  if (!text) {
    setStatus(els.previewStatus, "请输入试听文本", true);
    return;
  }
  const params = {
    speed_ratio: Number(els.speedRatio.value),
    loudness_ratio: Number(els.loudnessRatio.value),
    pitch_ratio: Number(els.pitchRatio.value),
  };
  els.previewBtn.disabled = true;
  setStatus(els.previewStatus, "合成中...", false);
  try {
    const resp = await api("/api/v1/tts/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice_id: selectedVoiceId, text, params }),
    });
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    els.player.src = url;
    els.player.style.display = "block";
    els.stopBtn.disabled = false;
    setStatus(els.previewStatus, "合成完成，正在播放", false);
    log(`试听合成成功：${(blob.size / 1024).toFixed(1)} KB`);
    // 自动播放：设置 src 后立即播放，避免用户再点一次
    els.player.play().catch(() => {
      setStatus(els.previewStatus, "合成完成，点击播放（浏览器阻止了自动播放）", false);
    });
  } catch (err) {
    setStatus(els.previewStatus, "合成失败：" + err.message, true);
    log("试听失败: " + err.message);
  } finally {
    els.previewBtn.disabled = false;
  }
}

els.previewBtn.addEventListener("click", previewSpeak);

els.stopBtn.addEventListener("click", () => {
  els.player.pause();
  els.player.currentTime = 0;
  els.stopBtn.disabled = true;
});

/* ---------------------------------------------------------------- init */

/* 引擎可用性：云端未配置时默认选本地，并提示云端试听可能无声音 */
async function initEngines() {
  try {
    const resp = await api("/api/v1/engines");
    const engines = await resp.json();
    const cloudEnabled = engines.cloud && engines.cloud.enabled;
    const localEnabled = engines.local && engines.local.enabled;
    if (!cloudEnabled) {
      // 云端引擎未配置：默认选本地，避免新建音色默认走静音 mock
      els.providerSel.value = "local";
      syncRefTextField();
      setStatus(els.recStatus, "云端引擎未配置，已默认使用本地引擎；选择云端音色试听可能无声音", false);
    }
    if (!localEnabled && !cloudEnabled) {
      setStatus(els.recStatus, "本地与云端引擎均未配置，试听可能无声音", true);
    }
  } catch (err) {
    log("获取引擎状态失败: " + err.message);
  }
}

els.refreshBtn.addEventListener("click", loadVoices);
syncRefTextField();
initEngines();
loadVoices();