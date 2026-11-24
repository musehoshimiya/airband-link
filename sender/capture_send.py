#!/usr/bin/env python3
"""Airband Link - sender (Windows PC side).

Watches the receiver's LineOut audio coming into the PC, cuts it into
utterances with a simple level-based VAD, and POSTs each utterance as a
WAV file to the receiver side (Muse's VM) over Tailscale.

Usage:
    python capture_send.py --list-devices
    python capture_send.py --device 3 --url http://100.x.x.x:8765/upload

Environment:
    AIRBAND_TOKEN   shared secret sent as X-Airband-Token header
    AIRBAND_URL     default upload URL (overridden by --url)
"""

from __future__ import annotations

import argparse
import io
import os
import queue
import sys
import threading
import time
import wave
from collections import deque
from datetime import datetime

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sys.exit("sounddevice is required: pip install sounddevice numpy requests")

try:
    import requests
except ImportError:
    requests = None  # fall back to urllib below

SAMPLE_RATE = 16000
BLOCK = 1024  # frames per callback block (~64 ms at 16 kHz)


def rms_level(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))


def encode_wav(frames: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Encode mono float32 samples in [-1, 1] as 16-bit PCM WAV bytes."""
    pcm = (np.clip(frames, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


class Segmenter:
    """Level-based VAD: starts a segment when the level crosses the
    threshold, closes it after `hangover` seconds of silence."""

    def __init__(self, threshold: float, hangover_s: float,
                 min_s: float, max_s: float, preroll_s: float = 0.3):
        self.threshold = threshold
        self.hangover_blocks = int(hangover_s * SAMPLE_RATE / BLOCK)
        self.min_blocks = int(min_s * SAMPLE_RATE / BLOCK)
        self.max_blocks = int(max_s * SAMPLE_RATE / BLOCK)
        self.preroll_blocks = max(1, int(preroll_s * SAMPLE_RATE / BLOCK))
        self._pre: deque[np.ndarray] = deque(maxlen=self.preroll_blocks)
        self._seg: list[np.ndarray] | None = None
        self._silence = 0

    def feed(self, block: np.ndarray) -> list[np.ndarray]:
        """Feed one block; returns any finished segments."""
        out: list[np.ndarray] = []
        loud = rms_level(block) >= self.threshold
        if self._seg is None:
            if loud:
                self._seg = list(self._pre) + [block]
                self._silence = 0
            else:
                self._pre.append(block)
        else:
            self._seg.append(block)
            if loud:
                self._silence = 0
            else:
                self._silence += 1
                if self._silence >= self.hangover_blocks:
                    out += self._flush()
            if self._seg is not None and len(self._seg) >= self.max_blocks:
                out += self._flush()
        return out

    def _flush(self) -> list[np.ndarray]:
        seg, self._seg = self._seg, None
        silence, self._silence = self._silence, 0
        self._pre.clear()
        if seg is None:
            return []
        # Trim trailing silence, keeping a short tail so words are not clipped.
        keep_blocks = int(0.3 * SAMPLE_RATE / BLOCK)
        trim = max(0, silence - keep_blocks)
        if trim:
            seg = seg[:len(seg) - trim]
        if len(seg) >= self.min_blocks:
            return [np.concatenate(seg)]
        return []


def post_wav(url: str, token: str, wav_bytes: bytes, stamp: str) -> bool:
    headers = {"X-Airband-Token": token, "X-Airband-Stamp": stamp,
               "Content-Type": "audio/wav"}
    try:
        if requests is not None:
            r = requests.post(url, data=wav_bytes, headers=headers, timeout=15)
            return r.status_code == 200
        import urllib.request
        req = urllib.request.Request(url, data=wav_bytes, headers=headers,
                                     method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:  # network hiccup; caller retries
        print(f"[send] failed ({exc})", flush=True)
        return False


def send_worker(send_q: "queue.Queue[tuple[str, bytes] | None]",
                url: str, token: str) -> None:
    while True:
        item = send_q.get()
        if item is None:
            return
        stamp, wav_bytes = item
        for attempt in range(3):
            if post_wav(url, token, wav_bytes, stamp):
                print(f"[send] {stamp} ok ({len(wav_bytes)} bytes)", flush=True)
                break
            time.sleep(2 * (attempt + 1))
        else:
            print(f"[send] {stamp} dropped after retries", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Airband Link sender")
    ap.add_argument("--list-devices", action="store_true",
                    help="list input devices and exit")
    ap.add_argument("--device", default=None,
                    help="input device index or name substring")
    ap.add_argument("--url", default=os.environ.get("AIRBAND_URL", ""),
                    help="upload URL, e.g. http://100.x.x.x:8765/upload")
    ap.add_argument("--threshold", type=float, default=0.02,
                    help="VAD level threshold (RMS, 0-1). Default 0.02")
    ap.add_argument("--hangover", type=float, default=2.0,
                    help="silence seconds before a segment closes. Default 2.0")
    ap.add_argument("--min-duration", type=float, default=0.8,
                    help="drop segments shorter than this. Default 0.8")
    ap.add_argument("--max-duration", type=float, default=60.0,
                    help="force-cut segments longer than this. Default 60")
    args = ap.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return
    if not args.url:
        sys.exit("--url or AIRBAND_URL is required")

    token = os.environ.get("AIRBAND_TOKEN", "")
    device = args.device
    if device is not None and device.isdigit():
        device = int(device)

    seg = Segmenter(args.threshold, args.hangover,
                    args.min_duration, args.max_duration)
    send_q: "queue.Queue[tuple[str, bytes] | None]" = queue.Queue(maxsize=100)
    threading.Thread(target=send_worker, args=(send_q, args.url, token),
                     daemon=True).start()

    def callback(indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            print(f"[audio] {status}", file=sys.stderr, flush=True)
        block = indata[:, 0].copy()
        for samples in seg.feed(block):
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            wav_bytes = encode_wav(samples)
            try:
                send_q.put_nowait((stamp, wav_bytes))
                print(f"[vad] segment {stamp} "
                      f"({len(samples) / SAMPLE_RATE:.1f}s)", flush=True)
            except queue.Full:
                print("[vad] send queue full; segment dropped", flush=True)

    print(f"Listening on device={device!r} -> {args.url} "
          f"(threshold={args.threshold})", flush=True)
    with sd.InputStream(device=device, channels=1, samplerate=SAMPLE_RATE,
                        blocksize=BLOCK, dtype="float32",
                        callback=callback):
        print("Running. Ctrl+C to stop.", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("Stopped.", flush=True)


if __name__ == "__main__":
    main()
