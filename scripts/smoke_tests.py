"""End-to-end smoke tests for the voice-clone service (Todo 12).

Boots the real FastAPI app (uvicorn subprocess) against a throwaway SQLite
DB and drives the full HTTP chain with httpx:

    create voice (mock reference wav) -> poll status to active
    -> speak (wav) -> speak/stream (SSE frames) -> tune params -> disable

Modes (mutually exclusive):
    --mock        (default) both providers mocked via VOLC_TTS_DISABLE=1 and
                  LOCAL_TTS_DISABLE=1; must be all-green with no credentials.
    --local-real  real ZipVoice engine (LOCAL_TTS_DISABLE unset), cloud still
                  mocked. SKIPs (exit 0) when models/zipvoice/ is not ready.
    --cloud-real  real Volcengine ICL (VOLC_TTS_DISABLE unset), local mocked.
                  SKIPs (exit 0) when credentials are absent/disabled.

Every assertion prints a PASS/FAIL line to stdout and, when --evidence is
given, is appended to that file. Exit code is 0 only when every executed
assertion passed (SKIPs do not fail the run).

Failure QA contract: if the service never becomes healthy, the script prints
"[FAIL] service not ready at <url>" and exits 1 — it never silently succeeds.

No real credentials are ever written by this script; cloud-real reads them
from the environment only.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import httpx

ROOT_DIR = Path(__file__).resolve().parent.parent
APP_DIR = ROOT_DIR / "app"
SMOKE_DB = "data/smoke.db"  # relative to ROOT_DIR; never touches the dev DB

# Mirrors app/providers/local.REQUIRED_MODEL_FILES (kept in sync by tests).
LOCAL_MODEL_FILES = (
    "encoder.int8.onnx",
    "decoder.int8.onnx",
    "tokens.txt",
    "vocos_24khz.onnx",
)

HEALTH_TIMEOUT_S = 20.0
HEALTH_POLL_S = 0.5
STATUS_POLL_MAX = 10
STATUS_POLL_S = 0.5

# ------------------------------------------------------------------ helpers


def _make_reference_wav(seconds: float = 6.0, rate: int = 24000) -> bytes:
    """Valid mono PCM16 wav used as the uploaded reference audio.

    Defaults to 6 s so it satisfies the cloud provider's
    [MIN_AUDIO_SECONDS=5, MAX_AUDIO_SECONDS=15] reference contract.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def _assert_wav(data: bytes, label: str) -> None:
    """Validate a wav payload: RIFF header, readable, non-empty frames."""
    if not data.startswith(b"RIFF") or not data.startswith(b"WAVE", 8):
        raise AssertionError(f"{label}: not a RIFF/WAVE payload")
    with wave.open(io.BytesIO(data), "rb") as wf:
        if wf.getnframes() <= 0:
            raise AssertionError(f"{label}: wav has no frames")
        if wf.getframerate() <= 0:
            raise AssertionError(f"{label}: wav has invalid sample rate")


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse SSE text into [(event, data_dict), ...]."""
    frames: list[tuple[str, dict]] = []
    event = ""
    data_lines: list[str] = []
    for line in text.splitlines():
        if line == "":
            if event:
                frames.append((event, json.loads("".join(data_lines))))
            event, data_lines = "", []
        elif line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
    if event:
        frames.append((event, json.loads("".join(data_lines))))
    return frames


# ------------------------------------------------------------- test harness


class SmokeRunner:
    """Boots the app and runs the mode-specific assertion chain."""

    def __init__(self, mode: str, port: int, evidence: Path | None) -> None:
        self.mode = mode
        self.port = port
        self.evidence = evidence
        self.base_url = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen | None = None
        self.failures: list[str] = []

    # ------------------------------------------------------------ lifecycle

    def _server_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["DB_PATH"] = SMOKE_DB
        env["PORT"] = str(self.port)
        if self.mode == "mock":
            env["VOLC_TTS_DISABLE"] = "1"
            env["LOCAL_TTS_DISABLE"] = "1"
        elif self.mode == "local-real":
            env.pop("LOCAL_TTS_DISABLE", None)  # real local engine
            env["VOLC_TTS_DISABLE"] = "1"  # cloud stays mocked
        elif self.mode == "cloud-real":
            env.pop("VOLC_TTS_DISABLE", None)  # real volcengine
            env["LOCAL_TTS_DISABLE"] = "1"  # local stays mocked
        return env

    def start(self) -> None:
        db = ROOT_DIR / SMOKE_DB
        db.unlink(missing_ok=True)  # deterministic run
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--log-level",
            "warning",
        ]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT_DIR),
            env=self._server_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def wait_healthy(self) -> bool:
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False  # server died before becoming healthy
            try:
                with httpx.Client(timeout=2.0) as client:
                    r = client.get(f"{self.base_url}/health")
                if r.status_code == 200 and r.json().get("status") == "ok":
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(HEALTH_POLL_S)
        return False

    # ------------------------------------------------------------ assertions

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        line = f"[PASS] {label}" if ok else f"[FAIL] {label}"
        if detail:
            line += f" — {detail}"
        print(line)
        if self.evidence is not None:
            with self.evidence.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if not ok:
            self.failures.append(label)

    def check_raises(self, label: str, exc_type: type[Exception], fn) -> None:
        try:
            fn()
        except exc_type:
            self.check(label, True)
        except Exception as exc:  # noqa: BLE001 - smoke must report any failure
            self.check(label, False, f"unexpected {type(exc).__name__}: {exc}")
        else:
            self.check(label, False, "no exception raised")

    def check_no_raise(self, label: str, fn) -> None:
        """Pass when fn() completes without raising (e.g. wav validation)."""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - smoke must report any failure
            self.check(label, False, f"{type(exc).__name__}: {exc}")
        else:
            self.check(label, True)

    # ---------------------------------------------------------------- chains

    def run_mock_chain(self) -> None:
        """Full HTTP chain with both providers mocked (no credentials)."""
        with httpx.Client(base_url=self.base_url, timeout=30.0) as client:
            # 1. create voice (cloud provider, mock reference wav)
            files = {"file": ("ref.wav", _make_reference_wav(), "audio/wav")}
            r = client.post("/api/v1/voices", data={"name": "smoke-voice", "provider": "cloud"}, files=files)
            self.check("create voice -> 201", r.status_code == 201, f"got {r.status_code}")
            if r.status_code != 201:
                print(f"  server said: {r.text[:300]}")
                return
            voice = r.json()
            voice_id = voice["id"]
            self.check("create voice returns id", bool(voice_id))
            self.check("create voice provider=cloud", voice.get("provider") == "cloud")
            self.check("create voice status=training", voice.get("status") == "training")

            # 2. poll status until active (mock: TRAINING twice, then ACTIVE)
            status = ""
            for _ in range(STATUS_POLL_MAX):
                r = client.get(f"/api/v1/voices/{voice_id}/status")
                self.check("status endpoint 200", r.status_code == 200, f"got {r.status_code}")
                status = r.json().get("status", "")
                if status == "active":
                    break
                time.sleep(STATUS_POLL_S)
            self.check("voice reaches active", status == "active", f"final status={status!r}")

            # 3. speak -> wav
            r = client.post(
                "/api/v1/tts/speak",
                json={"voice_id": voice_id, "text": "你好，这是冒烟测试。"},
            )
            self.check("speak -> 200", r.status_code == 200, f"got {r.status_code}")
            self.check("speak media type audio/wav", r.headers.get("content-type", "").startswith("audio/wav"))
            self.check_no_raise("speak returns playable wav", lambda: _assert_wav(r.content, "speak"))

            # 4. speak/stream -> SSE frames (provider, audio, done)
            r = client.post(
                "/api/v1/tts/speak/stream",
                json={"voice_id": voice_id, "text": "第一句。第二句！第三句？"},
            )
            self.check("stream -> 200", r.status_code == 200, f"got {r.status_code}")
            frames = _parse_sse(r.text)
            events = [e for e, _ in frames]
            self.check("stream first frame is provider", events and events[0] == "provider", f"events={events}")
            self.check("stream has audio frames", "audio" in events, f"events={events}")
            self.check("stream ends with done", events and events[-1] == "done", f"events={events}")
            audio_frames = [d for e, d in frames if e == "audio"]
            if audio_frames:
                wav_bytes = base64.b64decode(audio_frames[0]["audio_base64"])
                self.check_no_raise(
                    "stream audio frame is playable wav",
                    lambda: _assert_wav(wav_bytes, "stream"),
                )
                self.check("stream audio frame has provider", audio_frames[0].get("provider") in ("volcengine", "local"))
            else:
                self.check("stream audio frame is playable wav", False, "no audio frames")

            # 5. tune params via PATCH
            r = client.patch(
                f"/api/v1/voices/{voice_id}",
                json={"speed_ratio": 1.2, "loudness_ratio": 1.1, "pitch_ratio": 0.9},
            )
            self.check("patch params -> 200", r.status_code == 200, f"got {r.status_code}")
            patched = r.json()
            self.check("speed_ratio persisted", abs(patched.get("speed_ratio", 0) - 1.2) < 1e-9)
            self.check("loudness_ratio persisted", abs(patched.get("loudness_ratio", 0) - 1.1) < 1e-9)
            self.check("pitch_ratio persisted", abs(patched.get("pitch_ratio", 0) - 0.9) < 1e-9)

            # 6. disable
            r = client.patch(f"/api/v1/voices/{voice_id}", json={"status": "disabled"})
            self.check("disable -> 200", r.status_code == 200, f"got {r.status_code}")
            self.check("voice status=disabled", r.json().get("status") == "disabled")

            # 7. delete
            r = client.delete(f"/api/v1/voices/{voice_id}")
            self.check("delete -> 204", r.status_code == 204, f"got {r.status_code}")
            r = client.get(f"/api/v1/voices/{voice_id}")
            self.check("deleted voice is gone (404)", r.status_code == 404, f"got {r.status_code}")

    def run_local_real_chain(self) -> None:
        """Real ZipVoice engine: create local voice, speak, verify wav."""
        with httpx.Client(base_url=self.base_url, timeout=120.0) as client:
            files = {"file": ("ref.wav", _make_reference_wav(), "audio/wav")}
            r = client.post(
                "/api/v1/voices",
                data={
                    "name": "smoke-local",
                    "provider": "local",
                    "reference_text": "这是一段用于克隆的参考音频。",
                },
                files=files,
            )
            self.check("create local voice -> 201", r.status_code == 201, f"got {r.status_code}")
            voice = r.json()
            voice_id = voice["id"]
            self.check("local voice status=active", voice.get("status") == "active")

            r = client.post(
                "/api/v1/tts/speak",
                json={"voice_id": voice_id, "text": "本地模型克隆语音冒烟测试。"},
            )
            self.check("local speak -> 200", r.status_code == 200, f"got {r.status_code}")
            self.check_no_raise(
                "local speak returns playable wav",
                lambda: _assert_wav(r.content, "local speak"),
            )

            r = client.delete(f"/api/v1/voices/{voice_id}")
            self.check("delete local voice -> 204", r.status_code == 204, f"got {r.status_code}")

    def run_cloud_real_chain(self) -> None:
        """Real Volcengine ICL: create cloud voice, poll to active, speak."""
        with httpx.Client(base_url=self.base_url, timeout=120.0) as client:
            files = {"file": ("ref.wav", _make_reference_wav(), "audio/wav")}
            r = client.post("/api/v1/voices", data={"name": "smoke-cloud", "provider": "cloud"}, files=files)
            self.check("create cloud voice -> 201", r.status_code == 201, f"got {r.status_code}")
            voice = r.json()
            voice_id = voice["id"]

            status = ""
            for _ in range(STATUS_POLL_MAX):
                r = client.get(f"/api/v1/voices/{voice_id}/status")
                status = r.json().get("status", "")
                if status == "active":
                    break
                time.sleep(STATUS_POLL_S)
            self.check("cloud voice reaches active", status == "active", f"final status={status!r}")

            r = client.post(
                "/api/v1/tts/speak",
                json={"voice_id": voice_id, "text": "云端克隆语音冒烟测试。"},
            )
            self.check("cloud speak -> 200", r.status_code == 200, f"got {r.status_code}")
            self.check_no_raise(
                "cloud speak returns playable wav",
                lambda: _assert_wav(r.content, "cloud speak"),
            )

            r = client.delete(f"/api/v1/voices/{voice_id}")
            self.check("delete cloud voice -> 204", r.status_code == 204, f"got {r.status_code}")

    # ---------------------------------------------------------------- driver

    def run(self) -> int:
        if self.evidence is not None:
            self.evidence.parent.mkdir(parents=True, exist_ok=True)
            with self.evidence.open("a", encoding="utf-8") as fh:
                fh.write(f"\n## smoke_tests.py --{self.mode} @ {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

        # Pre-flight SKIP decisions (no server needed).
        if self.mode == "local-real":
            model_dir = ROOT_DIR / "models" / "zipvoice"
            missing = [f for f in LOCAL_MODEL_FILES if not (model_dir / f).is_file()]
            if missing:
                print(f"[SKIP] --local-real: models/zipvoice missing {missing}; run scripts/download_models first")
                return 0
        if self.mode == "cloud-real":
            if os.environ.get("VOLC_TTS_DISABLE") == "1" or not (
                os.environ.get("VOLC_APP_ID") and os.environ.get("VOLC_TOKEN")
            ):
                print("[SKIP] --cloud-real: VOLC_TTS_DISABLE=1 or credentials not configured")
                return 0

        self.start()
        try:
            if not self.wait_healthy():
                print(f"[FAIL] service not ready at {self.base_url}")
                return 1
            if self.mode == "mock":
                self.run_mock_chain()
            elif self.mode == "local-real":
                self.run_local_real_chain()
            else:
                self.run_cloud_real_chain()
        finally:
            self.stop()

        if self.failures:
            print(f"[FAIL] {len(self.failures)} assertion(s) failed: {self.failures}")
            return 1
        print("[PASS] all smoke assertions passed")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="voice-clone-service end-to-end smoke tests")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mock", action="store_true", help="both providers mocked (default)")
    group.add_argument("--local-real", action="store_true", help="real ZipVoice engine, cloud mocked")
    group.add_argument("--cloud-real", action="store_true", help="real Volcengine ICL, local mocked")
    parser.add_argument("--port", type=int, default=8021, help="port to run the app on (default 8021)")
    parser.add_argument("--evidence", type=Path, default=None, help="append PASS/FAIL lines to this file")
    args = parser.parse_args()

    mode = "mock"
    if args.local_real:
        mode = "local-real"
    elif args.cloud_real:
        mode = "cloud-real"

    return SmokeRunner(mode=mode, port=args.port, evidence=args.evidence).run()


if __name__ == "__main__":
    sys.exit(main())