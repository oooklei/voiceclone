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
import secrets
import socket
import sys


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VoiceClone sidecar (desktop)")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="bind port, 0 = random (default 0)")
    parser.add_argument("--token", default="", help="auth token; auto-generated when empty")
    return parser.parse_args(argv)


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

    config = Config(app, host=args.host, port=actual_port, log_level="warning")
    server = Server(config)
    # Socket is already bound+listening, so READY is accurate before run().
    print(f"READY port={actual_port} token={token}", flush=True)
    server.run(sockets=[sock])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())