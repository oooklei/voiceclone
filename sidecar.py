"""Sidecar entrypoint for the desktop app.

Spawns the VoiceClone FastAPI service on a localhost port with a per-run
auth token, printing a ``READY port=... token=...`` handshake on stdout so
the Tauri shell can discover the endpoint. Auth is opt-in: when no token is
set (browser/1.9 deployment) the API stays open.

Usage:
    python sidecar.py [--host 127.0.0.1] [--port 0] [--token <t>]
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import secrets
import socket
import sys
import time


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VoiceClone sidecar (desktop)")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="bind port, 0 = random (default 0)")
    parser.add_argument("--token", default="", help="auth token; auto-generated when empty")
    return parser.parse_args(argv)


def _write_endpoint(port: int, token: str) -> None:
    """将 {base_url, token, pid, time} 写入 %APPDATA%\VoiceClone\endpoint.json，
    供 recorder 等外部系统发现并绑定此 sidecar 实例。
    base_url 需与 recorder parseEndpointJson（只认 base_url/baseUrl）对齐。"""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return
    ep_dir = os.path.join(appdata, "VoiceClone")
    os.makedirs(ep_dir, exist_ok=True)
    ep_file = os.path.join(ep_dir, "endpoint.json")
    try:
        with open(ep_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "base_url": f"http://127.0.0.1:{port}",
                    "port": port,
                    "token": token,
                    "pid": os.getpid(),
                    "time": time.time(),
                },
                f,
                ensure_ascii=False,
            )
    except Exception:
        pass  # 写入失败不影响 sidecar 运行


def _remove_endpoint() -> None:
    """sidecar 退出时删除 endpoint.json。"""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return
    ep_file = os.path.join(appdata, "VoiceClone", "endpoint.json")
    try:
        if os.path.isfile(ep_file):
            os.remove(ep_file)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    token = args.token or secrets.token_hex(16)

    # Pre-bind so the actual port is known before uvicorn starts (no race).
    # uvicorn's fd= option relies on socket.fromfd (Unix-only), so pass the
    # already-bound socket via Server.run(sockets=[...]) instead.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    actual_port = sock.getsockname()[1]
    sock.listen(128)

    from app.main import app  # lazy: --help works without importing the app

    app.state.auth_token = token

    from uvicorn import Config, Server

    # 解决 Windows GUI 模式下 sys.stdout=None 导致 uvicorn 日志配置失败
    config = Config(
        app, host=args.host, port=actual_port, log_level="warning",
        log_config=None,
    )
    server = Server(config)
    # Socket is already bound+listening, so READY is accurate before run().
    print(f"READY port={actual_port} token={token}", flush=True)

    # 导出 endpoint.json 供外部系统发现；退出时自动清理
    _write_endpoint(actual_port, token)
    atexit.register(_remove_endpoint)

    server.run(sockets=[sock])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())