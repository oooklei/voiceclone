"""Sidecar integration test (Windows).

Spawns ``python sidecar.py --port 0 --token <t>``, parses the READY
handshake, and verifies health + auth enforcement over real HTTP.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def _read_ready(proc: subprocess.Popen, timeout: float = 20.0) -> str:
    """Read the first stdout line with a hard timeout (readline blocks)."""
    result: dict[str, str] = {}

    def reader() -> None:
        result["line"] = proc.stdout.readline()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        proc.kill()
        raise AssertionError("sidecar did not print READY within timeout")
    return result["line"].strip()


@pytest.mark.skipif(sys.platform != "win32", reason="sidecar handshake verified on Windows")
def test_sidecar_reads_ready_and_enforces_auth() -> None:
    proc = subprocess.Popen(
        [str(PYTHON), "sidecar.py", "--port", "0", "--token", "sidecar-test-token"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        ready = _read_ready(proc)
        match = re.match(r"READY port=(\d+) token=(\S+)", ready)
        assert match, f"unexpected READY line: {ready!r}"
        port, token = int(match.group(1)), match.group(2)
        assert token == "sidecar-test-token"

        base = f"http://127.0.0.1:{port}"
        # /health is open; poll until the app is actually serving.
        resp = None
        for _ in range(50):
            try:
                resp = httpx.get(f"{base}/health", timeout=1.0)
                if resp.status_code == 200:
                    break
            except httpx.ConnectError:
                time.sleep(0.1)
        assert resp is not None and resp.status_code == 200, "sidecar did not become healthy"

        # /api/v1/* requires the token.
        assert httpx.get(f"{base}/api/v1/voices", timeout=5.0).status_code == 401
        ok = httpx.get(
            f"{base}/api/v1/voices", headers={"X-Auth-Token": token}, timeout=5.0
        )
        assert ok.status_code == 200
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()