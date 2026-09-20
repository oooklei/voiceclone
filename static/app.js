(function () {
  "use strict";

  /* ================================================================
   * 声音助手工作台 - 管理页前端（单页 SPA，无构建链）
   * 视图：home / create（5 步向导）/ manage（音色库）
   * ================================================================ */

  // ---------- 小工具 ----------
  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // ---------- 全局状态 ----------
  const State = {
    view: "home",                 // home | create | manage
    voices: [],                   // 音色列表
    selectedVoiceId: null,        // 当前选用音色
    retrainVoiceId: null,         // 重训目标（非空=重训模式，名称锁定）
    // 创建向导状态
    step: 0,                      // 0 命名 / 1 录音 / 2 文稿 / 3 训练 / 4 试听入库
    voiceName: "",
    audioSource: "",              // mic | system | file
    audioBlob: null,
    audioFileName: "",
    refText: "",
    refTextMode: "auto",          // auto | manual
    recSeconds: 0,
    busy: false,                  // 训练/合成中
    previewVoiceId: "",           // 刚训练生成的音色 id（供试听/入库）
    // 录音状态
    isRecording: false,
    mediaRecorder: null,
    activeStream: null,
    timer: null,
    levelAnim: null,
    recordingStopPromise: null,
    recordingStopResolve: function () {},
    // 合成中止
    currentAbortController: null,
    healthTimer: null,
  };

  const STORAGE_KEY = "vc_create_state";

  // ---------- 常用元素引用 ----------
  const els = {
    stopBtn: null,
    previewBtn: null,
    refreshBtn: null,
    createBtn: null,
    micBtn: null,
    sysBtn: null,
    player: null,
    voiceName: null,
    fileInput: null,
    previewText: null,
    refText: null,
  };

  function initEls() {
    els.stopBtn = $("stopBtn");
    els.previewBtn = $("previewBtn");
    els.refreshBtn = $("refreshBtn");
    els.createBtn = $("createBtn");
    els.micBtn = $("micBtn");
    els.sysBtn = $("sysBtn");
    els.player = $("player");
    els.voiceName = $("voiceName");
    els.fileInput = $("fileInput");
    els.previewText = $("previewText");
    els.refText = $("refText");
  }

  // ---------- Tauri 环境检测 ----------
  // Tauri v2 会向所有 webview（含 WebviewUrl::External 直载的外部 URL）注入
  // __TAURI_INTERNALS__，但远程上下文默认未被 capabilities 授权，invoke 一律
  // 失败（曾导致 chat-tool 直载本页时「服务异常 / 音色库为空 / 识别失败」，
  // 且 localStorage['vc_token'] 在 IPC 分支下根本不会被读取）。
  // 故：仅当页面运行在 Tauri 本地源（tauri://localhost / http(s)://tauri.localhost /
  // ipc.localhost）时走 IPC 代理分支；宿主经 http(s) 直载本服务页面时，
  // 一律走浏览器分支 + localStorage['vc_token']（协议 §6，X-Auth-Token）。
  function vcIsTauri() {
    if (typeof window === "undefined" || !window.__TAURI_INTERNALS__) return false;
    const proto = window.location.protocol;
    if (proto === "http:" || proto === "https:") {
      const host = window.location.hostname;
      return host === "tauri.localhost" || host === "ipc.localhost";
    }
    return true;
  }

  // ---------- 网络层 ----------
  // 统一请求入口：浏览器分支直接用 fetch；Tauri 分支经 Rust 侧命令转发。
  async function rawProxy(path, opts) {
    const options = opts || {};
    const method = options.method || "GET";
    const headers = options.headers ? JSON.parse(JSON.stringify(options.headers)) : {};
    const body = options.body;
    const signal = options.signal;

    if (vcIsTauri()) {
      let payload = null;
      let status = 200;
      try {
        if (method === "GET") {
          payload = await window.__TAURI_INTERNALS__.invoke("fetch_get", { url: path });
        } else if (body && typeof body.append === "function") {
          payload = await invokeMultipart(path, body);
        } else {
          const bodyText = typeof body === "string" ? body : JSON.stringify(body || {});
          payload = await window.__TAURI_INTERNALS__.invoke("fetch_post", {
            url: path,
            method: method,
            headers: headers,
            body: bodyText,
          });
        }
        if (signal && signal.aborted) {
          throw new DOMException("Aborted", "AbortError");
        }
        if (payload && typeof payload === "object" && "status" in payload && "data" in payload) {
          status = payload.status || 200;
        }
      } catch (e) {
        if (e && e.name === "AbortError") throw e;
        return {
          ok: false,
          status: 500,
          headers: { get: function () { return "application/json"; } },
          json: async function () { return { detail: String((e && e.message) || e) }; },
          text: async function () { return String((e && e.message) || e); },
        };
      }
      return {
        ok: status >= 200 && status < 300,
        status: status,
        headers: { get: function () { return "application/json"; } },
        json: async function () {
          return payload && payload.data !== undefined ? payload.data : payload;
        },
        text: async function () { return JSON.stringify(payload); },
      };
    }

    // 浏览器分支：协议 §6 约定管理页由 recorder 代开并注入 localStorage['vc_token']，
    // 需对 /api/* 附加 X-Auth-Token（Tauri 分支由 Rust 侧转发时已带 token）。
    if (typeof localStorage !== "undefined" && path.indexOf("/api/") === 0) {
      const tk = localStorage.getItem("vc_token");
      if (tk) headers["X-Auth-Token"] = tk;
    }
    const fetchInit = { method: method, headers: headers };
    if (body !== undefined && body !== null) fetchInit.body = body;
    if (signal) fetchInit.signal = signal;
    return fetch(path, fetchInit);
  }

  async function api(path, options) {
    return rawProxy(path, options);
  }

  // Tauri 上传：原生 FormData 无法跨 IPC，须转成可序列化的
  // { path, filename, data_b64, fields } 后调用 Rust 侧 api_upload 命令。
  async function invokeMultipart(path, form) {
    const fields = {};
    let filename = "audio.webm";
    let dataB64 = "";
    for (const entry of form.entries()) {
      const [name, value] = entry;
      if (value && typeof value === "object" && typeof value.arrayBuffer === "function") {
        // Blob/File：读取为 base64
        const buf = await value.arrayBuffer();
        const bytes = new Uint8Array(buf);
        let binary = "";
        const CHUNK = 0x8000;
        for (let i = 0; i < bytes.length; i += CHUNK) {
          binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
        }
        dataB64 = btoa(binary);
        if (value.name) filename = value.name;
      } else {
        fields[name] = String(value);
      }
    }
    return window.__TAURI_INTERNALS__.invoke("api_upload", {
      path: path,
      filename: filename,
      dataB64: dataB64,
      fields: fields,
    });
  }

  // 音频合成：POST /api/v1/tts/speak，返回 Blob（浏览器）/ 字节（Tauri）
  async function apiAudio(path, payload, signal) {
    if (vcIsTauri()) {
      const bytes = await window.__TAURI_INTERNALS__.invoke("speak", {
        voiceId: payload.voice_id,
        text: payload.text,
      });
      if (signal && signal.aborted) throw new DOMException("Aborted", "AbortError");
      return new Blob([new Uint8Array(bytes)], { type: "audio/wav" });
    }
    const audioHeaders = { "Content-Type": "application/json" };
    if (typeof localStorage !== "undefined" && path.indexOf("/api/") === 0) {
      const tk = localStorage.getItem("vc_token");
      if (tk) audioHeaders["X-Auth-Token"] = tk;
    }
    const r = await fetch(path, {
      method: "POST",
      headers: audioHeaders,
      body: JSON.stringify(payload),
      signal: signal,
    });
    if (!r.ok) {
      let detail = "";
      try {
        const d = await r.json();
        detail = (d && (d.detail || d.message)) || "";
      } catch (err) { /* ignore */ }
      throw new Error(detail || ("HTTP " + r.status));
    }
    return r.blob();
  }

  // ---------- Toast ----------
  function toast(msg, type) {
    const box = $("toastContainer");
    if (!box) return;
    const el = document.createElement("div");
    el.className = "toast " + (type || "info");
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(function () {
      el.classList.add("hide");
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 300);
    }, 2600);
  }

  // ---------- 时间格式化 ----------
  function fmtTime(sec) {
    const s = Math.max(0, Math.floor(sec || 0));
    const m = Math.floor(s / 60);
    const r = s % 60;
    return (m < 10 ? "0" + m : String(m)) + ":" + (r < 10 ? "0" + r : String(r));
  }

  // ------------------------------------------------------------------
  // 音色库
  // ------------------------------------------------------------------

  async function loadVoices() {
    const r = await api("/api/v1/voices");
    // 401/403：token 缺失或不匹配 —— 抛错触发上层「等 token 注入后自愈」，
    // 而不是把音色库静默清空（外部宿主注入晚于页面初始化时的典型症状）
    if (r && (r.status === 401 || r.status === 403)) {
      State.voices = [];
      throw new Error(r.status === 401 ? "未授权：token 缺失或不匹配" : "禁止访问");
    }
    const data = await r.json();
    State.voices = Array.isArray(data) ? data : (data && data.voices ? data.voices : []);
  }

  async function refreshVoices() {
    els.refreshBtn.classList.add("loading");
    els.refreshBtn.disabled = true;
    try {
      await loadVoices();
      renderVoices();
      updateVoiceCount();
      toast("音色库已刷新", "success");
    } catch (err) {
      toast("刷新失败：" + (err.message || err), "error");
    } finally {
      els.refreshBtn.classList.remove("loading");
      els.refreshBtn.disabled = false;
    }
  }

  function statusLabel(status) {
    if (status === "active") return "已就绪";
    if (status === "training") return "训练中";
    if (status === "failed") return "失败";
    return "未入库";
  }

  function genderLabel(g) {
    if (g === "male") return "男";
    if (g === "female") return "女";
    return "未知";
  }

  function ageLabel(a) {
    if (a === "child") return "小孩";
    if (a === "elderly") return "老人";
    return "成人";
  }

  function renderVoices() {
    const list = $("voiceList");
    if (!list) return;
    if (!State.voices || State.voices.length === 0) {
      list.innerHTML =
        '<div class="empty"><span class="empty-text">暂无音色，先创建一个吧</span></div>';
      return;
    }
    let html = "";
    for (let i = 0; i < State.voices.length; i++) {
      const v = State.voices[i] || {};
      const id = v.id || "";
      const speed = v.speed_ratio != null ? v.speed_ratio : 1.0;
      const loudness = v.loudness_ratio != null ? v.loudness_ratio : 1.0;
      const pitch = v.pitch_ratio != null ? v.pitch_ratio : 1.0;
      const nickname = v.nickname ? "（" + escapeHtml(v.nickname) + "）" : "";
      const genderTag = '<tag class="tag-gender">' + genderLabel(v.gender) + "</tag>";
      const ageTag = '<tag class="tag-age">' + ageLabel(v.age_group) + "</tag>";
      html +=
        '<div class="voice-item" data-id="' + escapeHtml(id) + '">' +
          '<div class="voice-name">' + escapeHtml(v.name || "未命名音色") + nickname + "</div>" +
          '<div class="voice-meta">' +
            (v.provider === "local" ? "本地合成" : "云端合成") +
            " · 语速 " + speed + " · 音量 " + loudness + " · 音调 " + pitch +
            " · " + genderTag + " · " + ageTag +
            ' <span class="badge ' + escapeHtml(v.status || "inactive") + '">' + escapeHtml(statusLabel(v.status)) + "</span>" +
          "</div>" +
          '<div class="voice-actions">' +
            '<button type="button" class="btn-text" data-action="preview" data-id="' + escapeHtml(id) + '">试听</button>' +
            '<button type="button" class="btn-text" data-action="edit" data-id="' + escapeHtml(id) + '">编辑</button>' +
            '<button type="button" class="btn-text" data-action="retrain" data-id="' + escapeHtml(id) + '">重训</button>' +
            '<button type="button" class="btn-text danger" data-action="delete" data-id="' + escapeHtml(id) + '">删除</button>' +
          "</div>" +
        "</div>";
    }
    list.innerHTML = html;
  }

  function updateVoiceCount() {
    const el = $("voiceCount");
    if (el) el.textContent = String(State.voices ? State.voices.length : 0);
  }

  async function deleteVoice(id) {
    if (!id) return;
    const v = (State.voices || []).find(function (x) { return x && x.id === id; });
    if (v && !window.confirm("确定删除音色「" + v.name + "」？")) return;
    try {
      const r = await api("/api/v1/voices/" + id, { method: "DELETE" });
      if (!r.ok) {
        const d = await r.json();
        throw new Error((d && d.detail) || ("HTTP " + r.status));
      }
      if (State.selectedVoiceId === id) State.selectedVoiceId = null;
      await loadVoices();
      renderVoices();
      updateVoiceCount();
      toast("已删除", "success");
    } catch (err) {
      toast("删除失败：" + (err.message || err), "error");
    }
  }

  // ------------------------------------------------------------------
  // 音色编辑（改名、昵称、性别、年龄段）
  // ------------------------------------------------------------------

  function editVoice(id) {
    if (!id) return;
    const v = (State.voices || []).find(function (x) { return x && x.id === id; });
    if (!v) return;

    var modal = $("voiceEditModal");
    if (!modal) {
      modal = document.createElement("div");
      modal.id = "voiceEditModal";
      modal.className = "modal-overlay";
      modal.innerHTML =
        '<div class="modal-box">' +
          '<h3 class="modal-title">编辑音色</h3>' +
          '<div class="form-row"><label>名称</label><input type="text" id="editName" maxlength="100"></div>' +
          '<div class="form-row"><label>昵称</label><input type="text" id="editNickname" maxlength="100"></div>' +
          '<div class="form-row"><label>性别</label>' +
            '<select id="editGender">' +
              '<option value="unknown">未知</option>' +
              '<option value="male">男</option>' +
              '<option value="female">女</option>' +
            '</select>' +
          '</div>' +
          '<div class="form-row"><label>年龄段</label>' +
            '<select id="editAge">' +
              '<option value="adult">成人</option>' +
              '<option value="child">小孩</option>' +
              '<option value="elderly">老人</option>' +
            '</select>' +
          '</div>' +
          '<div class="form-row"><label>默认语速</label>' +
            '<input type="number" id="editSpeed" min="0.1" max="2.0" step="0.1" value="1.0">' +
            '<span class="field-hint">0.1 ~ 2.0，合成该音色时的默认播报速度</span>' +
          '</div>' +
          '<div class="btn-group" style="justify-content:flex-end;margin-top:var(--sp-3)">' +
            '<button type="button" class="btn btn-ghost" id="editCancel">取消</button>' +
            '<button type="button" class="btn btn-primary" id="editSave">保存</button>' +
          '</div>' +
        '</div>';
      document.body.appendChild(modal);
      // 点击遮罩关闭
      modal.addEventListener("click", function (e) {
        if (e.target === modal) modal.style.display = "none";
      });
      $("editCancel").addEventListener("click", function () { modal.style.display = "none"; });
      // 保存按钮事件（只绑定一次）
      $("editSave").addEventListener("click", async function () {
        var name = $("editName").value.trim();
        if (!name) { toast("名称不能为空", "warning"); return; }
        var speedVal = parseFloat($("editSpeed").value);
        if (isNaN(speedVal) || speedVal < 0.1 || speedVal > 2.0) {
          toast("默认语速需在 0.1 ~ 2.0 之间", "warning");
          return;
        }
        var payload = {
          name: name,
          nickname: $("editNickname").value.trim(),
          gender: $("editGender").value,
          age_group: $("editAge").value,
          speed_ratio: speedVal,
        };
        try {
          var saveBtn = $("editSave");
          saveBtn.textContent = "保存中…";
          saveBtn.disabled = true;
          const r = await api("/api/v1/voices/" + modal.dataset.voiceId, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          if (!r.ok) {
            const d = await r.json();
            throw new Error((d && d.detail) || ("HTTP " + r.status));
          }
          modal.style.display = "none";
          await loadVoices();
          renderVoices();
          toast("已保存", "success");
        } catch (err) {
          toast("保存失败：" + (err.message || err), "error");
        } finally {
          var saveBtn = $("editSave");
          if (saveBtn) { saveBtn.textContent = "保存"; saveBtn.disabled = false; }
        }
      });
    }

    // 填充当前值
    $("editName").value = v.name || "";
    $("editNickname").value = v.nickname || "";
    $("editGender").value = v.gender || "unknown";
    $("editAge").value = v.age_group || "adult";
    $("editSpeed").value = v.speed_ratio != null ? String(v.speed_ratio) : "1.0";
    modal.dataset.voiceId = v.id;

    // 显示模态框
    modal.style.display = "flex";
  }

  // ------------------------------------------------------------------
  // 创建向导
  // ------------------------------------------------------------------

  // 重置创建向导（进入新建时清空，包括重训标记）
  function resetCreateState() {
    State.step = 0;
    State.voiceName = "";
    State.audioSource = "";
    State.audioBlob = null;
    State.audioFileName = "";
    State.refText = "";
    State.refTextMode = "auto";
    State.recSeconds = 0;
    State.busy = false;
    State.previewVoiceId = "";
    State.retrainVoiceId = null;
    clearCreateState();
  }

  function saveCreateState() {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          step: State.step,
          voiceName: State.voiceName,
          refText: State.refText,
          refTextMode: State.refTextMode,
        })
      );
    } catch (e) { /* 存储不可用时忽略 */ }
  }

  function loadCreateState() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const d = JSON.parse(raw);
      if (d && typeof d === "object") {
        State.step = typeof d.step === "number" ? d.step : 0;
        State.voiceName = typeof d.voiceName === "string" ? d.voiceName : "";
        State.refText = typeof d.refText === "string" ? d.refText : "";
        State.refTextMode = typeof d.refTextMode === "string" ? d.refTextMode : "auto";
      }
    } catch (e) { /* 忽略 */ }
  }

  function clearCreateState() {
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch (e) { /* 忽略 */ }
  }

  function startCreate() {
    stopPlayback();
    resetCreateState();
    State.view = "create";
    render();
  }

  // 重新进入创建（重训）：保留名称但重走录音→文稿→训练→试听→入库全流程
  function startRetrainFlow(id) {
    const voice = (State.voices || []).find(function (v) { return v && v.id === id; });
    if (!voice) return;
    stopPlayback();
    State.retrainVoiceId = id;
    State.voiceName = voice.name || "";
    State.audioSource = "";
    State.audioBlob = null;
    State.audioFileName = "";
    State.refText = "";
    State.refTextMode = "auto";
    State.recSeconds = 0;
    State.busy = false;
    State.previewVoiceId = "";
    State.step = 0;
    clearCreateState();
    State.view = "create";
    render();
    toast("重训音色「" + State.voiceName + "」：名称保持不变，请重新录制音频", "info");
  }

  function syncCreateUi() {
    const retrain = !!State.retrainVoiceId;
    const title = $("wizardTitle");
    if (title) title.textContent = retrain ? "重训音色" : "新建音色";
    if (els.voiceName) {
      els.voiceName.value = State.voiceName;
      els.voiceName.disabled = retrain;
    }
    if (els.refText) {
      els.refText.value = State.refText;
      els.refText.readOnly = State.refTextMode !== "manual";
    }
    updateCharCount();
    const btnEdit = $("btnEditRefText");
    if (btnEdit) btnEdit.style.display = State.refTextMode === "manual" ? "none" : "";
    renderStepUI();
    syncWizardButtons();
  }

  function renderStepUI() {
    const panels = document.querySelectorAll(".step-content");
    for (let i = 0; i < panels.length; i++) {
      const p = panels[i];
      p.classList.toggle("active", Number(p.getAttribute("data-step")) === State.step);
    }
    const steps = document.querySelectorAll(".steps .step");
    for (let j = 0; j < steps.length; j++) {
      steps[j].classList.toggle("active", j === State.step);
      steps[j].classList.toggle("done", j < State.step);
    }
    const trainStatus = $("trainStatus");
    if (trainStatus) {
      if (State.busy) {
        trainStatus.innerHTML =
          '<div class="loading-spinner" style="margin:0 auto var(--sp-4)"></div>' +
          '<p style="font-size:15px;font-weight:500;color:var(--c-text-1)">正在训练音色…</p>' +
          '<p style="font-size:13px;color:var(--c-text-3);margin-top:var(--sp-2)">请稍候，训练完成后将自动进入试听</p>';
      } else if (State.previewVoiceId) {
        trainStatus.innerHTML =
          '<p style="font-size:15px;font-weight:500;color:var(--c-success)">✓ 训练完成</p>' +
          '<p style="font-size:13px;color:var(--c-text-3);margin-top:var(--sp-2)">可进入试听页试听效果</p>';
      } else {
        trainStatus.innerHTML =
          '<p style="font-size:15px;font-weight:500;color:var(--c-text-1)">准备就绪</p>' +
          '<p style="font-size:13px;color:var(--c-text-3);margin-top:var(--sp-2)">点击「下一步：试听」继续</p>';
      }
    }
    updateRecStatus();
  }

  function syncWizardButtons() {
    const s0 = $("step0Next");
    if (s0) s0.disabled = !(els.voiceName ? els.voiceName.value.trim() : "");
    const s1 = $("step1Next");
    if (s1) s1.disabled = !State.audioBlob;
    const s2 = $("step2Next");
    if (s2) s2.disabled = !State.refText.trim();
    const s3 = $("step3Next");
    if (s3) s3.disabled = !State.previewVoiceId || State.busy;
    updateCreateBtn();
  }

  // 入库按钮状态：需要已训练出音色 + 名称 + 音频
  function updateCreateBtn() {
    if (!els.createBtn || !els.voiceName) return;
    const hasName = els.voiceName.value.trim();
    const hasFile = !!(State.audioBlob || (els.fileInput && els.fileInput.files && els.fileInput.files.length > 0));
    const ready = !!State.previewVoiceId && !!hasName && hasFile;
    els.createBtn.disabled = !(ready);
  }

  function updateRecStatus() {
    const hint = $("recStatus");
    if (hint) {
      if (State.isRecording) {
        hint.textContent = "录音中… 点击「停止录音」结束";
      } else if (State.audioBlob) {
        hint.textContent = "✓ 已获取音频：" + (State.audioFileName || "录音") + "（" + (State.audioBlob.size / 1024).toFixed(1) + " KB）";
      } else {
        hint.textContent = "选择录音方式后点击按钮开始（最长 30 秒）";
      }
    }
    const stopBtn = $("btnStopRecording");
    if (stopBtn) stopBtn.style.display = State.isRecording ? "" : "none";
    const timerEl = $("timer");
    if (timerEl) timerEl.textContent = fmtTime(State.recSeconds);
    const fileText = $("fileText");
    if (fileText) fileText.textContent = State.audioFileName || "点击选择音频文件";
  }

  function updateCharCount() {
    const cc = $("charCount");
    if (cc) cc.textContent = String((State.refText || "").length);
  }

  // ------------------------------------------------------------------
  // 录音
  // ------------------------------------------------------------------

  function getBestAudioMime() {
    if (typeof MediaRecorder === "undefined") return "audio/webm";
    const types = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
    for (let i = 0; i < types.length; i++) {
      if (MediaRecorder.isTypeSupported(types[i])) return types[i];
    }
    return "audio/webm";
  }

  function beginRecording(stream) {
    // 预检：MediaRecorder 用纯音频 mime，流中无音频轨时 start() 直接抛引擎错误
    // ("Failed to execute 'start' ... There was an error starting the MediaRecorder")。
    // 系统声音路径常见于分享弹窗未勾选「同时分享系统音频」/宿主未返回回环音频轨，
    // 这里给出明确指引而非原始报错（并清理已获取的轨道，避免泄漏）。
    if (!stream || !stream.getAudioTracks || stream.getAudioTracks().length === 0) {
      try {
        if (stream && stream.getTracks) stream.getTracks().forEach(function (t) { t.stop(); });
      } catch (e) { /* ignore */ }
      State.isRecording = false;
      render();
      toast(
        "无法捕获声音：未获取到音频轨。系统声音请在弹出的分享弹窗中勾选「同时分享系统音频」，或改用麦克风 / 上传音频文件。",
        "error"
      );
      return;
    }
    State.activeStream = stream;
    State.isRecording = true;
    State.audioBlob = null;
    State.audioFileName = "";
    State.audioSource = State.audioSource || "mic";
    State.recSeconds = 0;
    State.mediaRecorder = new MediaRecorder(stream, { mimeType: getBestAudioMime() });
    const chunks = [];

    State.recordingStopPromise = new Promise(function (resolve) {
      State.recordingStopResolve = resolve;
    });

    State.mediaRecorder.ondataavailable = function (e) {
      if (e.data && e.data.size > 0) chunks.push(e.data);
    };

    State.mediaRecorder.onstop = function () {
      State.recordingStopResolve();
      State.recordingStopResolve = function () {};
      const type = State.mediaRecorder && State.mediaRecorder.mimeType ? State.mediaRecorder.mimeType : "audio/webm";
      const blob = new Blob(chunks, { type: type });
      State.isRecording = false;
      State.activeStream = null;
      State.mediaRecorder = null;
      stopTimer();
      stopLevelAnimation();
      if (blob && blob.size > 0) {
        State.audioBlob = blob;
        State.audioFileName = "recording." + (type.indexOf("mp4") >= 0 ? "m4a" : "webm");
        render();
        toast("录音完成：" + (blob.size / 1024).toFixed(1) + " KB", "success");
        if (State.refTextMode === "auto") transcribeAudio(blob);
      } else {
        render();
        toast("录音内容为空，请重试", "warning");
      }
    };

    State.mediaRecorder.onerror = function () {
      State.recordingStopResolve();
      State.recordingStopResolve = function () {};
      State.isRecording = false;
      stopTimer();
      stopLevelAnimation();
      render();
      toast("录音出错", "error");
    };

    State.mediaRecorder.start(250);
    startTimer();
    startLevelAnimation();
    render();
  }

  async function startRecordingMic() {
    if (State.isRecording) return;
    if (State.activeStream) {
      try {
        State.activeStream.getTracks().forEach(function (t) { t.stop(); });
      } catch (e) { /* ignore */ }
      State.activeStream = null;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast("当前环境不支持麦克风录音", "error");
      return;
    }
    try {
      State.audioSource = "mic";
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      beginRecording(stream);
    } catch (e) {
      toast("无法访问麦克风：" + (e.message || e), "error");
    }
  }

  async function startRecordingSystem() {
    if (State.isRecording) return;
    if (vcIsTauri()) {
      toast("正在开启系统声音录制…", "info");
      try {
        await window.__TAURI_INTERNALS__.invoke("start_capture");
        State.audioSource = "system";
        State.isRecording = true;
        startTimer();
        render();
      } catch (e) {
        toast("系统声音录制失败：" + (e.message || e), "error");
        render();
      }
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
      toast("当前环境不支持系统声音录制，请使用麦克风或文件上传", "error");
      return;
    }
    try {
      State.audioSource = "system";
      const stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
      beginRecording(stream);
    } catch (e) {
      toast("无法捕获系统声音：" + (e.message || e), "error");
    }
  }

  function beginRecordingCaptureBlob(b64) {
    try {
      const binary = atob(b64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      const blob = new Blob([bytes], { type: "audio/wav" });
      State.audioBlob = blob;
      State.audioFileName = "system_capture.wav";
      State.isRecording = false;
      render();
      if (State.refTextMode === "auto") transcribeAudio(blob);
    } catch (e) {
      toast("系统声音处理失败：" + (e.message || e), "error");
    }
  }

  async function stopRecording() {
    if (State.mediaRecorder && State.mediaRecorder.state !== "inactive") {
      try { State.mediaRecorder.stop(); } catch (e) { /* ignore */ }
    }
    if (State.activeStream) {
      try {
        State.activeStream.getTracks().forEach(function (t) { t.stop(); });
      } catch (e) { /* ignore */ }
      State.activeStream = null;
    }
    if (vcIsTauri()) {
      try {
        const b64 = await window.__TAURI_INTERNALS__.invoke("stop_capture");
        if (b64) beginRecordingCaptureBlob(b64);
      } catch (e) { /* ignore */ }
    }
    stopTimer();
    stopLevelAnimation();
    State.isRecording = false;
    render();
  }

  async function handleSourceClick(source) {
    if (State.isRecording) return;
    // 切换来源时清空旧结果
    if (State.audioSource && State.audioSource !== source && State.audioBlob) {
      State.audioBlob = null;
      State.audioFileName = "";
      State.refText = "";
      State.refTextMode = "auto";
    }
    if (source === "mic") {
      await startRecordingMic();
    } else if (source === "system") {
      await startRecordingSystem();
    } else if (source === "file") {
      State.audioSource = "file";
      if (els.fileInput) els.fileInput.click();
    }
    render();
  }

  function handleFile(file) {
    if (!file) return;
    const isAudio = (file.type && file.type.indexOf("audio") >= 0) ||
      /\.(wav|mp3|m4a|ogg|flac|webm)$/i.test(file.name || "");
    if (!isAudio) {
      toast("请选择音频文件", "warning");
      return;
    }
    State.audioBlob = file;
    State.audioFileName = file.name || "audio";
    State.audioSource = "file";
    State.refText = "";
    State.recSeconds = 0;
    render();
    toast("已选择音频：" + State.audioFileName, "success");
    if (State.refTextMode === "auto") transcribeAudio(file);
  }

  function stopTimer() {
    if (State.timer) {
      clearInterval(State.timer);
      State.timer = null;
    }
  }

  function startTimer() {
    stopTimer();
    State.recSeconds = 0;
    const timerEl = $("timer");
    if (timerEl) timerEl.textContent = fmtTime(0);
    State.timer = setInterval(function () {
      State.recSeconds += 1;
      const t = $("timer");
      if (t) t.textContent = fmtTime(State.recSeconds);
      if (State.recSeconds >= 30) {
        if (State.isRecording) stopRecording();
      }
    }, 1000);
  }

  function stopLevelAnimation() {
    if (State.levelAnim) {
      cancelAnimationFrame(State.levelAnim);
      State.levelAnim = null;
    }
    const bar = $("recLevels");
    if (bar && bar.children) {
      for (let i = 0; i < bar.children.length; i++) {
        const c = bar.children[i];
        if (c.style) c.style.height = "";
      }
    }
  }

  function startLevelAnimation() {
    stopLevelAnimation();
    const bar = $("recLevels");
    if (!bar) return;
    const levels = document.createDocumentFragment();
    for (let i = 0; i < 24; i++) {
      const span = document.createElement("span");
      levels.appendChild(span);
    }
    bar.innerHTML = "";
    bar.appendChild(levels);
    const update = function () {
      for (let i = 0; i < bar.children.length; i++) {
        const c = bar.children[i];
        if (c.style) c.style.height = (15 + Math.random() * 85).toFixed(0) + "%";
      }
      State.levelAnim = requestAnimationFrame(update);
    };
    update();
  }

  // ------------------------------------------------------------------
  // 文字稿（ASR：繁→简后端已处理）
  // ------------------------------------------------------------------

  async function transcribeAudio(blob) {
    if (!blob) return;
    if (blob.size === 0) {
      toast("音频为空，无法识别", "warning");
      return;
    }
    const statusEl = $("refTextStatus");
    if (statusEl) statusEl.textContent = "正在识别";
    try {
      const fd = new FormData();
      fd.append("file", blob, State.audioFileName || "audio.webm");
      const r = await api("/api/v1/asr", { method: "POST", body: fd });
      const data = await r.json();
      const text = (data && (data.text || data.result)) || "";
      State.refText = text;
      State.refTextMode = "auto";
      if (els.refText) {
        els.refText.value = text;
        els.refText.readOnly = true;
      }
      if (statusEl) statusEl.textContent = text ? "✓ 已自动识别" : "未识别到文字，可点击「编辑文稿」手动输入";
      updateCharCount();
      syncWizardButtons();
      if (!text) toast("未识别到文字，请手动输入", "warning");
    } catch (err) {
      if (statusEl) statusEl.textContent = "识别失败，可点击「编辑文稿」手动输入";
      toast("识别失败：" + (err.message || err), "error");
    }
  }

  // ------------------------------------------------------------------
  // 训练
  // ------------------------------------------------------------------

  async function startTraining() {
    if (State.busy) return;
    if (!els.refText || !els.refText.value.trim()) {
      toast("请先填写参考音频文字稿", "warning");
      return;
    }
    State.refText = els.refText.value.trim();
    if (!State.audioBlob) {
      toast("请先录制或上传音频", "warning");
      return;
    }
    State.busy = true;
    render();
    try {
      const fd = new FormData();
      fd.append("reference_text", State.refText);
      let url = "/api/v1/voices";
      if (State.retrainVoiceId) {
        url = "/api/v1/voices/" + State.retrainVoiceId + "/retrain";
        fd.append("file", State.audioBlob, State.audioFileName || "audio.webm");
      } else {
        fd.append("name", State.voiceName.trim());
        fd.append("provider", "local");
        fd.append("file", State.audioBlob, State.audioFileName || "audio.webm");
      }
      const r = await api(url, { method: "POST", body: fd });
      const data = await r.json();
      if (!r.ok) {
        throw new Error((data && data.detail) || ("HTTP " + r.status));
      }
      State.previewVoiceId = (data && data.id) || "";
      State.busy = false;
      State.step = 4;
      clearCreateState();
      render();
      toast(State.retrainVoiceId ? "重训完成，请试听后入库" : "创建完成，请试听后入库", "success");
    } catch (err) {
      State.busy = false;
      render();
      toast("训练失败：" + (err.message || err), "error");
    }
  }

  // ------------------------------------------------------------------
  // 试听 / 播放
  // ------------------------------------------------------------------

  // 停止当前播放并复位 UI
  function stopPlayback() {
    if (State.currentAbortController) {
      try { State.currentAbortController.abort(); } catch (e) { /* ignore */ }
      State.currentAbortController = null;
    }
    if (els.player) {
      els.player.pause();
      els.player.currentTime = 0;
    }
    if (els.stopBtn) {
      els.stopBtn.disabled = true;
    }
    if (els.previewBtn) {
      els.previewBtn.textContent = "▶ 试听";
      els.previewBtn.classList.remove("loading");
      els.previewBtn.disabled = false;
    }
  }

  // 播放器事件：ended/play/pause 与停止按钮状态联动
  function setupPlayerEvents() {
    if (!els.player || !els.stopBtn) return;
    els.player.addEventListener("ended", function () {
      els.stopBtn.disabled = true;
    });
    els.player.addEventListener("play", function () {
      els.stopBtn.disabled = false;
    });
    els.player.addEventListener("pause", function () {
      // 暂停时保持停止按钮可用，停止按钮统一复位
    });
    els.stopBtn.addEventListener("click", function () {
      stopPlayback();
    });
  }

  async function previewSpeak() {
    const voiceId = State.view === "create" ? State.previewVoiceId : State.selectedVoiceId;
    const text = (els.previewText && els.previewText.value) || State.refText || "";
    if (!voiceId) {
      toast("请先在管理页选用音色，或完成训练后试听", "warning");
      return;
    }
    if (!text.trim()) {
      toast("请先输入试听文本", "warning");
      return;
    }
    stopPlayback();
    els.player.pause();
    els.stopBtn.disabled = true;
    const controller = new AbortController();
    State.currentAbortController = controller;
    const signal = State.currentAbortController.signal;
    if (els.previewBtn) {
      els.previewBtn.textContent = "合成中…";
      els.previewBtn.classList.add("loading");
      els.previewBtn.disabled = true;
    }
    let timedOut = false;
    const watchdog = setTimeout(function () {
      if (State.currentAbortController === controller) {
        timedOut = true;
        State.currentAbortController.abort();
      }
    }, 120000);
    try {
      const blob = await apiAudio("/api/v1/tts/speak", { voice_id: voiceId, text: text.trim() }, signal);
      clearTimeout(watchdog);
      if (signal.aborted) return;
      const url = URL.createObjectURL(blob);
      els.player.src = url;
      els.player.onended = function () {
        URL.revokeObjectURL(url);
        stopPlayback();
      };
      await els.player.play();
    } catch (e) {
      clearTimeout(watchdog);
      if (e && e.name === "AbortError") {
        if (!timedOut) toast("已取消合成", "info");
        else toast("合成超时，已停止", "warning");
      } else {
        toast("试听失败：" + (e.message || e), "error");
      }
    } finally {
      if (els.previewBtn) {
        els.previewBtn.textContent = "▶ 试听";
        els.previewBtn.classList.remove("loading");
        els.previewBtn.disabled = false;
      }
    }
  }

  // 管理页行内试听
  async function previewVoiceById(id) {
    if (!id) return;
    stopPlayback();
    const text = (els.previewText && els.previewText.value) || "你好，我是你的专属声音。";
    if (!text.trim()) {
      toast("请先输入试听文本", "warning");
      return;
    }
    // 找到对应的列表按钮并显示加载状态
    var btn = document.querySelector('.voice-item[data-id="' + id + '"] [data-action="preview"]');
    if (btn) { btn.textContent = "合成中…"; btn.disabled = true; }
    const controller = new AbortController();
    State.currentAbortController = controller;
    const signal = State.currentAbortController.signal;
    if (els.previewBtn) {
      els.previewBtn.textContent = "合成中…";
      els.previewBtn.classList.add("loading");
      els.previewBtn.disabled = true;
    }
    let timedOut = false;
    const watchdog = setTimeout(function () {
      if (State.currentAbortController === controller) {
        timedOut = true;
        State.currentAbortController.abort();
      }
    }, 120000);
    try {
      const blob = await apiAudio("/api/v1/tts/speak", { voice_id: id, text: text.trim() }, signal);
      clearTimeout(watchdog);
      if (signal.aborted) return;
      const url = URL.createObjectURL(blob);
      els.player.src = url;
      els.player.onended = function () {
        URL.revokeObjectURL(url);
        stopPlayback();
      };
      await els.player.play();
    } catch (e) {
      clearTimeout(watchdog);
      if (e && e.name === "AbortError") {
        if (!timedOut) toast("已取消合成", "info");
        else toast("合成超时，已停止", "warning");
      } else {
        toast("试听失败：" + (e.message || e), "error");
      }
    } finally {
      if (els.previewBtn) {
        els.previewBtn.textContent = "▶ 试听";
        els.previewBtn.classList.remove("loading");
        els.previewBtn.disabled = false;
      }
      if (btn) { btn.textContent = "试听"; btn.disabled = false; }
    }
  }

  // ------------------------------------------------------------------
  // 入库（PATCH status=active）
  // ------------------------------------------------------------------

  async function saveToLibrary() {
    if (!State.previewVoiceId) {
      toast("请先完成训练生成音色", "warning");
      return;
    }
    els.createBtn.classList.add("loading");
    els.createBtn.disabled = true;
    try {
      const r = await api("/api/v1/voices/" + State.previewVoiceId, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "active" }),
      });
      if (!r.ok) {
        const d = await r.json();
        throw new Error((d && d.detail) || ("HTTP " + r.status));
      }
      State.selectedVoiceId = State.previewVoiceId;
      State.retrainVoiceId = null;
      State.previewVoiceId = "";
      clearCreateState();
      toast("已入库到音色库", "success");
      State.view = "manage";
      await loadVoices();
      render();
    } catch (err) {
      toast("入库失败：" + (err.message || err), "error");
    } finally {
      els.createBtn.classList.remove("loading");
      els.createBtn.disabled = false;
      updateCreateBtn();
    }
  }

  // ------------------------------------------------------------------
  // 服务状态
  // ------------------------------------------------------------------

  async function checkStatus() {
    const pill = $("statusPill");
    const text = $("statusText");
    try {
      const r = await api("/health");
      const data = await r.json();
      const ok = !!(data && data.status === "ok");
      if (pill) pill.className = ok ? "status-pill online" : "status-pill offline";
      if (text) text.textContent = ok ? "服务正常" : "服务异常";
    } catch (e) {
      if (pill) pill.className = "status-pill offline";
      if (text) text.textContent = "服务不可用";
    }
  }

  function startHealthCheck() {
    checkStatus();
    if (State.healthTimer) clearInterval(State.healthTimer);
    State.healthTimer = setInterval(checkStatus, 60000);
  }

  // ------------------------------------------------------------------
  // 渲染
  // ------------------------------------------------------------------

  function render() {
    const views = [
      { view: "home", el: $("viewHome") },
      { view: "create", el: $("viewCreate") },
      { view: "manage", el: $("viewManage") },
    ];
    for (let i = 0; i < views.length; i++) {
      if (views[i].el) views[i].el.classList.toggle("active", State.view === views[i].view);
    }
    const navItems = document.querySelectorAll(".nav-item[data-view]");
    for (let j = 0; j < navItems.length; j++) {
      navItems[j].classList.toggle("active", navItems[j].getAttribute("data-view") === State.view);
    }
    if (State.view === "manage") {
      renderVoices();
      updateVoiceCount();
    } else if (State.view === "create") {
      syncCreateUi();
    }
  }

  function navigate(view) {
    if (view === "create") {
      startCreate();
      return;
    }
    if (view === "manage") {
      stopPlayback();
      State.view = "manage";
      render();
      loadVoices()
        .then(function () {
          renderVoices();
          updateVoiceCount();
        })
        .catch(function (err) {
          toast("加载音色失败：" + (err.message || err), "error");
        });
      return;
    }
    stopAllMedia();
    State.view = "home";
    render();
  }

  function stopAllMedia() {
    stopPlayback();
    if (State.isRecording) {
      stopRecording();
    }
  }

  // ------------------------------------------------------------------
  // 事件绑定
  // ------------------------------------------------------------------

  function bindEvents() {
    // 顶部导航
    const navItems = document.querySelectorAll(".nav-item[data-view]");
    for (let i = 0; i < navItems.length; i++) {
      navItems[i].addEventListener("click", function () {
        navigate(navItems[i].getAttribute("data-view"));
      });
    }

    // 全局 data-action 委托（首页卡片 / 向导步骤 / 音色行操作）
    document.addEventListener("click", function (e) {
      const t = e.target;
      const el = t && t.closest ? t.closest("[data-action]") : null;
      if (!el) return;
      const action = el.getAttribute("data-action");
      const id = el.getAttribute("data-id");
      if (action === "goCreate") {
        startCreate();
      } else if (action === "goManage") {
        navigate("manage");
      } else if (action === "goHome") {
        stopAllMedia();
        State.view = "home";
        render();
      } else if (action === "stepBack") {
        if (State.step > 0) {
          State.step -= 1;
          State.busy = false;
          saveCreateState();
          render();
        }
      } else if (action === "preview") {
        previewVoiceById(id);
      } else if (action === "edit") {
        editVoice(id);
      } else if (action === "delete") {
        deleteVoice(id);
      } else if (action === "retrain") {
        startRetrainFlow(id);
      }
    });

    // 录音按钮（点击时若在录音则先停止并等待结束）
    els.micBtn.addEventListener("click", async function () {
      if (State.isRecording) {
        await stopRecording();
        if (State.recordingStopPromise) await State.recordingStopPromise;
        return;
      }
      await handleSourceClick("mic");
    });

    els.sysBtn.addEventListener("click", async function () {
      if (State.isRecording) {
        await stopRecording();
        if (State.recordingStopPromise) await State.recordingStopPromise;
        return;
      }
      await handleSourceClick("system");
    });

    const fileBtn = $("fileBtn");
    if (fileBtn) {
      fileBtn.addEventListener("click", function () {
        handleSourceClick("file");
      });
    }

    const fileDrop = $("fileDrop");
    if (fileDrop) {
      fileDrop.addEventListener("click", function () {
        if (els.fileInput) els.fileInput.click();
      });
      fileDrop.addEventListener("dragover", function (e) {
        e.preventDefault();
        fileDrop.classList.add("dragover");
      });
      fileDrop.addEventListener("dragleave", function () {
        fileDrop.classList.remove("dragover");
      });
      fileDrop.addEventListener("drop", function (e) {
        e.preventDefault();
        fileDrop.classList.remove("dragover");
        const files = e.dataTransfer && e.dataTransfer.files;
        if (files && files.length > 0) handleFile(files[0]);
      });
    }

    if (els.fileInput) {
      els.fileInput.addEventListener("change", function (e) {
        const files = e.target.files;
        if (files && files.length > 0) handleFile(files[0]);
      });
    }

    // 名称输入
    if (els.voiceName) {
      els.voiceName.addEventListener("input", function () {
        State.voiceName = els.voiceName.value;
        syncWizardButtons();
      });
      els.voiceName.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
          const s0 = $("step0Next");
          if (s0 && !s0.disabled) s0.click();
        }
      });
    }

    // 文稿输入
    if (els.refText) {
      els.refText.addEventListener("input", function () {
        State.refText = els.refText.value;
        State.refTextMode = "manual";
        updateCharCount();
        syncWizardButtons();
      });
    }

    const btnEditRef = $("btnEditRefText");
    if (btnEditRef) {
      btnEditRef.addEventListener("click", function () {
        State.refTextMode = "manual";
        if (els.refText) {
          els.refText.readOnly = false;
          els.refText.focus();
        }
        const btn = $("btnEditRefText");
        if (btn) btn.style.display = "none";
        toast("已切换为手动编辑", "info");
      });
    }

    const btnStopRec = $("btnStopRecording");
    if (btnStopRec) {
      btnStopRec.addEventListener("click", function () {
        stopRecording();
      });
    }

    // 向导下一步
    const s0 = $("step0Next");
    if (s0) {
      s0.addEventListener("click", function () {
        if (!els.voiceName || !els.voiceName.value.trim()) {
          toast("请先为音色命名", "warning");
          return;
        }
        State.voiceName = els.voiceName.value.trim();
        State.step = 1;
        saveCreateState();
        render();
      });
    }

    const s1 = $("step1Next");
    if (s1) {
      s1.addEventListener("click", function () {
        if (!State.audioBlob) {
          toast("请先录制或上传音频", "warning");
          return;
        }
        State.step = 2;
        saveCreateState();
        render();
      });
    }

    const s2 = $("step2Next");
    if (s2) {
      s2.addEventListener("click", function () {
        if (!els.refText || !els.refText.value.trim()) {
          toast("请先填写参考音频文字稿", "warning");
          return;
        }
        State.refText = els.refText.value.trim();
        State.step = 3;
        saveCreateState();
        render();
        startTraining();
      });
    }

    const s3 = $("step3Next");
    if (s3) {
      s3.addEventListener("click", function () {
        if (State.busy || !State.previewVoiceId) return;
        State.step = 4;
        render();
      });
    }

    // 试听 / 停止 / 重训 / 入库 / 刷新
    if (els.previewBtn) els.previewBtn.addEventListener("click", previewSpeak);
    if (els.stopBtn) els.stopBtn.addEventListener("click", stopPlayback);

    const btnRetrain = $("btnRetrain");
    if (btnRetrain) {
      btnRetrain.addEventListener("click", function () {
        // 重新训练：回到录音步骤，保留名称，重新走全流程
        State.step = 1;
        State.audioBlob = null;
        State.audioFileName = "";
        State.refText = "";
        State.refTextMode = "auto";
        State.recSeconds = 0;
        State.previewVoiceId = "";
        render();
        toast("请重新录制音频", "info");
      });
    }

    if (els.createBtn) els.createBtn.addEventListener("click", saveToLibrary);
    if (els.refreshBtn) els.refreshBtn.addEventListener("click", refreshVoices);
  }

  // ------------------------------------------------------------------
  // 初始化
  // ------------------------------------------------------------------

  function init() {
    initEls();
    bindEvents();
    setupPlayerEvents();
    startHealthCheck();
    loadVoices()
      .then(function () {
        renderVoices();
        updateVoiceCount();
      })
      .catch(function () {
        // 首次加载失败：若因 token 尚未注入（宿主在页面加载后才写入
        // localStorage['vc_token']），轮询等待注入完成后自动恢复，
        // 消化各宿主注入时序差异，避免首屏「音色库为空 / 服务异常」。
        scheduleTokenRecovery();
      });
    render();
  }

  // 自愈：浏览器分支下首次 loadVoices 因无 token 失败时，轮询等待宿主注入
  // localStorage['vc_token']（300ms × 40 ≈ 12s 上限），出现后重载音色库并
  // 刷新服务状态。已带 token 仍失败视为服务侧问题，不轮询。
  function scheduleTokenRecovery() {
    if (vcIsTauri() || typeof localStorage === "undefined") return;
    if (localStorage.getItem("vc_token")) return;
    let tries = 0;
    const timer = setInterval(function () {
      tries += 1;
      let tk = null;
      try { tk = localStorage.getItem("vc_token"); } catch (e) { /* ignore */ }
      if (tk) {
        clearInterval(timer);
        loadVoices()
          .then(function () {
            renderVoices();
            updateVoiceCount();
          })
          .catch(function () { /* 仍失败：进入音色库时会再次加载 */ });
        checkStatus();
      } else if (tries >= 40) {
        clearInterval(timer);
      }
    }, 300);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();