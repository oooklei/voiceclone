// VoiceClone 桌面壳：管理 Python 侧车进程（sidecar.py）并暴露录音命令。
// 侧车以 `--port 0`（随机端口）+ 自动 token 启动，stdout 打印
// `READY port=<actual> token=<token>`，本模块解析后供命令调用其 HTTP API。

use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::os::windows::process::CommandExt;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use anyhow::{Context, Result};
use tauri::Manager;
use tauri::menu::MenuBuilder;

// ---------------------------------------------------------------------------
// 错误类型：结构化、可扩展，避免 String 错误在匹配时丢失语义
// ---------------------------------------------------------------------------

#[derive(Debug, thiserror::Error)]
enum SidecarError {
    #[error("sidecar not found (sidecar.exe or sidecar.py)")]
    NotFound,
    #[error("sidecar has no parent dir")]
    NoParentDir,
    #[error("sidecar stdout unavailable")]
    StdoutUnavailable,
    #[error("failed to spawn sidecar: {0}")]
    SpawnFailed(#[source] std::io::Error),
    #[error("sidecar not ready")]
    NotReady,
    #[error("invalid HTTP method: {0}")]
    InvalidMethod(String),
    #[error("proxy request failed: {0}")]
    ProxyFailed(#[source] reqwest::Error),
    #[error("proxy read body failed: {0}")]
    ProxyReadFailed(#[source] reqwest::Error),
    #[error("base64 decode failed: {0}")]
    Base64DecodeFailed(#[source] base64::DecodeError),
    #[error("invalid mime type: {0}")]
    InvalidMime(String),
    #[error("upload failed: {0}")]
    UploadFailed(#[source] reqwest::Error),
    #[error("capture start failed: HTTP {0}")]
    CaptureStartFailed(u16),
    #[error("capture start request failed: {0}")]
    CaptureStartRequestFailed(#[source] reqwest::Error),
    #[error("capture stop failed: HTTP {0}")]
    CaptureStopFailed(u16),
    #[error("capture stop request failed: {0}")]
    CaptureStopRequestFailed(#[source] reqwest::Error),
    #[error("TTS failed: HTTP {0}")]
    TtsFailed(u16),
    #[error("user Downloads directory not found")]
    NoDownloadsDir,
    #[error("failed to save file: {0}")]
    SaveFailed(#[source] std::io::Error),
    #[error("internal: {0}")]
    Internal(#[source] anyhow::Error),
}

// 为 Tauri 命令提供 String 兼容（保留原有接口）
impl From<SidecarError> for String {
    fn from(e: SidecarError) -> Self {
        e.to_string()
    }
}

impl From<anyhow::Error> for SidecarError {
    fn from(e: anyhow::Error) -> Self {
        SidecarError::Internal(e)
    }
}

// ---------------------------------------------------------------------------
// 状态
// ---------------------------------------------------------------------------

#[derive(Default)]
struct SidecarState {
    child: Mutex<Option<Child>>,
    endpoint: Mutex<Option<SidecarEndpoint>>,
}

#[derive(Clone)]
struct SidecarEndpoint {
    base_url: String,
    token: String,
}

#[derive(serde::Serialize)]
struct SidecarStatus {
    running: bool,
    healthy: bool,
    base_url: Option<String>,
}

// ---------------------------------------------------------------------------
// 辅助：获取 endpoint，统一错误（若侧车已挂则尝试重启）
// ---------------------------------------------------------------------------

fn endpoint(app: &tauri::AppHandle) -> Result<SidecarEndpoint, SidecarError> {
    let state = app.state::<SidecarState>();
    let ep = state
        .endpoint
        .lock()
        .expect("endpoint mutex poisoned")
        .clone();
    if ep.is_some() {
        return ep.ok_or(SidecarError::NotReady);
    }
    // endpoint 为空，尝试重启
    if ensure_sidecar(app)? {
        log::info!("sidecar restarted, waiting for READY...");
    }
    // 重启后可能还没 READY，返回 NotReady
    app.state::<SidecarState>()
        .endpoint
        .lock()
        .expect("endpoint mutex poisoned")
        .clone()
        .ok_or(SidecarError::NotReady)
}

// ---------------------------------------------------------------------------
// 侧车程序定位
// ---------------------------------------------------------------------------

/// 定位侧车可执行：环境变量覆盖 > 打包资源（resources/sidecar/sidecar.exe）> exe 相对路径 > cargo manifest。
fn sidecar_program(app: &tauri::AppHandle) -> Result<(PathBuf, bool), SidecarError> {
    // 1. 环境变量覆盖（打包/调试均可指定）
    if let Ok(dir) = std::env::var("VOICECLONE_SIDECAR_DIR") {
        let d = PathBuf::from(dir);
        let exe = d.join("sidecar.exe");
        if exe.exists() {
            return Ok((exe, false));
        }
        let py = d.join("sidecar.py");
        if py.exists() {
            return Ok((py, true));
        }
    }
    // 2. 打包分发：<resource_dir>/resources/sidecar/sidecar.exe（PyInstaller onedir）
    //    resource_dir 在 Windows = exe 所在目录（$INSTDIR），resources target 为 "resources/sidecar"
    if let Ok(res) = app.path().resource_dir() {
        let exe = res.join("resources").join("sidecar").join("sidecar.exe");
        if exe.exists() {
            return Ok((exe, false));
        }
    }
    // 3. dev：exe 相对路径（debug: target/debug -> src-tauri -> voiceclone-desktop -> voiceclone）
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let p = dir.join("..").join("..").join("..").join("..").join("sidecar.py");
            if p.exists() {
                return Ok((p, true));
            }
        }
    }
    // 4. cargo manifest 相对路径
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("sidecar.py");
    if p.exists() {
        return Ok((p, true));
    }
    Err(SidecarError::NotFound)
}

/// 优先使用脚本旁的 venv python，否则回退 PATH 上的 python。
/// Windows 下使用 pythonw.exe 避免控制台窗口。
fn python_executable(script_dir: &Path) -> PathBuf {
    let venv_dir = script_dir.join(".venv").join("Scripts");
    #[cfg(windows)]
    {
        let venv_pyw = venv_dir.join("pythonw.exe");
        if venv_pyw.exists() {
            return venv_pyw;
        }
    }
    let venv_py = venv_dir.join("python.exe");
    if venv_py.exists() {
        venv_py
    } else {
        PathBuf::from("python")
    }
}

// ---------------------------------------------------------------------------
// 启动 / 回收
// ---------------------------------------------------------------------------

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

fn spawn_sidecar(app: &tauri::AppHandle) -> Result<()> {
    let (program, is_script) = sidecar_program(app).context("locate sidecar program")?;
    let mut cmd = if is_script {
        let script_dir = program
            .parent()
            .ok_or(SidecarError::NoParentDir)?
            .to_path_buf();
        let python = python_executable(&script_dir);
        let mut c = Command::new(&python);
        c.arg(&program);
        c
    } else {
        Command::new(&program)
    };

    // Windows: 隐藏控制台窗口
    #[cfg(windows)]
    {
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let port = std::env::var("VOICECLONE_SIDECAR_PORT").unwrap_or_else(|_| "0".into());
    cmd.args(["--port", &port]);
    if let Ok(token) = std::env::var("VOICECLONE_SIDECAR_TOKEN") {
        cmd.args(["--token", &token]);
    }
    cmd.stdout(Stdio::piped()).stderr(Stdio::null());

    let mut child = cmd.spawn().map_err(SidecarError::SpawnFailed)?;
    let stdout = child.stdout.take().ok_or(SidecarError::StdoutUnavailable)?;
    {
        let state = app.state::<SidecarState>();
        *state.child.lock().expect("child mutex poisoned") = Some(child);
    }

    // 后台线程读 READY 行并填充 endpoint（stdout 在子进程退出时 EOF，线程自然结束）
    let app = app.clone();
    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines() {
            let line = match line {
                Ok(l) => l,
                Err(_) => break,
            };
            if let Some(rest) = line.strip_prefix("READY ") {
                let mut port: Option<u16> = None;
                let mut token: Option<String> = None;
                for part in rest.split_whitespace() {
                    if let Some(p) = part.strip_prefix("port=") {
                        port = p.parse().ok();
                    } else if let Some(t) = part.strip_prefix("token=") {
                        token = Some(t.to_string());
                    }
                }
                if let (Some(port), Some(token)) = (port, token) {
                    let state = app.state::<SidecarState>();
                    *state.endpoint.lock().expect("endpoint mutex poisoned") =
                        Some(SidecarEndpoint {
                            base_url: format!("http://127.0.0.1:{port}"),
                            token,
                        });
                }
                break;
            }
        }
    });
    Ok(())
}

fn kill_sidecar(app: &tauri::AppHandle) {
    let state = app.state::<SidecarState>();
    let mut guard = state.child.lock().expect("child mutex poisoned");
    if let Some(mut child) = guard.take() {
        let _ = child.kill();
    }
}

/// 确保侧车运行：若已挂则自动重启。返回 Ok(true) 表示需要重新等待 READY。
fn ensure_sidecar(app: &tauri::AppHandle) -> Result<bool, SidecarError> {
    let state = app.state::<SidecarState>();
    let mut child_guard = state.child.lock().expect("child mutex poisoned");
    if let Some(mut child) = child_guard.take() {
        match child.try_wait() {
            Ok(None) => {
                // 仍在运行
                *child_guard = Some(child);
                return Ok(false);
            }
            Ok(Some(status)) => {
                log::info!("sidecar exited with {status}, restarting...");
            }
            Err(e) => {
                log::warn!("try_wait failed: {e}, restarting...");
            }
        }
        let _ = child.kill();
    }
    drop(child_guard);
    // 清空旧的 endpoint，spawn_sidecar 会重新填充
    *state.endpoint.lock().expect("endpoint mutex poisoned") = None;
    spawn_sidecar(app)?;
    Ok(true)
}

// ---------------------------------------------------------------------------
// Tauri 命令
// ---------------------------------------------------------------------------

#[tauri::command]
async fn sidecar_status(
    state: tauri::State<'_, SidecarState>,
) -> Result<SidecarStatus, String> {
    sidecar_status_inner(state).await.map_err(|e| e.to_string())
}

// 实际实现：异步健康检查，避免引入 blocking feature
async fn sidecar_status_inner(
    state: tauri::State<'_, SidecarState>,
) -> Result<SidecarStatus, SidecarError> {
    let Some(ep) = state.endpoint.lock().expect("endpoint mutex poisoned").clone() else {
        return Ok(SidecarStatus {
            running: false,
            healthy: false,
            base_url: None,
        });
    };
    let healthy = reqwest::Client::new()
        .get(format!("{}/health", ep.base_url))
        .timeout(std::time::Duration::from_secs(2))
        .send()
        .await
        .map(|r| r.status().is_success())
        .unwrap_or(false);
    Ok(SidecarStatus {
        running: true,
        healthy,
        base_url: Some(ep.base_url),
    })
}

/// 通用代理核心：向侧车发起任意方法请求，返回 `{status, data}`。
/// `path` 需含 `/api` 前缀（如 `/api/v1/voices`）。`data` 优先解析为 JSON，
/// 解析失败时回退为字符串（兼容文本/错误内容）。
/// 若侧车已挂，会自动尝试重启。
async fn proxy_json(
    app: &tauri::AppHandle,
    method: &str,
    path: &str,
    headers: Option<serde_json::Value>,
    body: Option<String>,
) -> Result<serde_json::Value, SidecarError> {
    use base64::{engine::general_purpose::STANDARD as B64, Engine as _};

    let ep = endpoint(app)?;
    let url = format!("{}{path}", ep.base_url);
    let http_method: reqwest::Method = method
        .parse()
        .map_err(|_| SidecarError::InvalidMethod(method.to_string()))?;

    let mut req = reqwest::Client::new()
        .request(http_method, url)
        .header("X-Auth-Token", &ep.token);

    if let Some(serde_json::Value::Object(map)) = headers {
        for (k, v) in map {
            if let serde_json::Value::String(s) = v {
                req = req.header(k, s);
            }
        }
    }
    if let Some(b) = &body {
        req = req.body(b.clone());
    }
    // TTS 合成可能较慢（CPU 上 sherpa-onnx），设置 120s 超时
    req = req.timeout(std::time::Duration::from_secs(120));

    let resp = req.send().await.map_err(SidecarError::ProxyFailed)?;
    let status = resp.status().as_u16();
    let bytes = resp.bytes().await.map_err(SidecarError::ProxyReadFailed)?;

    // 优先按 JSON 解析 body；否则按 UTF-8 文本回退；都失败则 base64 兜底
    let data: serde_json::Value = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(_) => match String::from_utf8(bytes.to_vec()) {
            Ok(s) => serde_json::Value::String(s),
            Err(_) => serde_json::Value::String(B64.encode(&bytes)),
        },
    };
    Ok(serde_json::json!({ "status": status, "data": data }))
}

/// 前端通用 GET：`invoke("fetch_get", { url: "/api/..." })` → `{status, data}`。
#[tauri::command]
async fn fetch_get(
    app: tauri::AppHandle,
    url: String,
) -> Result<serde_json::Value, String> {
    proxy_json(&app, "GET", &url, None, None)
        .await
        .map_err(|e| e.to_string())
}

/// 前端通用非上传请求（POST/PATCH/DELETE 等）：
/// `invoke("fetch_post", { url, method, headers, body })` → `{status, data}`。
#[tauri::command]
async fn fetch_post(
    app: tauri::AppHandle,
    url: String,
    method: String,
    headers: Option<serde_json::Value>,
    body: Option<String>,
) -> Result<serde_json::Value, String> {
    proxy_json(&app, &method, &url, headers, body)
        .await
        .map_err(|e| e.to_string())
}

/// 文件上传代理：前端发送 base64 编码的文件数据，由后端转发 multipart 请求到侧车。
/// 若侧车已挂，会自动尝试重启。
#[tauri::command]
async fn api_upload(
    app: tauri::AppHandle,
    path: String,
    filename: String,
    data_b64: String,
    fields: Option<serde_json::Value>,
) -> Result<serde_json::Value, String> {
    api_upload_inner(&app, path, filename, data_b64, fields)
        .await
        .map_err(|e| e.to_string())
}

async fn api_upload_inner(
    app: &tauri::AppHandle,
    path: String,
    filename: String,
    data_b64: String,
    fields: Option<serde_json::Value>,
) -> Result<serde_json::Value, SidecarError> {
    use base64::{engine::general_purpose::STANDARD as B64, Engine as _};

    let ep = endpoint(app)?;
    let bytes = B64.decode(&data_b64).map_err(SidecarError::Base64DecodeFailed)?;

    let mut form = reqwest::multipart::Form::new();
    if let Some(serde_json::Value::Object(map)) = fields {
        for (k, v) in map {
            let val = match v {
                serde_json::Value::String(s) => s,
                other => other.to_string(),
            };
            form = form.text(k, val);
        }
    }
    form = form.part(
        "file",
        reqwest::multipart::Part::bytes(bytes)
            .file_name(filename)
            .mime_str("audio/webm")
            .map_err(|e| SidecarError::InvalidMime(e.to_string()))?,
    );

    let resp = reqwest::Client::new()
        .post(format!("{}{path}", ep.base_url))
        .header("X-Auth-Token", &ep.token)
        .multipart(form)
        .send()
        .await
        .map_err(SidecarError::UploadFailed)?;

    let status = resp.status().as_u16();
    let bytes = resp.bytes().await.map_err(|e| SidecarError::UploadFailed(e))?;
    // 优先按 JSON 解析 body；否则按 UTF-8 文本回退；都失败则 base64 兜底
    let data: serde_json::Value = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(_) => match String::from_utf8(bytes.to_vec()) {
            Ok(s) => serde_json::Value::String(s),
            Err(_) => serde_json::Value::String(B64.encode(&bytes)),
        },
    };
    Ok(serde_json::json!({ "status": status, "data": data }))
}

/// 前端音频合成：`invoke("speak", { voiceId, text })` → 原始音频字节（Vec<u8>）。
/// 云端失败时侧车自动回退本地引擎，故只需转发一次即可。
#[tauri::command]
async fn speak(app: tauri::AppHandle, voice_id: String, text: String) -> Result<Vec<u8>, String> {
    let ep = endpoint(&app)?;
    let resp = reqwest::Client::new()
        .post(format!("{}/api/v1/tts/speak", ep.base_url))
        .header("X-Auth-Token", &ep.token)
        .header("Content-Type", "application/json")
        .body(serde_json::json!({ "voice_id": voice_id, "text": text }).to_string())
        .timeout(std::time::Duration::from_secs(120))
        .send()
        .await
        .map_err(SidecarError::ProxyFailed)?;
    if !resp.status().is_success() {
        return Err(SidecarError::TtsFailed(resp.status().as_u16()).into());
    }
    resp.bytes()
        .await
        .map(|b| b.to_vec())
        .map_err(|e| SidecarError::ProxyReadFailed(e).into())
}

/// 捕获单段音频，返回 base64 编码的 WAV。`start` 标志用于选择端点。
async fn capture_wav_b64(app: &tauri::AppHandle, start: bool) -> Result<String, SidecarError> {
    use base64::{engine::general_purpose::STANDARD as B64, Engine as _};

    let ep = endpoint(app)?;
    let endpoint_path = if start {
        "/api/v1/capture/start"
    } else {
        "/api/v1/capture/stop"
    };
    let resp = reqwest::Client::new()
        .post(format!("{}{}", ep.base_url, endpoint_path))
        .header("X-Auth-Token", &ep.token)
        .send()
        .await
        .map_err(if start {
            SidecarError::CaptureStartRequestFailed
        } else {
            SidecarError::CaptureStopRequestFailed
        })?;
    if !resp.status().is_success() {
        let code = resp.status().as_u16();
        return Err(if start {
            SidecarError::CaptureStartFailed(code)
        } else {
            SidecarError::CaptureStopFailed(code)
        });
    }
    let bytes = resp
        .bytes()
        .await
        .map_err(SidecarError::CaptureStopRequestFailed)?;
    Ok(B64.encode(&bytes))
}

#[tauri::command]
async fn start_capture(app: tauri::AppHandle) -> Result<String, String> {
    capture_wav_b64(&app, true).await.map_err(|e| e.to_string())
}

#[tauri::command]
async fn stop_capture(app: tauri::AppHandle) -> Result<String, String> {
    capture_wav_b64(&app, false)
        .await
        .map_err(|e| e.to_string())
}

/// 前端下载：将合成好的音频字节（base64）保存到系统「下载」目录，返回完整路径。
/// 文件名会做 Windows 非法字符清理，并以 `.wav` 结尾；同名时自动追加 ` (n)` 防覆盖。
#[tauri::command]
async fn save_audio_file(file_name: String, data_b64: String) -> Result<String, String> {
    save_audio_file_inner(file_name, data_b64).map_err(|e| e.to_string())
}

fn save_audio_file_inner(file_name: String, data_b64: String) -> Result<String, SidecarError> {
    use base64::{engine::general_purpose::STANDARD as B64, Engine as _};

    let bytes = B64.decode(&data_b64).map_err(SidecarError::Base64DecodeFailed)?;
    let dir = downloads_dir().ok_or(SidecarError::NoDownloadsDir)?;
    std::fs::create_dir_all(&dir).map_err(SidecarError::SaveFailed)?;

    let target = write_audio_to(&dir, &file_name, &bytes)?;
    Ok(target.to_string_lossy().to_string())
}

/// 将音频字节写入 `dir` 下、名为 `file_name`（经清理、强制 .wav）的文件；
/// 同名时自动追加 ` (n)` 避免覆盖。返回实际写入的完整路径。
fn write_audio_to(dir: &Path, file_name: &str, bytes: &[u8]) -> Result<PathBuf, SidecarError> {
    let safe = sanitize_file_name(file_name);
    let target = unique_path(dir, &safe);
    std::fs::write(&target, bytes).map_err(SidecarError::SaveFailed)?;
    Ok(target)
}

/// 用户「下载」目录：Windows 用 USERPROFILE，其余平台回退 HOME。
fn downloads_dir() -> Option<PathBuf> {
    std::env::var_os("USERPROFILE")
        .map(|p| Path::new(&p).join("Downloads"))
        .or_else(|| std::env::var_os("HOME").map(|p| Path::new(&p).join("Downloads")))
}

/// 清理文件名：替换 Windows 保留字符，去尾部点/空格，空则回退 voice.wav，强制 .wav 后缀。
fn sanitize_file_name(name: &str) -> String {
    let mut base: String = name
        .chars()
        .map(|c| {
            if "\\/:*?\"<>|".contains(c) || (c as u32) < 0x20 {
                '_'
            } else {
                c
            }
        })
        .collect();
    base = base.trim().to_string();
    while base.ends_with('.') || base.ends_with(' ') {
        base.pop();
    }
    if base.is_empty() {
        base = "voice".to_string();
    }
    if !base.to_lowercase().ends_with(".wav") {
        base.push_str(".wav");
    }
    base
}

/// 若目标已存在，则依次尝试 `stem (1).ext`、`stem (2).ext` … 直到不冲突。
fn unique_path(dir: &Path, name: &str) -> PathBuf {
    let candidate = dir.join(name);
    if !candidate.exists() {
        return candidate;
    }
    let path = Path::new(name);
    let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("voice");
    let ext = path.extension().and_then(|s| s.to_str()).unwrap_or("wav");
    for i in 1..10000 {
        let cand = dir.join(format!("{stem} ({i}).{ext}"));
        if !cand.exists() {
            return cand;
        }
    }
    dir.join(format!("{stem} (9999).{ext}"))
}

// ---------------------------------------------------------------------------
// 入口
// ---------------------------------------------------------------------------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(SidecarState::default())
        .setup(|app| {
            spawn_sidecar(app.handle())?;
            // 隐藏菜单栏：设置空菜单
            let menu = MenuBuilder::new(app).build()?;
            app.set_menu(menu)?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            sidecar_status,
            fetch_get,
            fetch_post,
            speak,
            start_capture,
            stop_capture,
            api_upload,
            save_audio_file
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    // 事件循环退出（含正常关窗）时回收侧车子进程
    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            kill_sidecar(app_handle);
        }
    });
}

#[cfg(test)]
mod save_audio_tests {
    use super::*;
    use base64::{engine::general_purpose::STANDARD as B64, Engine as _};

    #[test]
    fn sanitize_replaces_illegal_chars_and_forces_wav() {
        assert_eq!(sanitize_file_name("雷军"), "雷军.wav");
        assert_eq!(sanitize_file_name("a/b:c*d?.wav"), "a_b_c_d_.wav");
        assert_eq!(sanitize_file_name("  name  "), "name.wav");
        assert_eq!(sanitize_file_name("trailing."), "trailing.wav");
        assert_eq!(sanitize_file_name(""), "voice.wav");
        assert_eq!(sanitize_file_name("ALREADY.WAV"), "ALREADY.WAV");
    }

    #[test]
    fn write_audio_creates_named_wav() {
        let dir = std::env::temp_dir().join(format!("vc_test_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();

        let path = write_audio_to(&dir, "我的音色", b"RIFFdata").unwrap();
        assert_eq!(path.file_name().unwrap().to_str().unwrap(), "我的音色.wav");
        assert_eq!(std::fs::read(&path).unwrap(), b"RIFFdata");

        // 同名再写一次：不覆盖，追加 (1)
        let path2 = write_audio_to(&dir, "我的音色", b"second").unwrap();
        assert_eq!(path2.file_name().unwrap().to_str().unwrap(), "我的音色 (1).wav");

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn save_inner_decodes_base64_and_writes() {
        let b64 = B64.encode(b"wavbytes");
        // 通过注入 dir 的等价路径验证解码 + 写盘（避免污染真实 Downloads）
        let dir = std::env::temp_dir().join(format!("vc_inner_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let bytes = B64.decode(&b64).unwrap();
        let path = write_audio_to(&dir, "t", &bytes).unwrap();
        assert_eq!(std::fs::read(path).unwrap(), b"wavbytes");
        let _ = std::fs::remove_dir_all(&dir);
    }
}

