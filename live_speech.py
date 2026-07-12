"""Speculative live speech: speak the final response while it streams.

Consumes ``stream_event`` callbacks (part_start / part_delta / part_end)
from the MAIN agent only, buffering TextPart deltas and enqueueing
complete sentences for TTS as they arrive.

The speculation: mid-stream we cannot know whether text is the final
response or preamble to a tool call. So we speak sentences optimistically
and cancel the moment a ToolCallPart appears -- a fragment of interim
text may occasionally be voiced. Sentences shorter than the minimum
threshold never speak, which filters most interim chatter.
"""

import queue
import threading

from code_puppy.messaging import emit_warning
from code_puppy.tools.subagent_context import is_subagent

from puppy_talk import playback, server, settings
from puppy_talk.sanitize import sanitize, speakable_cut

_MIN_SENTENCE_CHARS = 60
_NO_TRUNCATION = 10**9

_queue: "queue.Queue[tuple[int, str]]" = queue.Queue()
_lock = threading.Lock()
_gen = 0
_speaker_thread: threading.Thread | None = None
_warned_server_down = False
_warned_synth_error = False

# Per-turn streaming state (event-loop thread only, guarded for stop()).
_buffers: dict[int, str] = {}
_spoken: dict[int, int] = {}
_started: set[int] = set()  # parts that cleared the min-start threshold
_spoke_this_turn = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def on_stream_event(event_type: str, event_data, session_id=None) -> None:
    """Feed one stream event into the live-speech state machine."""
    if not (settings.enabled() and settings.live()):
        return
    if is_subagent():
        return  # only the main agent's stream gets a voice

    if event_type == "part_start":
        part_type = event_data.get("part_type", "")
        if part_type == "ToolCallPart":
            _cancel_interim()
        elif part_type == "TextPart":
            idx = event_data.get("index")
            content = getattr(event_data.get("part"), "content", "") or ""
            with _lock:
                _buffers[idx] = content
                _spoken[idx] = 0
                _started.discard(idx)
            _try_extract(idx)
    elif event_type == "part_delta":
        if event_data.get("delta_type") == "TextPartDelta":
            idx = event_data.get("index")
            delta = getattr(event_data.get("delta"), "content_delta", "") or ""
            with _lock:
                if idx not in _buffers:
                    return
                _buffers[idx] += delta
            _try_extract(idx)
    elif event_type == "part_end":
        idx = event_data.get("index")
        if event_data.get("next_part_kind") is None and idx in _buffers:
            _flush_tail(idx)


def flush_turn_end() -> bool:
    """Flush remaining tails; True when live mode spoke this turn.

    Called from the interactive_turn_end hook. A True return means the
    buffered whole-response path should be skipped.
    """
    if not (settings.enabled() and settings.live()):
        return False
    with _lock:
        indices = list(_buffers)
    for idx in indices:
        _flush_tail(idx)
    with _lock:
        handled = _spoke_this_turn
    reset_turn()
    return handled


def reset_turn() -> None:
    """Clear per-turn state (new prompt, cancel, or turn end)."""
    global _spoke_this_turn
    with _lock:
        _buffers.clear()
        _spoken.clear()
        _started.clear()
        _spoke_this_turn = False


def stop() -> None:
    """Drop queued speech and halt playback."""
    _bump()
    playback.stop()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _bump() -> int:
    global _gen
    with _lock:
        _gen += 1
        return _gen


def _cancel_interim() -> None:
    """A tool call proved buffered text was not the final response."""
    global _spoke_this_turn
    _bump()
    playback.stop()
    with _lock:
        _buffers.clear()
        _spoken.clear()
        _started.clear()
        _spoke_this_turn = False


def _try_extract(idx: int) -> None:
    with _lock:
        # Hold speech until this part looks like a real final response;
        # short interim preambles before tool calls never clear the bar.
        if idx not in _started:
            if len(_buffers[idx]) < settings.live_min_start():
                return
            _started.add(idx)
        pending = _buffers[idx][_spoken[idx] :]
        cut = speakable_cut(pending, _MIN_SENTENCE_CHARS)
        if not cut:
            return
        _spoken[idx] += cut
    _enqueue(pending[:cut])


def _flush_tail(idx: int) -> None:
    with _lock:
        if idx not in _buffers:
            return
        tail = _buffers[idx][_spoken[idx] :]
        _spoken[idx] = len(_buffers[idx])
    if tail.strip():
        _enqueue(tail)


def _enqueue(raw: str) -> None:
    global _spoke_this_turn
    text = sanitize(raw, _NO_TRUNCATION)
    if not text:
        return
    with _lock:
        _spoke_this_turn = True
        gen = _gen
    _ensure_speaker()
    _queue.put((gen, text))


def _ensure_speaker() -> None:
    global _speaker_thread
    if _speaker_thread is None or not _speaker_thread.is_alive():
        _speaker_thread = threading.Thread(
            target=_speaker_loop, daemon=True, name="puppy_talk_live"
        )
        _speaker_thread.start()


def _speaker_loop() -> None:
    global _warned_server_down, _warned_synth_error
    while True:
        gen, text = _queue.get()
        with _lock:
            if gen != _gen:
                continue  # cancelled while queued
        port = settings.port()
        if not server.ensure_server(port, quantize=settings.quantize()):
            if not _warned_server_down:
                _warned_server_down = True
                emit_warning(
                    f"puppy_talk: pocket-tts sidecar unreachable on port {port}."
                )
            continue
        _warned_server_down = False
        try:
            if playback.stream_supported():
                with server.open_tts_stream(
                    text, settings.request_voice(), port
                ) as (fmt, pcm_iter):
                    with _lock:
                        if gen != _gen:
                            continue
                    rate, channels, bits = fmt
                    playback.stream_pcm(pcm_iter, rate, channels, bits)
            else:
                # No streaming backend (e.g. macOS/Linux without the
                # optional sounddevice package): buffer this sentence's
                # WAV and hand it to the OS file player instead.
                wav = server.synthesize(text, settings.request_voice(), port)
                with _lock:
                    if gen != _gen:
                        continue
                playback.play_wav_bytes(wav)
            _warned_synth_error = False  # healthy again
        except Exception as exc:
            # Drop everything queued -- one failure means the rest of
            # this turn's sentences will fail identically. Warn once,
            # then stay quiet until a synthesis succeeds.
            _bump()
            if not _warned_synth_error:
                _warned_synth_error = True
                emit_warning(
                    f"puppy_talk: live synthesis failed: "
                    f"{server.describe_synth_error(exc)} "
                    "(muting further errors until synthesis recovers; "
                    "/talk voice alba to fall back to a built-in voice)"
                )
