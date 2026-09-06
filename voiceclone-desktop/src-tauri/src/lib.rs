// VoiceClone 桌面壳：管理 Python 侧车进程（sidecar.py）并暴露录音命令。
// 侧车以 `--port 0`（随机端口）+ 自动 token 启动，stdout 打印
// `READY port=<actual> token=<token>`，本模块解析后供命令调用其 HTTP API。

use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::Manager;

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

/// 定位侧车可执行：环境变量覆盖 > 打包资源（resources/sidecar/sidecar.exe）> exe 相对路径 > cargo manifest。
/// 返回 (程序路径, 是否需要 python 包装)。
fn sidecar_program(app: &tauri::AppHandle) -> Option<(PathBuf, bool)> {
    // 1. 环境变量覆盖（打包/调试均可指定）
    if let Ok(dir) = std::env::var("VOICECLONE_SIDECAR_DIR") {
        let d = PathBuf::from(dir);
        let exe = d.join("sidecar.exe");
        if exe.exists() {
            return Some((exe, false));
        }
        let py = d.join("sidecar.py");
        if py.exists() {
            return Some((py, true));
        }
    }
    // 2. 打包分发：<resource_dir>/sidecar/sidecar.exe（PyInstaller onedir）
    if let Ok(res) = app.path().resource_dir() {
        let exe = res.join("sidecar").join("sidecar.exe");
        if exe.exists() {
            return Some((exe, false));
        }
    }
    // 3. dev：exe 相对路径（debug: target/debug -> src-tauri -> voiceclone-desktop -> voiceclone）
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let p = dir.join("..").join("..").join("..").join("..").join("sidecar.py");
            if p.exists() {
                return Some((p, true));
            }
        }
    }
    // 4. cargo manifest 相对路径
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..").join("sidecar.py");
    if p.exists() {
        return Some((p, true));
    }
    None
}

/// 优先使用脚本旁的 venv python，否则回退 PATH 上的 python。
fn python_executable(script_dir: &Path) -> PathBuf {
    let venv_py = script_dir.join(".venv").join("Scripts").join("python.exe");
    if venv_py.exists() {
        venv_py
    } else {
        PathBuf::from("python")
    }
}

fn spawn_sidecar(app: &tauri::AppHandle) -> Result<(), String> {
    let (program, is_script) =
        sidecar_program(app).ok_or("sidecar not found (sidecar.exe or sidecar.py)")?;
    let mut cmd = if is_script {
        let script_dir = program.parent().ok_or("sidecar has no parent dir")?.to_path_buf();
        let python = python_executable(&script_dir);
        let mut c = Command::new(&python);
        c.arg(&program);
        c
    } else {
        Command::new(&program)
    };

    let port = std::env::var("VOICECLONE_SIDECAR_PORT").unwrap_or_else(|_| "0".into());
    cmd.args(["--port", &port]);
    if let Ok(token) = std::env::var("VOICECLONE_SIDECAR_TOKEN") {
        cmd.args(["--token", &token]);
    }
    cmd.stdout(Stdio::piped()).stderr(Stdio::null());

    let mut child = cmd.spawn().map_err(|e| format!("failed to spawn sidecar: {e}"))?;
    let stdout = child.stdout.take().ok_or("sidecar stdout unavailable")?;
    {
        let state = app.state::<SidecarState>();
        *state.child.lock().unwrap() = Some(child);
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
                    *state.endpoint.lock().unwrap() = Some(SidecarEndpoint {
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
    if let Some(mut child) = state.child.lock().unwrap().take() {
        let _ = child.kill();
        let _ = child.wait();
    }
    *state.endpoint.lock().unwrap() = None;
}

fn endpoint(state: &tauri::State<'_, SidecarState>) -> Result<SidecarEndpoint, String> {
    state.endpoint.lock().unwrap().clone().ok_or_else(|| "sidecar not ready".to_string())
}

#[tauri::command]
async fn sidecar_status(state: tauri::State<'_, SidecarState>) -> Result<SidecarStatus, String> {
    let Some(ep) = state.endpoint.lock().unwrap().clone() else {
        return Ok(SidecarStatus { running: false, healthy: false, base_url: None });
    };
    let resp = reqwest::Client::new()
        .get(format!("{}/health", ep.base_url))
        .timeout(std::time::Duration::from_secs(2))
        .send()
        .await;
    let healthy = matches!(resp, Ok(r) if r.status().is_success());
    Ok(SidecarStatus { running: true, healthy, base_url: Some(ep.base_url) })
}

#[tauri::command]
async fn start_capture(state: tauri::State<'_, SidecarState>) -> Result<(), String> {
    let ep = endpoint(&state)?;
    let resp = reqwest::Client::new()
        .post(format!("{}/api/v1/capture/start", ep.base_url))
        .header("X-Auth-Token", &ep.token)
        .send()
        .await
        .map_err(|e| format!("capture start request failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("capture start failed: HTTP {}", resp.status()));
    }
    Ok(())
}

#[tauri::command]
async fn stop_capture(state: tauri::State<'_, SidecarState>) -> Result<Vec<u8>, String> {
    let ep = endpoint(&state)?;
    let resp = reqwest::Client::new()
        .post(format!("{}/api/v1/capture/stop", ep.base_url))
        .header("X-Auth-Token", &ep.token)
        .send()
        .await
        .map_err(|e| format!("capture stop request failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("capture stop failed: HTTP {}", resp.status()));
    }
    resp.bytes().await.map(|b| b.to_vec()).map_err(|e| e.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(SidecarState::default())
        .setup(|app| {
            spawn_sidecar(app.handle()).map_err(|e| e.into())
        })
        .invoke_handler(tauri::generate_handler![sidecar_status, start_capture, stop_capture])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    // 事件循环退出（含正常关窗）时回收侧车子进程
    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            kill_sidecar(app_handle);
        }
    });
}