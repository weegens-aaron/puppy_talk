"""Local voice files for the pocket-tts sidecar.

The sidecar's /tts only accepts voice_url values that are built-in names
or http/https/hf URLs, and it only decodes WAV (stdlib wave module; no
soundfile installed). It caches voice states per URL -- uploads via
voice_wav are reprocessed on every request, which is poison for live
mode's per-sentence calls.

So local voices work like this:
  1. import_voice() converts any audio/video file to a 30s mono WAV in
     ./voices/ (via `uvx static-ffmpeg`; plain WAVs are just copied).
     Filenames embed a source hash so edits bust the sidecar's cache.
  2. ensure_serving() runs a tiny stdlib HTTP server over ./voices/ so
     the sidecar can fetch (and cache) the voice by URL.
"""

import hashlib
import http.server
import shutil
import subprocess
import threading
from functools import partial
from pathlib import Path

import httpx

VOICES_DIR = Path(__file__).parent / "voices"
DEFAULT_VOICE_HOST_PORT = 8918

_CLIP_SECONDS = "30"  # voice prompt length cap; longer = slower processing
_CONVERT_TIMEOUT_S = 600  # first run downloads static-ffmpeg binaries

_server: http.server.ThreadingHTTPServer | None = None
_lock = threading.Lock()


def url_for(filename: str, port: int) -> str:
    return f"http://127.0.0.1:{port}/{filename}"


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # no REPL spam per sidecar fetch
        pass


def ensure_serving(port: int) -> bool:
    """Serve ./voices/ on 127.0.0.1:*port*; True when something does."""
    global _server
    with _lock:
        if _server is not None:
            return True
        VOICES_DIR.mkdir(exist_ok=True)
        handler = partial(_QuietHandler, directory=str(VOICES_DIR))
        try:
            _server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            # Port taken -- likely another code-puppy instance serving the
            # same directory. Probe it before giving up.
            return _probe(port)
        threading.Thread(
            target=_server.serve_forever, daemon=True, name="puppy_talk_voices"
        ).start()
        return True


def _probe(port: int) -> bool:
    try:
        return httpx.get(url_for("", port), timeout=2.0).status_code < 500
    except httpx.HTTPError:
        return False


# ---------------------------------------------------------------------------
# Importing
# ---------------------------------------------------------------------------


def import_voice(source: Path) -> str:
    """Convert/copy *source* into ./voices/; return the served filename.

    Non-WAV sources (mp3, mp4, mkv, ...) are converted to 24kHz mono WAV,
    capped at the first 30 seconds. Re-imports of an unchanged source
    reuse the existing file.
    """
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(str(source))
    VOICES_DIR.mkdir(exist_ok=True)

    stat = source.stat()
    digest = hashlib.sha1(
        f"{source}|{stat.st_size}|{stat.st_mtime_ns}".encode()
    ).hexdigest()[:8]
    safe_stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in source.stem)
    filename = f"{safe_stem}-{digest}.wav"
    target = VOICES_DIR / filename
    if target.exists():
        return filename  # unchanged source already imported

    if source.suffix.lower() == ".wav":
        shutil.copyfile(source, target)
        return filename

    cmd = [
        "uvx", "--from", "static-ffmpeg", "static_ffmpeg",
        "-y", "-i", str(source),
        "-vn", "-ac", "1", "-ar", "24000", "-t", _CLIP_SECONDS,
        "-acodec", "pcm_s16le",
        str(target),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=_CONVERT_TIMEOUT_S
    )
    if result.returncode != 0 or not target.exists():
        tail = (result.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError(
            f"audio conversion failed (exit {result.returncode}): "
            + " | ".join(tail)
        )
    return filename
