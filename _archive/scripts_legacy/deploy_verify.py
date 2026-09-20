#!/usr/bin/env python3
"""1.9 deployment verification: create voice -> speak -> verify audio + fallback header."""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

# HTTPS with self-signed cert: skip verification (LAN deployment)
BASE = "https://192.168.1.9:8021"
WAV = r"D:\voiceclone\models\zipvoice\test_wavs\leijun-1.wav"
TEXT = "你好，这是人声克隆服务的实机部署验证。"

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def req(method, path, body=None, headers=None, timeout=60):
    url = BASE + path
    data = body.encode() if isinstance(body, str) else body
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=timeout, context=_SSL_CTX) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()

def main():
    results = []
    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

    # 1. health
    st, hd, body = req("GET", "/health")
    check("health", st == 200 and b'"status":"ok"' in body, f"status={st} body={body[:60]}")

    # 2. create voice (local provider, with reference_text)
    boundary = "----voiceclone-verify"
    with open(WAV, "rb") as f:
        wav_data = f.read()
    parts = []
    def field(name, value):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    field("name", "deploy-verify")
    field("provider", "local")
    field("reference_text", "雷军，你好，这是参考音频的文字稿。")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"leijun-1.wav\"\r\nContent-Type: audio/wav\r\n\r\n".encode())
    parts.append(wav_data)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    st, hd, resp = req("POST", "/api/v1/voices", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=120)
    check("create voice", st == 201, f"status={st} body={resp[:200]}")
    if st != 201:
        print("ABORT: voice creation failed")
        sys.exit(1)
    voice = json.loads(resp)
    vid = voice.get("id")
    check("voice id present", bool(vid), f"id={vid}")

    # 3. poll status until active (local should be immediate)
    for i in range(10):
        st, hd, resp = req("GET", f"/api/v1/voices/{vid}/status")
        if st == 200:
            s = json.loads(resp)
            if s.get("status") == "active":
                check("voice active", True, f"after {i+1} polls")
                break
        time.sleep(2)
    else:
        check("voice active", False, f"last status={resp[:100]}")

    # 4. speak
    speak_body = json.dumps({"voice_id": vid, "text": TEXT}).encode()
    st, hd, resp = req("POST", "/api/v1/tts/speak", speak_body, {"Content-Type": "application/json"}, timeout=120)
    fb = hd.get("X-Fallback", "none")
    check("speak 200", st == 200, f"status={st} fallback={fb}")
    check("audio non-empty", len(resp) > 1000, f"bytes={len(resp)}")
    check("audio RIFF header", resp[:4] == b"RIFF", f"magic={resp[:4]}")
    out = os.path.join(os.path.dirname(__file__), "deploy-verify.wav")
    with open(out, "wb") as f:
        f.write(resp)
    check("audio saved", os.path.getsize(out) > 1000, f"path={out}")

    # 5. cleanup: delete voice
    st, hd, resp = req("DELETE", f"/api/v1/voices/{vid}")
    check("delete voice", st == 204, f"status={st}")

    # summary
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} checks passed ===")
    sys.exit(0 if passed == len(results) else 1)

if __name__ == "__main__":
    main()