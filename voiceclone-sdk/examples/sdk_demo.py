"""VoiceClone SDK demo: clone a voice, synthesize speech, transcribe audio.

Usage:
    python examples/sdk_demo.py <reference.wav> [--text 你好，世界] \
        [--base-url http://127.0.0.1:8021] [--token <t>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vc import VoiceCloneError, VoiceCloneHTTPError, asr, clone, speak


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VoiceClone SDK demo")
    parser.add_argument("reference", type=Path, help="reference audio (wav) for cloning")
    parser.add_argument("--text", default="你好，世界！这是人声克隆播报服务。", help="text to synthesize")
    parser.add_argument("--base-url", default="http://127.0.0.1:8021")
    parser.add_argument("--token", default="", help="X-Auth-Token when the sidecar runs with auth")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if not args.reference.is_file():
        print(f"error: reference audio not found: {args.reference}", file=sys.stderr)
        return 2

    token = args.token or None
    try:
        print(f"[1/3] cloning voice from {args.reference} ...")
        voice_id = clone(
            name=args.reference.stem,
            audio=args.reference,
            reference_text="参考文字稿",
            base_url=args.base_url,
            token=token,
        )
        print(f"      voice_id = {voice_id}")

        print(f"[2/3] synthesizing: {args.text!r}")
        wav = speak(voice_id, args.text, base_url=args.base_url, token=token)
        out = Path("output.wav")
        out.write_bytes(wav)
        print(f"      wrote {out} ({len(wav)} bytes)")

        print("[3/3] transcribing output.wav ...")
        try:
            result = asr(out, base_url=args.base_url, token=token)
            print(
                f"      text={result['text']!r} "
                f"confidence={result['confidence']:.3f} engine={result['engine']}"
            )
        except VoiceCloneHTTPError as exc:
            if exc.status_code == 503:
                print(f"      (ASR model not installed on the server: {exc.message})")
            else:
                raise

        print("done.")
        return 0
    except VoiceCloneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())