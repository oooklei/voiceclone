# VoiceClone 桌面版 v1 实施方案

> 状态：已确认（2026-09-06）
> 决策：Windows-only v1 / Python SDK 优先 / 模型内置（砍掉 whisper medium）
> 背景：浏览器无法直接捕获系统音频（安全模型限制），业务调整为 Rust 桌面版 + SDK 集成

## 一、目标

1. **桌面应用**：Tauri 2（Rust 外壳）+ WebView2 UI，离线可安装使用
2. **系统音频**：WASAPI Loopback 原生捕获，无需浏览器选择器
3. **SDK**：其他程序通过本地 HTTP API + Python 薄封装调用"克隆 + 合成"
4. **复用**：现有 FastAPI 引擎（ZipVoice TTS + faster-whisper ASR）零改动核心

## 二、架构

```
┌─────────────────────────────────────────────────┐
│  Tauri 2 桌面应用（Rust 外壳）                    │
│  ├─ WebView2 UI（复用 static/ 页面）             │
│  ├─ 侧车生命周期：启动/健康检查/崩溃重启/退出清理  │
│  └─ 前端适配：window.__TAURI__ 存在时走本地 API   │
└──────────────┬──────────────────────────────────┘
               │ 127.0.0.1:随机端口 + X-Auth-Token
┌──────────────▼──────────────────────────────────┐
│  Python 侧车（PyInstaller 打包）                 │
│  ├─ 现有 FastAPI 应用（app/main.py 等）          │
│  ├─ 新增 /api/v1/capture/*（WASAPI loopback）    │
│  ├─ ZipVoice TTS + faster-whisper ASR（small）   │
│  └─ 模型：zipvoice + whisper small 内置          │
└──────────────┬──────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────┐
│  SDK：voiceclone-sdk（Python 包）                │
│  ├─ clone(name, audio, text) → voice_id         │
│  ├─ speak(voice_id, text, params) → wav bytes   │
│  └─ asr(audio) → text                           │
└─────────────────────────────────────────────────┘
```

## 三、API 契约（冻结 v1）

| 端点 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 健康检查（侧车存活） |
| `/api/v1/voices` | POST/GET/PATCH | 创建/列表/更新音色（现有） |
| `/api/v1/voices/{id}/retrain` | POST | 重新训练（现有） |
| `/api/v1/tts/speak` | POST | 合成语音（现有） |
| `/api/v1/asr` | POST | 识别文字稿（现有） |
| `/api/v1/capture/start` | POST | 开始 WASAPI loopback 录音 |
| `/api/v1/capture/stop` | POST | 停止录音，返回 WAV 文件 |
| `/api/v1/capture/status` | GET | 录音状态 |

鉴权：所有 `/api/v1/*` 需 `X-Auth-Token` 头（侧车启动时生成，Tauri/SDK 持有）。

## 四、分阶段任务

### Phase 1 — 引擎服务化 + 原生音频捕获（3-5 天）✅ 已完成

- [x] 1.1 侧车启动参数化：`--port 0`（随机端口）+ `--token`，stdout 打印 `READY port=xxxx token=xxxx`
- [x] 1.2 鉴权中间件：`X-Auth-Token` 校验（/health 除外）
- [x] 1.3 捕获模块 `app/capture.py`：pyaudiowpatch WASAPI loopback，能量门控复用
- [x] 1.4 捕获路由 `app/routes/capture.py`：start/stop/status，stop 返回 WAV
- [x] 1.5 requirements 增加 `pyaudiowpatch`；本地测试全绿
- [x] 1.6 验证：本地起侧车 → curl capture start → 播放音乐 → stop → 拿 WAV → ASR

### Phase 2 — Tauri 外壳（5-7 天）✅ 已完成

- [x] 2.1 脚手架：`voiceclone-desktop/` Tauri 2 项目（Rust 工具链确认）
- [x] 2.2 前端接入：static/ 页面加载进 WebView，app.js 加 Tauri 适配层（有 `__TAURI__` 走本地 API，否则回退浏览器捕获）
- [x] 2.3 Rust 命令：`start_capture` / `stop_capture` / `sidecar_status`（调侧车 HTTP）
- [x] 2.4 侧车进程管理：启动时 spawn（`--port 0 --token`），解析 stdout READY，退出时 kill
- [x] 2.5 录音→ASR→填参考文本链路打通（复用 autoTranscribe 逻辑）
- [x] 2.6 本地 `cargo tauri dev` 全流程验证

### Phase 3 — SDK 薄封装（3-5 天）✅ 已完成

- [x] 3.1 `voiceclone-sdk/` Python 包：`VoiceCloneClient`（clone/speak/asr）
- [x] 3.2 打包 wheel + 本地安装验证
- [x] 3.3 示例：`examples/sdk_demo.py`（克隆 + 合成 + 识别）

### Phase 4 — 打包与离线验证（3-5 天）

- [x] 4.1 PyInstaller 侧车 spec（hidden imports：onnxruntime/faster_whisper/pyaudiowpatch/uvicorn）
- [x] 4.2 模型内置：zipvoice + whisper small 随安装包分发
- [ ] 4.3 Tauri bundle → NSIS 安装包
- [ ] 4.4 断网验证清单：干净机器安装 → 系统音频录音 → 克隆 → 合成

## 五、风险与对策

| 风险 | 对策 |
|---|---|
| PyInstaller 打包 onnxruntime/faster-whisper 缺依赖 | Phase 4 前做打包 spike（半天），干净虚拟机验证 |
| pyaudiowpatch 依赖 Windows SDK | 有官方 wheel，pip 直装；Phase 1 先验证 |
| whisper small ~460MB 安装包偏大 | 接受（离线要求）；后续可换 distil-small 减半 |
| WASAPI loopback 只捕获默认输出设备 | v1 文档说明；后续加设备选择 |
| 侧车端口冲突 | 随机端口 + stdout 握手，无冲突 |
| 浏览器版回归 | app.js 适配层保留浏览器回退路径，两版共存 |

## 六、验收标准

1. 断网 Windows 机器：安装包安装成功，无网络依赖
2. 系统音频录音：播放任意声音 → 一键录制 → 自动识别文字稿
3. 克隆 + 合成：与浏览器版音质一致
4. SDK：`pip install voiceclone-sdk` 后 3 行代码完成克隆 + 合成
5. 浏览器版（1.9）不受影响，继续可用