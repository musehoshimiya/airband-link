#!/usr/bin/env python3
"""Airband Link - receiver (Muse's VM side).

Accepts WAV uploads from the sender over Tailscale, transcribes them
with faster-whisper, and appends the text to a daily transcript log
that Muse reads.

Usage:
    AIRBAND_TOKEN=<shared secret> python receive_transcribe.py

Environment:
    AIRBAND_TOKEN       shared secret; uploads without it are rejected
    AIRBAND_PORT        listen port (default 8765)
    AIRBAND_MODEL       faster-whisper model size (default "small")
    AIRBAND_DATA_DIR    where audio and transcripts are stored
                        (default: ./data next to this script)
"""

from __future__ import annotations

import os
import queue
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = os.environ.get("AIRBAND_TOKEN", "")
PORT = int(os.environ.get("AIRBAND_PORT", "8765"))
MODEL_SIZE = os.environ.get("AIRBAND_MODEL", "small")
DATA_DIR = Path(os.environ.get(
    "AIRBAND_DATA_DIR", Path(__file__).resolve().parent / "data"))

work_q: "queue.Queue[Path]" = queue.Queue()


def transcript_path(day: str) -> Path:
    return DATA_DIR / "transcripts" / f"{day}.txt"


def transcribe_worker() -> None:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("[asr] faster-whisper is not installed; audio will be "
              "saved but not transcribed. "
              "Install with: pip install faster-whisper", flush=True)
        # Drain the queue so it does not grow forever.
        while True:
            work_q.get()
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    print(f"[asr] model '{MODEL_SIZE}' ready", flush=True)
    while True:
        wav_path = work_q.get()
        try:
            segments, _info = model.transcribe(
                str(wav_path), language="en", beam_size=1,
                vad_filter=True)
            text = " ".join(s.text.strip() for s in segments).strip()
        except Exception as exc:
            text = f"(transcription failed: {exc})"
        stamp = wav_path.stem  # YYYYMMDD-HHMMSS
        day = stamp[:4] + "-" + stamp[4:6] + "-" + stamp[6:8]
        hhmmss = stamp[9:11] + ":" + stamp[11:13] + ":" + stamp[13:15]
        line = f"[{hhmmss}] {text}" if text else f"[{hhmmss}] (no speech)"
        path = transcript_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(f"[asr] {line}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # keep logs quiet
        pass

    def do_GET(self) -> None:
        if self.path == "/health":
            self._reply(200, "ok")
        else:
            self._reply(404, "not found")

    def do_POST(self) -> None:
        if self.path != "/upload":
            self._reply(404, "not found")
            return
        if not TOKEN or self.headers.get("X-Airband-Token") != TOKEN:
            self._reply(403, "forbidden")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 20 * 1024 * 1024:
            self._reply(400, "bad length")
            return
        body = self.rfile.read(length)
        stamp = self.headers.get("X-Airband-Stamp") or \
            datetime.now().strftime("%Y%m%d-%H%M%S")
        inbox = DATA_DIR / "inbox" / stamp[:8]
        inbox.mkdir(parents=True, exist_ok=True)
        wav_path = inbox / f"{stamp}.wav"
        suffix = 1
        while wav_path.exists():
            wav_path = inbox / f"{stamp}-{suffix}.wav"
            suffix += 1
        wav_path.write_bytes(body)
        work_q.put(wav_path)
        print(f"[recv] {wav_path.name} ({length} bytes)", flush=True)
        self._reply(200, "ok")

    def _reply(self, code: int, text: str) -> None:
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    if not TOKEN:
        raise SystemExit("AIRBAND_TOKEN must be set")
    (DATA_DIR / "inbox").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "transcripts").mkdir(parents=True, exist_ok=True)
    threading.Thread(target=transcribe_worker, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Airband Link receiver listening on :{PORT} "
          f"(data: {DATA_DIR})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.", flush=True)


if __name__ == "__main__":
    main()
