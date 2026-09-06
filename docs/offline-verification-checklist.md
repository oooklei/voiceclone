# VoiceClone 桌面版断网验证清单（Phase 4.4）

> 目标：在**无网络连接**的干净 Windows 机器上验证安装包可安装、可录音、可克隆、可合成，
> 全程零网络依赖。对应 `desktop-v1-plan.md` 验收标准第 1-3 条。

## 前置条件

| 项 | 值 |
|---|---|
| 测试机 | 无网络 Windows 10/11 x64（或断网虚拟机） |
| 安装包 | `voiceclone-desktop/src-tauri/target/release/bundle/nsis/VoiceClone_0.1.0_x64-setup.exe`（约 636 MB） |
| 参考音频 | 任意 5-10 秒人声 WAV（16kHz 单声道最佳） |
| 预期安装目录 | `C:\Program Files\VoiceClone\` |

## 1. 安装

- [ ] 断网状态下双击安装包，NSIS 向导完成安装（无网络请求、无报错）
- [ ] 安装目录包含：
  - `voiceclone-desktop.exe`（Tauri 主程序）
  - `resources\sidecar\sidecar.exe`（PyInstaller 侧车）
  - `resources\sidecar\_internal\models\zipvoice\`（音色模型）
  - `resources\sidecar\_internal\models\whisper\small\`（whisper small，含 model.bin 461MB）
  - `resources\sidecar\_internal\static\`（前端静态资源）
- [ ] 开始菜单出现 VoiceClone 快捷方式

## 2. 启动

- [ ] 启动 VoiceClone，窗口正常显示
- [ ] 任务管理器可见 `sidecar.exe` 进程（Rust 从 `resources\sidecar\` 自动拉起）
- [ ] 界面显示"服务已就绪"（`/health` 返回 ok，侧车握手成功）
- [ ] 断网确认：启动过程无任何网络请求（可用防火墙出站日志核对）

## 3. 系统音频录音 → 自动转写

- [ ] 播放任意声音（音乐/视频/人声）
- [ ] 点击"录制"，录音完成后自动转写
- [ ] 转写文本正确（whisper small 本地推理，无网络）
- [ ] 转写结果含 `confidence` 与 `engine=whisper-small` 字段

## 4. 克隆 + 合成

- [ ] 用参考音频克隆音色，返回 voice_id
- [ ] 输入文本合成语音，播放正常
- [ ] 音质与浏览器版（1.9）一致
- [ ] 合成不存在的 voice_id → 404 `VOICE_NOT_FOUND`（错误处理正常）

## 5. 持久化与重启

- [ ] 关闭并重启应用，已克隆音色仍在（`data\voiceclone.db` 持久化）
- [ ] 重启后侧车自动重新拉起，无需手动操作

## 6. SDK 验证（可选，需联网安装 SDK 后断网运行）

- [ ] `pip install voiceclone-sdk` 后，3 行代码完成克隆 + 合成：

```python
from voiceclone_sdk import VoiceCloneClient
client = VoiceCloneClient(base_url="http://127.0.0.1:8021", token="<token>")
voice_id = client.clone("ref.wav")
client.speak(voice_id, "你好，世界", "out.wav")
```

## 7. 回归确认

- [ ] 浏览器版（1.9）不受影响，继续可用（独立部署，互不干扰）

---

## 本地已验证项（2026-09-06，开发机）

以下项已在开发机完成验证，断网机器上仅需复核：

- [x] `cargo tauri build` 产出 NSIS 安装包（636.3 MB）
- [x] 打包资源 `target\release\sidecar\sidecar.exe` 独立启动：`READY port=4096 token=res-test-token`
- [x] 打包资源 sidecar 全链路：克隆 `9e3fc330d1d34bb59d6bc09ad393a286` → 合成 121370 字节 → ASR `confidence=0.910 engine=whisper-small`
- [x] 冻结 exe（`dist\sidecar\`）全链路：克隆 → 合成 129580 字节 → ASR `confidence=0.944 engine=whisper-small`
- [x] dev venv ASR：`confidence=0.969 engine=whisper-small`
- [x] 模型文件齐全：`models\whisper\small\`（config.json / model.bin 461MB / tokenizer.json / vocabulary.txt）

## 已知限制

- 安装包 636 MB（whisper small 461MB + zipvoice 模型），NSIS 压缩后体积大属预期（计划风险表已列）
- 首次启动 whisper 模型加载约需数秒（本地推理，无网络）
- 系统音频录音依赖 Windows WASAPI loopback（`pyaudiowpatch`），需测试机有声卡/虚拟声卡