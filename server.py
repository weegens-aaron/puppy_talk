"""Lifecycle + HTTP client for the pocket-tts sidecar server.

The sidecar is `uvx pocket-tts serve` -- it lives in its own uv-managed
environment so code-puppy's uvx env stays torch-free. We only speak HTTP
to it (httpx is already a code_puppy dependency).

Endpoints (verified against pocket_tts/main.py):
    GET  /health            liveness
    POST /tts               form fields: text, voice_url -> chunked audio/wav
"""

import contextlib
import os
import struct
import subprocess
import threading
import time
from pathlib import Path

import httpx

DEFAULT_PORT = 8917
_HOST = "127.0.0.1"
_HEALTH_TIMEOUT_S = 2.0
# First run downloads torch + the model into uvx's cache; be patient.
_STARTUP_WAIT_S = 300.0
_SYNTH_TIMEOUT_S = 120.0

_LOG_FILE = Path(__file__).parent / "pocket_tts_server.log"

_proc: subprocess.Popen | None = None
_lock = threading.Lock()


def base_url(port: int) -> str:
    return f"http://{_HOST}:{port}"


def is_healthy(port: int) -> bool:
    try:
        resp = httpx.get(f"{base_url(port)}/health", timeout=_HEALTH_TIMEOUT_S)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _spawn(port: int, quantize: bool = False) -> subprocess.Popen:
    """Start the sidecar detached from the terminal, logging to a file."""
    package = "pocket-tts[quantize]" if quantize else "pocket-tts"
    cmd = ["uvx", "--from", package, "pocket-tts", "serve"]
    cmd += ["--host", _HOST, "--port", str(port)]
    if quantize:
        cmd.append("--quantize")
    creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    log = open(_LOG_FILE, "ab")
    return subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def ensure_server(port: int, autostart: bool = True, quantize: bool = False) -> bool:
    """Return True when a healthy server is reachable on *port*.

    Spawns our own sidecar when *autostart* is set and nothing answers.
    Blocking -- call from a worker thread, never the event loop.
    """
    global _proc
    if is_healthy(port):
        return True
    if not autostart:
        return False

    with _lock:
        if is_healthy(port):
            return True
        if _proc is None or _proc.poll() is not None:
            try:
                _proc = _spawn(port, quantize)
            except (OSError, FileNotFoundError):
                return False

    deadline = time.monotonic() + _STARTUP_WAIT_S
    while time.monotonic() < deadline:
        if is_healthy(port):
            return True
        if _proc is not None and _proc.poll() is not None:
            return False  # sidecar died; see pocket_tts_server.log
        time.sleep(1.0)
    return False


def synthesize(text: str, voice: str, port: int) -> bytes:
    """POST text to the sidecar and return complete WAV bytes.

    Raises httpx.HTTPError on transport failures or bad status.
    """
    data = {"text": text}
    if voice:
        data["voice_url"] = voice
    with httpx.stream(
        "POST",
        f"{base_url(port)}/tts",
        data=data,
        timeout=_SYNTH_TIMEOUT_S,
    ) as resp:
        resp.raise_for_status()
        return _fix_wav_header(b"".join(resp.iter_bytes()))


@contextlib.contextmanager
def open_tts_stream(text: str, voice: str, port: int):
    """Stream synthesis: yields ((rate, channels, bits), pcm_chunk_iter).

    Parses the WAV header off the live HTTP stream so playback can start
    as soon as the server produces its first audio chunk.
    """
    data = {"text": text}
    if voice:
        data["voice_url"] = voice
    with httpx.stream(
        "POST",
        f"{base_url(port)}/tts",
        data=data,
        timeout=_SYNTH_TIMEOUT_S,
    ) as resp:
        resp.raise_for_status()
        reader = _StreamReader(resp.iter_bytes())
        fmt = _read_wav_format(reader)
        yield fmt, reader.iter_rest()


class _StreamReader:
    """Tiny buffered reader over an iterator of byte chunks."""

    def __init__(self, chunks):
        self._chunks = chunks
        self._buf = b""

    def read(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                break
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def iter_rest(self):
        if self._buf:
            yield self._buf
            self._buf = b""
        yield from self._chunks


def _read_wav_format(reader: _StreamReader) -> tuple[int, int, int]:
    """Consume RIFF chunks up to 'data'; return (rate, channels, bits)."""
    riff = reader.read(12)
    if riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
        raise ValueError("sidecar response is not a WAV stream")
    rate = channels = bits = 0
    while True:
        head = reader.read(8)
        if len(head) < 8:
            raise ValueError("WAV stream ended before data chunk")
        chunk_id, size = head[:4], struct.unpack("<I", head[4:8])[0]
        if chunk_id == b"data":
            if not rate:
                raise ValueError("WAV data chunk arrived before fmt chunk")
            return rate, channels, bits
        body = reader.read(size)
        if chunk_id == b"fmt ":
            tag, channels, rate = struct.unpack("<HHI", body[:8])
            bits = struct.unpack("<H", body[14:16])[0]
            if tag != 1:
                raise ValueError(f"unsupported WAV format tag {tag}")


def _fix_wav_header(wav: bytes) -> bytes:
    """Rewrite the RIFF/data chunk sizes with the real byte counts.

    The streaming endpoint emits a placeholder header (~2GB data size)
    because it cannot know the final length up front. Strict players
    (winsound, afplay) reject that, so patch it once fully buffered.
    """
    data_pos = wav.find(b"data", 12, 256)
    if not wav.startswith(b"RIFF") or data_pos == -1:
        return wav  # not a WAV we understand; pass through untouched
    body_start = data_pos + 8
    out = bytearray(wav)
    out[4:8] = struct.pack("<I", len(wav) - 8)
    out[data_pos + 4 : body_start] = struct.pack("<I", len(wav) - body_start)
    return bytes(out)


def describe_synth_error(exc: Exception) -> str:
    """Human-friendly synthesis failure message."""
    if (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code == 400
    ):
        return (
            "sidecar rejected the request (HTTP 400) -- usually an invalid "
            "voice name or url. Run /talk voices to list built-ins."
        )
    if (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code == 500
    ):
        return (
            "sidecar error (HTTP 500). If you are using a custom/local "
            "voice, the gated voice-cloning weights are likely missing: "
            "accept the terms at https://huggingface.co/kyutai/pocket-tts, "
            "run `uvx hf auth login`, then /talk restart."
        )
    return str(exc)


def owns_server() -> bool:
    return _proc is not None and _proc.poll() is None


def stop_server() -> None:
    """Terminate the sidecar if this plugin started it."""
    global _proc
    with _lock:
        if _proc is not None and _proc.poll() is None:
            try:
                _proc.terminate()
                _proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    _proc.kill()
                except OSError:
                    pass
        _proc = None
