/* VoiceClone Studio — 前端逻辑
 * 契约：/api/v1/voices / /api/v1/tts/speak / /api/v1/asr / /api/v1/capture/*
 * 桌面版（Tauri）：系统音频走侧车 WASAPI loopback；浏览器版走 getDisplayMedia
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
  fileDrop: $("fileDrop"),
  refText: $("refText"),
  refTextField: $("refTextField"),
  createBtn: $("createBtn"),
  voiceList: $("voiceList"),
  refreshBtn: $("refreshBtn"),
  previewText: $("previewText"),
  charCount: $("charCount"),
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
  statusPill: $("statusPill"),
  statusDot: $("statusDot"),
  statusText: $("statusText"),
  engineHint: $("engineHint"),
  toastContainer: $("toastContainer"),
};

const MAX_RECORD_MS = 15000;
const POLL_MS = 10000;
const SOURCE_LABEL = { mic: "麦克风", system: "系统音频" };
const isTauri = typeof window.__TAURI__ !== "undefined";

let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;
let recordingSource = null;
let timerId = null;
let recSeconds = 0;
let pollTimers = new Map();
let selectedVoiceId = null;

/* ===================================================== Toast 通知 */

const TOAST_ICONS = { info: "ℹ", success: "✓", warning: "⚠", error: "✕" };

function toast(message, type = "info", title = null, duration = 4000) {
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.setAttribute("role", type === "error" ? "alert" : "status");
  el.innerHTML =
    '<span class="toast-icon" aria-hidden="true">' + (TOAST_ICONS[type] || TOAST_ICONS.info) + '</span>' +
    '<div class="toast-content">' +
      (title ? '<div class="toast-title"></div>' : '') +
      '<div class="toast-message"></div>' +
    '</div>' +
    '<button class="toast-close" aria-label="关闭通知">×</button>';
  el.querySelector(".toast-message").textContent = message;
  if (title) el.querySelector(".toast-title").textContent = title;
  el.querySelector(".toast-close").onclick = () => removeToast(el);
  els.toastContainer.appendChild(el);
  if (duration > 0) setTimeout(() => removeToast(el), duration);
  return el;
}

function removeToast(el) {
  if (!el || !el.parentNode) return;
  el.classList.add("removing");
  setTimeout(() => el.remove(), 250);
}

/* ===================================================== 工具 */

function setRecStatus(text, level = "info") {
  els.recStatus.textContent = text;
  els.recStatus.className = "rec-hint " + (level === "error" ? "error" : level === "success" ? "success" : "");
}

function setPreviewStatus(text, level = "info") {
  els.previewStatus.textContent = text;
  els.previewStatus.className = "preview-status " + (level === "error" ? "error" : level === "success" ? "success" : "");
}

function fmtTime(s) {
  const m = Math.floor(s / 60);
  const ss = s % 60;
  return String(m).padStart(2, "0") + ":" + String(ss).padStart(2, "0");
}

async function api(path, options) {
  const resp = await fetch(path, options);
  if (!resp.ok) {
    let detail = "HTTP " + resp.status;
    try {
      const body = await resp.json();
      if (body && body.message) detail = body.message;
      else if (body && body.detail) {
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      }
    } catch (_) {}
    const err = new Error(detail);
    err.status = resp.status;
    throw err;
  }
  return resp;
}

function statusBadge(status) {
  const cls = { draft: "badge-draft", training: "badge-training", active: "badge-active", disabled: "badge-disabled", failed: "badge-failed" }[status] || "badge-draft";
  const label = { draft: "草稿", training: "训练中", active: "可用", disabled: "已停用", failed: "失败" }[status] || status;
  return '<span class="badge ' + cls + '">' + label + '</span>';
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function escapeAttr(s) { return escapeHtml(s); }

/* ===================================================== 连接状态 */

function setConnectionStatus(state, text) {
  els.statusPill.className = "status-pill " + state;
  els.statusText.textContent = text;
}

async function checkConnection() {
  try {
    const resp = await fetch("/health", { cache: "no-store" });
    if (resp.ok) {
      setConnectionStatus("online", "已连接");
      return true;
    }
  } catch (_) {}
  setConnectionStatus("offline", "未连接");
  return false;
}

/* ===================================================== 录音 */

async function getSystemAudioStream() {
  let stream;
  try {
    stream = await navigator.mediaDevices.getDisplayMedia({
      video: { displaySurface: "audio" },
      audio: true,
    });
  } catch (err) {
    if (err.name === "NotAllowedError" || err.name === "AbortError") throw err;
    stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
  }
  stream.getVideoTracks().forEach((t) => t.stop());
  const audioTracks = stream.getAudioTracks();
  if (audioTracks.length === 0) {
    stream.getTracks().forEach((t) => t.stop());
    throw new Error("未捕获到系统音频：请选择正在播放音频的标签页，不要选整个屏幕或窗口");
  }
  const track = audioTracks[0];
  if (track.readyState === "ended") {
    stream.getTracks().forEach((t) => t.stop());
    throw new Error("音频来源已关闭，请重新选择");
  }
  if (track.muted) {
    setRecStatus("等待系统音频输出（请确认所选标签页正在播放声音）...", "info");
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        track.onunmute = null;
        stream.getTracks().forEach((t) => t.stop());
        reject(new Error("未检测到系统音频输出：请确认所选标签页正在播放声音"));
      }, 5000);
      track.onunmute = () => { clearTimeout(timeout); track.onunmute = null; resolve(); };
    });
  }
  return stream;
}

async function startSystemAudioTauri() {
  try {
    await window.__TAURI__.core.invoke("start_capture");
    isRecording = true;
    recordingSource = "system";
    els.micBtn.classList.remove("selected");
    els.sysBtn.classList.add("selected", "recording");
    recSeconds = 0;
    els.timer.textContent = fmtTime(0);
    setRecStatus("系统音频录制中... 15 秒自动停止", "info");
    startTimer();
  } catch (err) {
    isRecording = false;
    recordingSource = null;
    setRecStatus("系统音频录制失败：" + err.message, "error");
    toast("系统音频启动失败", "error", err.message);
    throw err;
  }
}

async function stopSystemAudioTauri() {
  try {
    const wav = await window.__TAURI__.core.invoke("stop_capture");
    const blob = new Blob([new Uint8Array(wav)], { type: "audio/wav" });
    setRecordedAudio(blob, "recording.wav");
    setRecStatus("已录制 " + fmtTime(recSeconds) + "（系统音频），正在识别文字稿...", "info");
    autoTranscribe(blob, "recording.wav");
  } catch (err) {
    setRecStatus("系统音频录制失败：" + err.message, "error");
    toast("系统音频停止失败", "error", err.message);
  }
}

function setRecordedAudio(blob, filename) {
  els.fileInput._recordedBlob = blob;
  // 更新文件上传区显示
  const fileName = filename || "已录制音频";
  els.fileDrop.classList.add("has-file");
  const textEl = els.fileDrop.querySelector(".file-text");
  const subEl = els.fileDrop.querySelector(".file-sub");
  if (textEl) textEl.textContent = "✓ " + fileName;
  if (subEl) subEl.textContent = (blob.size / 1024).toFixed(1) + " KB · 点击重新选择";
  updateCreateButton();
}

function clearRecordedAudio() {
  delete els.fileInput._recordedBlob;
  els.fileDrop.classList.remove("has-file");
  const textEl = els.fileDrop.querySelector(".file-text");
  const subEl = els.fileDrop.querySelector(".file-sub");
  if (textEl) textEl.textContent = "点击或拖拽音频文件到此处";
  if (subEl) subEl.textContent = "支持 WAV / MP3 · 5–15 秒最佳";
  updateCreateButton();
}

function startTimer() {
  if (timerId) clearInterval(timerId);
  timerId = setInterval(() => {
    recSeconds++;
    els.timer.textContent = fmtTime(recSeconds);
    if (recSeconds * 1000 >= MAX_RECORD_MS) stopRecording();
  }, 1000);
}

async function startRecording(source) {
  if (isRecording) return;
  if (!window.isSecureContext && !isTauri) {
    const msg = "浏览器录音需要 HTTPS 环境。请通过 https:// 访问，或使用桌面版应用。";
    setRecStatus(msg, "error");
    toast("安全上下文不足", "error", "请使用 HTTPS 或桌面版");
    return;
  }
  if (source === "system" && isTauri) {
    try { await startSystemAudioTauri(); } catch (_) { /* already toasted */ }
    return;
  }
  try {
    let stream;
    if (source === "system") {
      setRecStatus("请在弹出窗口中选择要录制的音频来源（标签页/窗口）", "info");
      stream = await getSystemAudioStream();
    } else {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    }
    mediaRecorder = new MediaRecorder(stream);
    audioChunks = [];
    mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) audioChunks.push(e.data); };
    mediaRecorder.onstop = () => {
      clearInterval(timerId); timerId = null;
      stream.getTracks().forEach((t) => t.stop());
      const mimeType = mediaRecorder.mimeType || "audio/webm";
      const blob = new Blob(audioChunks, { type: mimeType });
      const ext = mimeType.includes("mp4") ? "mp4" : mimeType.includes("ogg") ? "ogg" : "webm";
      setRecordedAudio(blob, "recording." + ext);
      setRecStatus("已录制 " + fmtTime(recSeconds) + "（" + SOURCE_LABEL[source] + "），点击「新建音色」提交", "success");
      if (source === "system") autoTranscribe(blob, "recording." + ext);
    };
    mediaRecorder.start();
    isRecording = true;
    recordingSource = source;
    if (source === "mic") {
      els.micBtn.classList.add("selected", "recording");
      els.sysBtn.classList.remove("selected", "recording");
    } else {
      els.sysBtn.classList.add("selected", "recording");
      els.micBtn.classList.remove("selected", "recording");
    }
    recSeconds = 0;
    els.timer.textContent = fmtTime(0);
    setRecStatus(SOURCE_LABEL[source] + "录制中... 15 秒自动停止", "info");
    startTimer();
  } catch (err) {
    isRecording = false;
    recordingSource = null;
    const msg = SOURCE_LABEL[source] + "录制失败：" + err.message;
    setRecStatus(msg, "error");
    toast("录音启动失败", "error", err.message);
  }
}

function stopRecording() {
  if (!isRecording) return;
  const source = recordingSource;
  isRecording = false;
  recordingSource = null;
  clearInterval(timerId); timerId = null;
  els.micBtn.classList.remove("recording");
  els.sysBtn.classList.remove("recording");
  if (isTauri && source === "system") {
    stopSystemAudioTauri();
    return;
  }
  if (!mediaRecorder) return;
  try { mediaRecorder.stop(); } catch (_) {}
}

els.micBtn.addEventListener("click", () => {
  if (isRecording && recordingSource === "mic") { stopRecording(); return; }
  if (isRecording) stopRecording();
  startRecording("mic");
});
els.sysBtn.addEventListener("click", () => {
  if (isRecording && recordingSource === "system") { stopRecording(); return; }
  if (isRecording) stopRecording();
  startRecording("system");
});

/* ===================================================== ASR */

async function autoTranscribe(blob, filename) {
  setRecStatus("系统音频已录制，正在自动识别文字稿...", "info");
  const form = new FormData();
  form.append("file", blob, filename);
  try {
    const resp = await api("/api/v1/asr", { method: "POST", body: form });
    const data = await resp.json();
    const text = (data.text || "").trim();
    if (text) {
      els.refText.value = text;
      const conf = Math.round((data.confidence || 0) * 100);
      setRecStatus("已自动识别文字稿（" + data.engine + "，置信度 " + conf + "%），请核对后提交", "success");
      toast("识别完成", "success", "置信度 " + conf + "%");
      updateCreateButton();
    } else {
      setRecStatus("未识别到语音内容，请手动输入文字稿或重新录制", "error");
      toast("识别失败", "warning", "未识别到语音内容");
    }
  } catch (err) {
    setRecStatus("自动识别失败：" + err.message + "，请手动输入文字稿", "error");
    toast("识别失败", "error", err.message);
  }
}

/* ===================================================== 创建音色 */

function updateCreateButton() {
  const name = els.voiceName.value.trim();
  const hasAudio = els.fileInput._recordedBlob || (els.fileInput.files && els.fileInput.files[0]);
  const provider = els.providerSel.value;
  const refText = els.refText.value.trim();
  const isValid = name && hasAudio && (provider !== "local" || refText);
  els.createBtn.disabled = !isValid;
}

async function createVoice() {
  const name = els.voiceName.value.trim();
  const provider = els.providerSel.value;
  const recorded = els.fileInput._recordedBlob;
  const file = els.fileInput.files && els.fileInput.files[0];
  const audio = recorded || file;
  const referenceText = els.refText.value.trim();

  if (provider === "local" && !referenceText) {
    toast("需要参考文字稿", "warning", "本地引擎必填");
    els.refText.focus();
    return;
  }
  if (!audio) {
    toast("缺少参考音频", "warning", "请录音或上传文件");
    return;
  }

  const form = new FormData();
  form.append("name", name);
  form.append("provider", provider);
  form.append("file", audio, recorded ? (recorded.type.includes("mp4") ? "recording.mp4" : "recording.webm") : file.name);
  if (referenceText) form.append("reference_text", referenceText);

  els.createBtn.classList.add("loading");
  els.createBtn.disabled = true;
  setRecStatus("上传中...", "info");
  try {
    const resp = await api("/api/v1/voices", { method: "POST", body: form });
    const voice = await resp.json();
    toast("音色创建成功", "success", "「" + voice.name + "」训练中...");
    setRecStatus("已新建音色「" + voice.name + "」，训练中...", "info");
    els.voiceName.value = "";
    clearRecordedAudio();
    els.refText.value = "";
    els.fileInput.value = "";
    await loadVoices();
    startPolling(voice.id);
  } catch (err) {
    setRecStatus("创建失败：" + err.message, "error");
    toast("创建失败", "error", err.message);
  } finally {
    els.createBtn.classList.remove("loading");
    updateCreateButton();
  }
}

els.createBtn.addEventListener("click", createVoice);
els.voiceName.addEventListener("input", updateCreateButton);
els.refText.addEventListener("input", updateCreateButton);

/* ===================================================== 文件上传 */

els.fileInput.addEventListener("change", () => {
  if (els.fileInput.files && els.fileInput.files[0]) {
    clearRecordedAudio();
    const f = els.fileInput.files[0];
    els.fileDrop.classList.add("has-file");
    const textEl = els.fileDrop.querySelector(".file-text");
    const subEl = els.fileDrop.querySelector(".file-sub");
    if (textEl) textEl.textContent = "✓ " + f.name;
    if (subEl) subEl.textContent = (f.size / 1024).toFixed(1) + " KB";
    updateCreateButton();
  }
});

// 拖拽支持
["dragenter", "dragover"].forEach((ev) => {
  els.fileDrop.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); els.fileDrop.classList.add("dragover"); });
});
["dragleave", "drop"].forEach((ev) => {
  els.fileDrop.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); els.fileDrop.classList.remove("dragover"); });
});
els.fileDrop.addEventListener("drop", (e) => {
  const files = e.dataTransfer.files;
  if (files && files[0]) {
    clearRecordedAudio();
    els.fileInput.files = files;
    const f = files[0];
    els.fileDrop.classList.add("has-file");
    const textEl = els.fileDrop.querySelector(".file-text");
    const subEl = els.fileDrop.querySelector(".file-sub");
    if (textEl) textEl.textContent = "✓ " + f.name;
    if (subEl) subEl.textContent = (f.size / 1024).toFixed(1) + " KB";
    updateCreateButton();
  }
});

/* ===================================================== 引擎切换 */

function syncRefTextField() {
  const isLocal = els.providerSel.value === "local";
  els.refTextField.style.display = isLocal ? "" : "none";
  els.engineHint.textContent = isLocal ? "本地引擎无需联网，数据不出本机" : "云端引擎需要联网，速度更快、音质更好";
  updateCreateButton();
}
els.providerSel.addEventListener("change", syncRefTextField);

/* ===================================================== 音色列表 */

async function loadVoices() {
  try {
    const resp = await api("/api/v1/voices");
    const voices = await resp.json();
    renderVoices(voices);
  } catch (err) {
    els.voiceList.innerHTML = '<div class="empty-state"><span class="empty-icon">⚠</span><span class="empty-text">加载失败：' + escapeHtml(err.message) + '</span></div>';
  }
}

function renderVoices(voices) {
  if (!voices.length) {
    els.voiceList.innerHTML = '<div class="empty-state"><span class="empty-icon">🎙️</span><span class="empty-text">暂无音色，请先在左侧创建</span></div>';
    return;
  }
  els.voiceList.innerHTML = voices.map((v) => {
    const needsReview = v.needs_review
      ? '<span class="review-flag" title="参考音频头部过静，已自动偏移切片">需复核</span>'
      : "";
    const isLocal = v.provider === "local";
    return (
      '<div class="voice-card ' + (v.id === selectedVoiceId ? "selected" : "") + '" data-id="' + escapeAttr(v.id) + '" role="listitem">' +
        '<div class="voice-head">' +
          '<span class="voice-name">' + escapeHtml(v.name) + '</span>' +
          statusBadge(v.status) +
          needsReview +
        '</div>' +
        '<div class="voice-meta">' +
          '<span>' + (isLocal ? "本地 ZipVoice" : "云端 ICL") + '</span>' +
          '<span>语速 ' + (v.speed_ratio ?? 1.0) + '</span>' +
          '<span>音量 ' + (v.loudness_ratio ?? 1.0) + '</span>' +
          '<span>音调 ' + (v.pitch_ratio ?? 1.0) + '</span>' +
        '</div>' +
        '<div class="voice-actions">' +
          '<button data-action="select">选用</button>' +
          '<button data-action="retrain" ' + (v.status === "training" ? "disabled" : "") + '>重新训练</button>' +
          '<button data-action="toggle">' + (v.status === "disabled" ? "启用" : "停用") + '</button>' +
          '<button class="btn-delete" data-action="delete">删除</button>' +
        '</div>' +
      '</div>'
    );
  }).join("");
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
      await api("/api/v1/voices/" + id + "/retrain", { method: "POST" });
      toast("已触发重新训练", "info");
      startPolling(id);
      await loadVoices();
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "重新训练";
      toast("重新训练失败", "error", err.message);
    }
    return;
  }
  if (action === "toggle") {
    try {
      const voice = await fetchVoice(id);
      const next = voice.status === "disabled" ? "active" : "disabled";
      await api("/api/v1/voices/" + id, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: next }),
      });
      await loadVoices();
      toast("状态已更新", "success", next === "active" ? "已启用" : "已停用");
    } catch (err) {
      toast("切换状态失败", "error", err.message);
    }
    return;
  }
  if (action === "delete") {
    if (!confirm("确定删除该音色？此操作不可恢复。")) return;
    try {
      await api("/api/v1/voices/" + id, { method: "DELETE" });
      stopPolling(id);
      if (selectedVoiceId === id) selectedVoiceId = null;
      await loadVoices();
      toast("已删除", "success");
    } catch (err) {
      toast("删除失败", "error", err.message);
    }
  }
});

async function fetchVoice(id) {
  try {
    const resp = await api("/api/v1/voices/" + id);
    return await resp.json();
  } catch (err) {
    toast("获取音色详情失败", "error", err.message);
    return null;
  }
}

/* ===================================================== 轮询 */

function startPolling(voiceId) {
  stopPolling(voiceId);
  const iv = setInterval(async () => {
    try {
      const resp = await api("/api/v1/voices/" + voiceId + "/status");
      const { status } = await resp.json();
      const card = document.querySelector('.voice-card[data-id="' + voiceId + '"]');
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
          setRecStatus("训练失败，可点击「重新训练」重试", "error");
          toast("训练失败", "error", "可点击重新训练重试");
        } else {
          setRecStatus("训练完成，音色可用", "success");
          toast("训练完成", "success", "音色已可用");
        }
      }
    } catch (err) {
      stopPolling(voiceId);
    }
  }, POLL_MS);
  pollTimers.set(voiceId, iv);
}

function stopPolling(voiceId) {
  const iv = pollTimers.get(voiceId);
  if (iv) { clearInterval(iv); pollTimers.delete(voiceId); }
}

/* ===================================================== 试听 */

function applyVoiceToSliders(voice) {
  const isLocal = voice.provider === "local";
  els.speedRatio.value = voice.speed_ratio ?? 1.0;
  els.loudnessRatio.value = voice.loudness_ratio ?? 1.0;
  els.pitchRatio.value = voice.pitch_ratio ?? 1.0;
  updateSliderLabels();
  els.loudnessRatio.disabled = isLocal;
  els.pitchRatio.disabled = isLocal;
  els.loudnessRatio.closest(".slider-row").classList.toggle("cloud-only", isLocal);
  els.pitchRatio.closest(".slider-row").classList.toggle("cloud-only", isLocal);
  setPreviewStatus(isLocal ? "已选用本地音色（音量/音调仅云端支持）" : "已选用音色：" + voice.name, "info");
}

function updateSliderLabels() {
  els.speedVal.textContent = Number(els.speedRatio.value).toFixed(2);
  els.loudnessVal.textContent = Number(els.loudnessRatio.value).toFixed(2);
  els.pitchVal.textContent = Number(els.pitchRatio.value).toFixed(2);
}

[els.speedRatio, els.loudnessRatio, els.pitchRatio].forEach((s) =>
  s.addEventListener("input", updateSliderLabels)
);

function updateCharCount() {
  const len = els.previewText.value.length;
  els.charCount.textContent = len;
  els.charCount.style.color = len > 4500 ? "var(--color-warning)" : "var(--color-text-3)";
}
els.previewText.addEventListener("input", updateCharCount);
updateCharCount();

async function previewSpeak() {
  if (!selectedVoiceId) {
    setPreviewStatus("请先在列表中选择一个音色", "error");
    toast("未选择音色", "warning");
    return;
  }
  const text = els.previewText.value.trim();
  if (!text) {
    setPreviewStatus("请输入试听文本", "error");
    toast("文本为空", "warning");
    return;
  }
  const params = {
    speed_ratio: Number(els.speedRatio.value),
    loudness_ratio: Number(els.loudnessRatio.value),
    pitch_ratio: Number(els.pitchRatio.value),
  };
  els.previewBtn.classList.add("loading");
  els.previewBtn.disabled = true;
  setPreviewStatus("合成中...", "info");
  try {
    const resp = await api("/api/v1/tts/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice_id: selectedVoiceId, text, params }),
    });
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    els.player.src = url;
    els.stopBtn.disabled = false;
    setPreviewStatus("合成完成，正在播放", "success");
    toast("合成完成", "success", (blob.size / 1024).toFixed(1) + " KB");
    els.player.play().catch(() => {
      setPreviewStatus("合成完成，点击播放（浏览器阻止了自动播放）", "info");
    });
  } catch (err) {
    setPreviewStatus("合成失败：" + err.message, "error");
    toast("合成失败", "error", err.message);
  } finally {
    els.previewBtn.classList.remove("loading");
    els.previewBtn.disabled = false;
  }
}

els.previewBtn.addEventListener("click", previewSpeak);
els.stopBtn.addEventListener("click", () => {
  els.player.pause();
  els.player.currentTime = 0;
  els.stopBtn.disabled = true;
});

/* ===================================================== 初始化 */

async function initEngines() {
  try {
    const resp = await api("/api/v1/engines");
    const engines = await resp.json();
    const cloudEnabled = engines.cloud && engines.cloud.enabled;
    const localEnabled = engines.local && engines.local.enabled;
    if (!cloudEnabled) {
      els.providerSel.value = "local";
      syncRefTextField();
      toast("云端引擎未配置", "info", "已默认使用本地引擎");
    }
    if (!localEnabled && !cloudEnabled) {
      toast("无可用引擎", "error", "本地与云端均未配置");
    }
  } catch (err) {
    // /engines 端点不可用不算致命错误
  }
}

els.refreshBtn.addEventListener("click", () => {
  loadVoices();
  toast("已刷新", "info", "", 1500);
});

/* ---- 启动 ---- */
syncRefTextField();
updateSliderLabels();
initEngines();
loadVoices();
checkConnection();
setInterval(checkConnection, 30000);
