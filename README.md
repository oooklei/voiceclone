# Voice Clone Service（人声克隆播报服务）

双引擎人声克隆播报服务：管理页上传/录制试听音频 → 云端（火山引擎·声音复刻 ICL）训练出专属人声 → 调语速/音量/音调并试听 → 通过 API 用该人声播报任意文本；云端不可用时自动回退到服务器本地纯 CPU 离线引擎（ZipVoice-Distill int8 / sherpa-onnx），播报不中断。

## 目录内容

```text
app/                    FastAPI 应用（main/config/schemas + providers + repositories + routes）
static/                 管理页面单页（index.html + app.js + style.css，无构建链）
scripts/
  download_models.ps1   Windows 模型下载脚本（幂等）
  download_models.sh    Linux 模型下载脚本（幂等）
  smoke_tests.py        端到端冒烟脚本（--mock / --local-real）
tests/                  pytest 测试套件（双 mock 开关，≥30 条）
requirements.txt        运行时依赖（版本锁定）
Dockerfile              容器镜像（python:3.12-slim，非 root）
docker-compose.yml      本机 dev compose
docker-compose.gxy.yml  1.9 生产 compose（flattalk 网络）
run.ps1 / run.sh        本机一键启动（venv + uvicorn）
.env.example            环境变量模板（零密钥值）
```

## 快速启动（本机开发）

Windows：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
# 编辑 .env（可先全部留空，mock 模式即可跑通全链路）
.\run.ps1
```

Linux：

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod +x run.sh
./run.sh
```

默认监听 `http://0.0.0.0:8021`。

### 健康检查

```bash
curl http://127.0.0.1:8021/health
# {"status":"ok"}
```

### 测试

```bash
cd D:\voiceclone
$env:VOLC_TTS_DISABLE=1; $env:LOCAL_TTS_DISABLE=1
.\.venv\Scripts\python.exe -m pytest tests/ -q
# 72 passed（双 mock 开关下零真实凭据/网络依赖）
```

### 双 mock 开关

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `VOLC_TTS_DISABLE` | `0` | 设为 `1` 时火山引擎走 mock：train 返回假 speaker_id、poll ≤3 次后 active、synthesize 返回 1s 静音 wav |
| `LOCAL_TTS_DISABLE` | `0` | 设为 `1` 时本地引擎走 mock：synthesize 返回 0.3s beep wav |

两个开关同时为 1 时 speak/stream 仍可完整演示链路（管理页可试听），用于无凭据环境联调。

## 环境变量（.env）

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `PORT` | 否 | `8021` | 监听端口 |
| `HOST` | 否 | `0.0.0.0` | 监听地址 |
| `VOLC_APP_ID` | 云主力时 | - | 火山引擎应用 ID |
| `VOLC_TOKEN` | 云主力时 | - | 火山引擎访问令牌 |
| `VOLC_RESOURCE_ID` | 否 | `seed-icl-1.0` | 声音复刻资源 ID |
| `VOLC_TTS_DISABLE` | 否 | `0` | 设为 `1` 禁用火山调用（mock） |
| `VOLC_HTTP_TIMEOUT_S` | 否 | `15.0` | 火山 HTTP 超时（秒） |
| `VOLC_POLL_INTERVAL_S` | 否 | `10.0` | 训练状态轮询间隔（秒） |
| `VOLC_POLL_TIMEOUT_S` | 否 | `300.0` | 训练轮询上限（秒） |
| `LOCAL_TTS_DISABLE` | 否 | `0` | 设为 `1` 禁用本地引擎（mock） |
| `LOCAL_MODEL_DIR` | 否 | `models/` | 本地模型目录 |
| `LOCAL_TTS_THREADS` | 否 | `4` | sherpa-onnx 推理线程数 |
| `LOCAL_TTS_NUM_STEPS` | 否 | `4` | ZipVoice 采样步数 |
| `DB_PATH` | 否 | `data/voiceclone.db` | SQLite 数据库路径 |
| `ADMIN_TOKEN` | 否 | 空 | 预留管理鉴权钩子（本期不实现完整认证） |

`.env` 含真实凭据，**切勿提交到版本库**（`.gitignore`/`.dockerignore` 已排除）。

## 模型下载（本地兜底引擎）

ZipVoice-Distill int8 模型 + vocos 声码器，约 123M：

```bash
# Linux
bash scripts/download_models.sh
# Windows PowerShell
.\scripts\download_models.ps1
```

- 幂等：`models/zipvoice/` 已存在时跳过，可重复运行。
- 断网/慢网可用 `--mirror` 走 hf-mirror.com 备用源。
- 模型**永不打进镜像**：`models/` 目录经卷挂载进容器（见 compose 文件）。

## Docker 部署（本机 dev）

```bash
copy .env.example .env   # 或 cp
docker compose up -d --build
docker compose ps        # 期望 healthy
curl http://127.0.0.1:8021/health
```

- 卷挂载：`./data:/app/data`（SQLite + 参考音频）、`./models:/app/models`（模型）。
- 健康检查：容器内 `python -c urllib` 访问 `/health`，30s 间隔 / 5s 超时 / 3 次重试。
- 容器以非 root（uid 1000）运行；宿主 `data/`、`models/` 目录需 uid 1000 可写：
  `mkdir -p data models && chown -R 1000:1000 data models`。

## 1.9 部署（生产）

目标机 `192.168.1.9`（root + SSH 密钥 `C:/Users/Administrator/.ssh/id_ed25519`）。端口 `8021` 已核实空闲（宿主机已占用 5000/9080/8020/9000-9001/6333-6334/8080/8443/6379，其中 8020=gxy-tencent-asr；若未来 care_tts 上 1.9 需重估端口）。

> **HTTPS（必读）**：管理页麦克风录制依赖浏览器安全上下文（`getUserMedia` 仅 HTTPS/localhost 可用），
> 因此 1.9 生产 compose 通过 uvicorn `--ssl-keyfile/--ssl-certfile` 启用自签名 HTTPS（同端口 8021）。
> 浏览器首次访问需接受一次自签名证书警告（「高级」→「继续前往」），之后即为安全上下文，麦克风可用。
> 前端 `app.js` 已加 `isSecureContext` 检测：非 HTTPS 访问时明确提示改用 https 或文件上传。

### 部署七步

1. **代码上机**（二选一）：
   - 远端 git：`git clone <repo> /opt/voiceclone && cd /opt/voiceclone`
   - 或 scp 目录：`scp -r -i C:/Users/Administrator/.ssh/id_ed25519 . root@192.168.1.9:/opt/voiceclone`
2. **构建镜像**（在 1.9 上，用 `Dockerfile.gxy` 走 aliyun 源）：
   ```bash
   cd /opt/voiceclone
   docker build -f Dockerfile.gxy -t gxy-voiceclone:latest .
   ```
   > 实测：容器内 tuna 源被 CDN 拦截（apt/pip 均 403），aliyun 源 apt+pip 均成功。
   > `Dockerfile.gxy` 仅用主 archive trixie+trixie-updates（无 security suite），
   > pip 走 `--index-url https://mirrors.aliyun.com/pypi/simple/`。
3. **模型下载**（1.9 出网已验证可达 github/hf-mirror/pypi）：
   ```bash
   mkdir -p /mnt/lei/gxy/voiceclone/models
   bash scripts/download_models.sh
   # 或 scp 预置（可选兜底）：
   # scp -r -i C:/Users/Administrator/.ssh/id_ed25519 models/* root@192.168.1.9:/mnt/lei/gxy/voiceclone/models/
   ```
4. **.env 就位**（真实凭据由运维/用户填写，绝不进代码库）：
   ```bash
   cp .env.example .env && vi .env
   # 填 VOLC_APP_ID / VOLC_TOKEN（云主力）；其余可留默认
   ```
5. **自签名证书**（HTTPS 必需；容器内 uid 1000 需可读，故 chmod 644）：
   ```bash
   mkdir -p /mnt/lei/gxy/voiceclone/certs && cd /mnt/lei/gxy/voiceclone/certs
   openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
     -subj "/CN=192.168.1.9" -keyout key.pem -out cert.pem
   chmod 644 key.pem cert.pem
   ```
6. **启动**：
   ```bash
   docker compose -f docker-compose.gxy.yml up -d
   docker compose -f docker-compose.gxy.yml ps   # 期望 healthy
   ```
   > compose 已通过 `command:` 覆盖 CMD 启用 SSL（`--ssl-keyfile /app/certs/key.pem --ssl-certfile /app/certs/cert.pem`），
   > 并挂载 `./certs:/app/certs:ro`；healthcheck 走 HTTPS（ssl 不校验）。
7. **验证**（一键脚本，9 项断言）：
   ```bash
   curl -sk https://192.168.1.9:8021/health      # {"status":"ok"}（-k 跳过自签名校验）
   python3 scripts/deploy_verify.py              # 9/9 passed（建语音→speak→RIFF→删语音）
   ```
   `deploy_verify.py` 走本地真实链：health 200 / 建本地语音 201（active 立即）/ speak 200 /
   RIFF wav 可解析 / fallback=none（本地直链正确）/ 删语音 204 / 列表不含 / 重复删 404 / 无 5xx。
   脚本 BASE 已指向 `https://192.168.1.9:8021` 且 ssl 不校验（自签名场景）。

### 回退演练（云端失败场景，已实机验证）

> 关键：`VOLC_TTS_DISABLE=1` 时火山走 mock（返回静音 wav，**不触发回退、无 X-Fallback 头**）。
> 真实回退演练必须 `VOLC_TTS_DISABLE=0` + 空凭据（真实请求鉴权失败 → ProviderOperationalError → 本地回退），
> 且云音色的 `local_prompt_path` 必须指向真实本地 prompt（含 `.txt` 侧车，由 API 建本地音色时生成）。

```bash
# 1) 建一个真实本地音色（生成 prompt_slice.wav + 侧车 txt）
curl -s -X POST http://127.0.0.1:8021/api/v1/voices \
  -F "name=fb-base" -F "provider=local" -F "reference_text=参考文字稿" \
  -F "file=@/mnt/lei/gxy/voiceclone/models/zipvoice/test_wavs/leijun-1.wav" \
  -o /tmp/fb-local.json
# 记下返回的 local_prompt_path（如 /app/data/local_prompts/<id>.wav）

# 2) 启用云端（空凭据）+ 重建容器
sed -i 's/^VOLC_TTS_DISABLE=.*/VOLC_TTS_DISABLE=0/' .env
docker compose -f docker-compose.gxy.yml up -d --force-recreate

# 3) DB 种子一个 cloud 音色，local_prompt_path 指向步骤 1 的真实 prompt
docker exec gxy-voiceclone python -c "..."   # INSERT voices(provider='cloud', local_prompt_path='<步骤1路径>', ...)

# 4) speak 该云音色 → 期望 200 + X-Fallback: local + 可播 wav
curl -s -D - -X POST http://127.0.0.1:8021/api/v1/tts/speak \
  -H "Content-Type: application/json" \
  --data-binary '{"voice_id":"<cloud_id>","text":"回退测试"}' -o out.wav

# 5) 清理种子音色，恢复云端禁用态
docker exec gxy-voiceclone python -c "DELETE FROM voices WHERE id IN (...)"
sed -i 's/^VOLC_TTS_DISABLE=.*/VOLC_TTS_DISABLE=1/' .env
docker compose -f docker-compose.gxy.yml up -d --force-recreate
```

实机结果（2026-09-06）：HTTP 200 | `x-fallback: local` | audio/wav 162522 bytes | 3.38s 真实本地合成。

### 网络说明（SSE 直连）

宿主机 `gxy-sauc-nginx` 仅监听 `0.0.0.0:9080→80`、只反代内部 `api:8000`（gxy-sauc-api），**不会自动代理新增的 flattalk-net 容器端口**（compose 中 external 网络实际名 `flattalk_flattalk-network`）。SSE 验证一律走容器端口直连 curl。如需 nginx 反代 SSE，参考片段（对齐 gxy-sauc-nginx 与 care_tts 惯例）：

```nginx
location /api/v1/tts/speak/stream {
    proxy_pass http://127.0.0.1:8021;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_read_timeout 1200s;
    proxy_send_timeout 1200s;
    proxy_buffering off;
    proxy_cache off;
}
```

## API 一览

| 端点 | 说明 |
|------|------|
| `GET /health` | 健康检查 |
| `POST /api/v1/voices` | 创建音色（multipart: name/file/provider） |
| `GET /api/v1/voices` | 音色列表（可选 `?provider=`） |
| `GET /api/v1/voices/{id}` | 音色详情 |
| `PATCH /api/v1/voices/{id}` | 调参/启停（speed_ratio 0.1-2 / loudness_ratio 0.5-2 / pitch_ratio 0.1-2 / status） |
| `POST /api/v1/voices/{id}/retrain` | 重新训练 |
| `GET /api/v1/voices/{id}/status` | 训练状态轮询 |
| `DELETE /api/v1/voices/{id}` | 删除音色（连 data 文件） |
| `POST /api/v1/tts/speak` | 单段合成（云失败自动回退本地，响应头 `X-Fallback: local`） |
| `POST /api/v1/tts/speak/stream` | SSE 流式合成（首帧 `event: provider`） |

错误响应统一结构：`{code, message, detail?}`。

## 部署注意事项

1. 服务器需能访问 `https://openspeech.bytedance.com`（云主力）与 github/hf-mirror（模型下载）。
2. 生产环境 `.env` 含密钥，切勿提交到公开仓库。
3. 长文本（>300 字或预计 >60s）自动按标点切片拼接；单次输入限 1-5000 字。
4. 本地引擎不支持音量/音调调节（管理页对 local 音色禁用对应控件并标注"仅云端"）。
5. 本机（Windows venv pip）与容器（python:3.12-slim manylinux wheel）安装差异：容器内 sherpa-onnx/onnxruntime 依赖 `libgomp1`（OpenMP）运行时库，Dockerfile 已用 apt 补装；本机 Windows 无需此步。