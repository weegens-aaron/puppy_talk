"""Zero-dependency WAV playback with stop support.

Windows: winsound (stdlib). macOS: afplay. Linux: first available of
paplay/aplay/ffplay. Playback happens on the caller's thread (the plugin
always calls from a daemon worker); stop() interrupts from any thread.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading

_lock = threading.Lock()
_generation = 0
_active_proc: subprocess.Popen | None = None
_active_stream: "_WaveOutStream | None" = None

# Batch tiny HTTP chunks into ~200ms buffers before hitting the device.
_MIN_WRITE_BYTES = 9600


def _next_generation() -> int:
    global _generation
    with _lock:
        _generation += 1
        return _generation


def _is_current(gen: int) -> bool:
    with _lock:
        return gen == _generation


def stop() -> None:
    """Interrupt any in-progress playback."""
    global _active_proc, _active_stream
    _next_generation()  # invalidate pending jobs
    if sys.platform == "win32":
        import winsound

        winsound.PlaySound(None, winsound.SND_PURGE)
    with _lock:
        proc = _active_proc
        stream = _active_stream
        _active_proc = None
        _active_stream = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    if stream is not None:
        stream.abort()


def _sounddevice_available() -> bool:
    try:
        import sounddevice  # noqa: F401

        return True
    except (ImportError, OSError):  # OSError: PortAudio lib missing
        return False


def stream_supported() -> bool:
    """True when low-latency PCM streaming is available.

    Windows always qualifies (winmm waveOut, stdlib). Other platforms
    qualify when the optional `sounddevice` package is importable.
    """
    return sys.platform == "win32" or _sounddevice_available()


def _open_pcm_stream(rate: int, channels: int, bits: int):
    """Return the best available PCM output stream for this platform."""
    if sys.platform == "win32":
        return _WaveOutStream(rate, channels, bits)
    if _sounddevice_available():
        return _SoundDeviceStream(rate, channels, bits)
    raise OSError("no PCM streaming backend available")


def stream_pcm(pcm_iter, rate: int, channels: int, bits: int) -> None:
    """Play raw PCM chunks as they arrive. Blocking; stop() interrupts."""
    global _active_stream
    gen = _next_generation()
    stream = _open_pcm_stream(rate, channels, bits)
    block = max(1, channels * bits // 8)
    with _lock:
        _active_stream = stream
    try:
        pending = b""
        for chunk in pcm_iter:
            if not _is_current(gen):
                return
            pending += chunk
            if len(pending) >= _MIN_WRITE_BYTES:
                # Only write whole sample frames; carry the remainder so a
                # chunk boundary can never split an int16 sample.
                cut = len(pending) - (len(pending) % block)
                stream.write(pending[:cut])
                pending = pending[cut:]
        if pending and _is_current(gen):
            stream.write(pending[: len(pending) - (len(pending) % block)])
        if _is_current(gen):
            stream.drain()
    except Exception:
        # stop() aborting the device mid-write raises from the backend
        # (e.g. PortAudioError). That is an intentional interruption,
        # not a failure -- only real errors propagate.
        if _is_current(gen):
            raise
    finally:
        with _lock:
            if _active_stream is stream:
                _active_stream = None
        stream.close()


def play_wav_bytes(wav: bytes) -> None:
    """Play WAV bytes, blocking until finished or stopped."""
    gen = _next_generation()
    path = _write_temp(wav)
    try:
        if _is_current(gen):
            _play_file(path, gen)
    finally:
        _cleanup(path)


def _write_temp(wav: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="puppy_talk_")
    with os.fdopen(fd, "wb") as fh:
        fh.write(wav)
    return path


def _play_file(path: str, gen: int) -> None:
    if sys.platform == "win32":
        import winsound

        # Blocking on this worker thread; stop() purges from elsewhere.
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_NODEFAULT)
        return

    cmd = _unix_player_cmd(path)
    if cmd is None:
        return
    global _active_proc
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    with _lock:
        if gen != _generation:  # stopped while spawning
            proc.terminate()
            return
        _active_proc = proc
    proc.wait()
    with _lock:
        if _active_proc is proc:
            _active_proc = None


def _unix_player_cmd(path: str) -> list[str] | None:
    if sys.platform == "darwin":
        return ["afplay", path]
    for player, args in (
        ("paplay", [path]),
        ("aplay", ["-q", path]),
        ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet", path]),
    ):
        if shutil.which(player):
            return [player, *args]
    return None


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass  # Windows may still hold the file briefly after SND_PURGE


# ---------------------------------------------------------------------------
# Windows waveOut streaming (ctypes over winmm.dll -- stdlib only)
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes as _wt

    _winmm = ctypes.windll.winmm
    _WAVE_MAPPER = ctypes.c_uint(-1 & 0xFFFFFFFF)
    _WHDR_DONE = 0x00000001

    class _WAVEFORMATEX(ctypes.Structure):
        _fields_ = [
            ("wFormatTag", _wt.WORD),
            ("nChannels", _wt.WORD),
            ("nSamplesPerSec", _wt.DWORD),
            ("nAvgBytesPerSec", _wt.DWORD),
            ("nBlockAlign", _wt.WORD),
            ("wBitsPerSample", _wt.WORD),
            ("cbSize", _wt.WORD),
        ]

    class _WAVEHDR(ctypes.Structure):
        _fields_ = [
            ("lpData", ctypes.c_void_p),
            ("dwBufferLength", _wt.DWORD),
            ("dwBytesRecorded", _wt.DWORD),
            ("dwUser", ctypes.c_size_t),
            ("dwFlags", _wt.DWORD),
            ("dwLoops", _wt.DWORD),
            ("lpNext", ctypes.c_void_p),
            ("reserved", ctypes.c_size_t),
        ]

    class _WaveOutStream:
        """Minimal push-model PCM output device."""

        def __init__(self, rate: int, channels: int, bits: int):
            block_align = channels * bits // 8
            fmt = _WAVEFORMATEX(
                1,  # WAVE_FORMAT_PCM
                channels,
                rate,
                rate * block_align,
                block_align,
                bits,
                0,
            )
            self._handle = _wt.HANDLE()
            self._pending: list[tuple[_WAVEHDR, ctypes.Array]] = []
            self._closed = False
            rc = _winmm.waveOutOpen(
                ctypes.byref(self._handle), _WAVE_MAPPER, ctypes.byref(fmt), 0, 0, 0
            )
            if rc != 0:
                raise OSError(f"waveOutOpen failed with code {rc}")

        def write(self, data: bytes) -> None:
            buf = ctypes.create_string_buffer(data, len(data))
            hdr = _WAVEHDR()
            hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
            hdr.dwBufferLength = len(data)
            _winmm.waveOutPrepareHeader(
                self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr)
            )
            _winmm.waveOutWrite(self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr))
            self._pending.append((hdr, buf))
            self._reap()

        def _reap(self) -> None:
            still = []
            for hdr, buf in self._pending:
                if hdr.dwFlags & _WHDR_DONE:
                    _winmm.waveOutUnprepareHeader(
                        self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr)
                    )
                else:
                    still.append((hdr, buf))
            self._pending = still

        def drain(self) -> None:
            import time

            while self._pending and not self._closed:
                self._reap()
                time.sleep(0.05)

        def abort(self) -> None:
            if not self._closed:
                _winmm.waveOutReset(self._handle)  # marks all buffers done

        def close(self) -> None:
            if self._closed:
                return
            self._closed = True
            _winmm.waveOutReset(self._handle)
            self._reap()
            _winmm.waveOutClose(self._handle)
else:

    class _WaveOutStream:  # pragma: no cover - non-Windows placeholder
        def __init__(self, *args):
            raise OSError("waveOut streaming is Windows-only")


class _SoundDeviceStream:
    """PCM output via the optional `sounddevice` package (PortAudio).

    Same interface as _WaveOutStream: write / drain / abort / close.
    RawOutputStream.write() blocks for backpressure; stop() waits for
    buffered audio to finish; abort() discards it immediately.
    """

    def __init__(self, rate: int, channels: int, bits: int):
        if bits != 16:
            raise OSError(f"sounddevice backend expects 16-bit PCM, got {bits}")
        import sounddevice as sd

        self._stream = sd.RawOutputStream(
            samplerate=rate, channels=channels, dtype="int16"
        )
        self._stream.start()
        self._closed = False

    def write(self, data: bytes) -> None:
        if not self._closed and data:
            self._stream.write(data)

    def drain(self) -> None:
        if not self._closed:
            self._stream.stop()  # blocks until buffered audio has played

    def abort(self) -> None:
        if not self._closed:
            try:
                self._stream.abort()
            except Exception:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.abort()
            self._stream.close()
        except Exception:
            pass
