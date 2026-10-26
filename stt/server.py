"""Local speech-to-text service: NVIDIA Parakeet (ONNX, CPU).

POST /transcribe   body = raw audio bytes (any format ffmpeg reads, e.g. Telegram OGG/Opus)
                   -> {"text": "...", "seconds": 4.2, "ms": 830}
GET  /health       -> {"ok": true, "model": "..."}

Audio is decoded in memory and never written to disk. Reachable only on the Docker network.
"""
import json
import logging
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import onnx_asr

logging.basicConfig(level=logging.INFO, format="%(asctime)s stt: %(message)s")
log = logging.getLogger()

MODEL_NAME = os.environ.get("STT_MODEL", "nemo-parakeet-tdt-0.6b-v2")
QUANTIZATION = os.environ.get("STT_QUANTIZATION") or None
MAX_SECONDS = int(os.environ.get("VOICE_MAX_SECONDS", "120"))
MAX_BYTES = 10 * 1024 * 1024
SAMPLE_RATE = 16_000

log.info("loading %s (quantization=%s)", MODEL_NAME, QUANTIZATION)
_t = time.monotonic()
MODEL = onnx_asr.load_model(MODEL_NAME, quantization=QUANTIZATION)
log.info("model ready in %.1fs", time.monotonic() - _t)
_model_lock = threading.Lock()  # one inference at a time; keeps memory predictable


class AudioError(ValueError):
    pass


def decode(audio: bytes) -> np.ndarray:
    """Any audio -> 16 kHz mono float32 PCM via ffmpeg (stdin -> stdout, no temp files)."""
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-t", str(MAX_SECONDS + 1), "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"],
        input=audio, capture_output=True, timeout=60,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise AudioError(proc.stderr.decode(errors="replace").strip()[:200] or "could not decode audio")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(audio: bytes) -> dict:
    t0 = time.monotonic()
    wave = decode(audio)
    seconds = len(wave) / SAMPLE_RATE
    if seconds > MAX_SECONDS:
        raise AudioError(f"audio longer than {MAX_SECONDS}s")
    if seconds < 0.3:
        return {"text": "", "seconds": round(seconds, 2), "ms": 0}
    with _model_lock:
        text = MODEL.recognize(wave, sample_rate=SAMPLE_RATE)
    ms = int((time.monotonic() - t0) * 1000)
    log.info("transcribed %.1fs of audio in %d ms", seconds, ms)  # never log the text itself
    return {"text": (text or "").strip(), "seconds": round(seconds, 2), "ms": ms}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "model": MODEL_NAME, "quantization": QUANTIZATION})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/transcribe":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BYTES:
            self._send(413, {"error": f"body must be 1..{MAX_BYTES} bytes"})
            return
        try:
            self._send(200, transcribe(self.rfile.read(length)))
        except AudioError as e:
            self._send(422, {"error": str(e)})
        except Exception as e:
            log.exception("transcription failed")
            self._send(500, {"error": type(e).__name__})

    def log_message(self, fmt, *args):  # quiet access log
        pass


if __name__ == "__main__":
    log.info("listening on :8000")
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
