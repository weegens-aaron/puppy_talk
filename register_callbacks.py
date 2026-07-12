"""puppy_talk -- speak Code Puppy's final response with pocket-tts.

Architecture (see research/tech/pocket-tts-code-puppy-plugin-research.md):
a `uvx pocket-tts serve` sidecar owns the model in its own uv env; this
plugin only sends HTTP and streams PCM to the sound card. All slow work
runs on daemon threads so the REPL never waits on a talking dog.

Speech paths, fastest first:
1. live      -- sentences speak while the response is still streaming
                (stream_event hook; speculative, see live_speech.py)
2. streaming -- at turn end, PCM plays as the sidecar generates it
3. pipeline  -- buffered fallback: chunk N plays while N+1 synthesizes

Commands:
    /talk on|off        enable/disable speech (THIS instance only, not persisted)
    /talk status        show settings and sidecar health
    /talk stop          interrupt current speech
    /talk restart       restart the sidecar (applies talk_quantize etc.)
    /talk live on|off   speak during streaming vs only at turn end
    /talk voice <name>  set voice (alba, giovanni, lola, or hf:// url)
    /talk voices        list built-in voices
    /talk set [k v]     show or edit settings (stored in puppy_talk.json)
    /talk say <text>    speak arbitrary text (test)

Persisted preferences live in puppy_talk.json next to this plugin --
never in code-puppy's shared puppy.cfg.
"""

import difflib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from code_puppy.callbacks import register_callback
from code_puppy.messaging import emit_info, emit_warning

from puppy_talk import live_speech, playback, server, settings, voice_host
from puppy_talk.sanitize import sanitize, split_speech_chunks

_warned_server_down = False

# Bumped to cancel an in-flight turn-end speak pipeline between chunks.
_speak_gen = 0
_speak_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Cancellation plumbing
# ---------------------------------------------------------------------------


def _bump_gen() -> int:
    global _speak_gen
    with _speak_lock:
        _speak_gen += 1
        return _speak_gen


def _gen_current(gen: int) -> bool:
    with _speak_lock:
        return gen == _speak_gen


def _stop_speaking() -> None:
    _bump_gen()
    live_speech.stop()
    playback.stop()


# ---------------------------------------------------------------------------
# Turn-end speech (used when live mode is off or spoke nothing)
# ---------------------------------------------------------------------------


def _speak_worker(text: str, gen: int) -> None:
    global _warned_server_down
    port = settings.port()
    if not server.ensure_server(port, quantize=settings.quantize()):
        if not _warned_server_down:
            _warned_server_down = True
            emit_warning(
                "puppy_talk: pocket-tts sidecar unreachable on port "
                f"{port}. Is uvx on PATH? See pocket_tts_server.log; "
                "disable with /talk off."
            )
        return
    _warned_server_down = False

    voice = settings.request_voice()
    if playback.stream_supported() and _stream_speak(text, voice, port, gen):
        return
    _pipelined_speak(text, voice, port, gen)


def _stream_speak(text: str, voice: str, port: int, gen: int) -> bool:
    """Low-latency path: play PCM as the sidecar generates it.

    Returns True when handled (even on synth error); False means the
    caller should fall back to the buffered pipeline.
    """
    try:
        with server.open_tts_stream(text, voice, port) as (fmt, pcm_iter):
            if not _gen_current(gen):
                return True
            rate, channels, bits = fmt
            playback.stream_pcm(pcm_iter, rate, channels, bits)
        return True
    except ValueError:
        return False  # unexpected stream format; buffered path handles it
    except OSError:
        return False  # audio device refused; try the file-based player
    except Exception as exc:
        emit_warning(f"puppy_talk: {server.describe_synth_error(exc)}")
        return True


def _pipelined_speak(text: str, voice: str, port: int, gen: int) -> None:
    """Buffered fallback: synthesize chunk N+1 while chunk N plays."""
    chunks = split_speech_chunks(text)
    if not chunks:
        return

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="puppy_tts") as pool:
        future = pool.submit(server.synthesize, chunks[0], voice, port)
        for i in range(len(chunks)):
            try:
                wav = future.result()
            except Exception as exc:  # httpx errors, bad voice names, etc.
                emit_warning(f"puppy_talk: {server.describe_synth_error(exc)}")
                return
            if not _gen_current(gen):
                return
            if i + 1 < len(chunks):
                future = pool.submit(server.synthesize, chunks[i + 1], voice, port)
            playback.play_wav_bytes(wav)
            if not _gen_current(gen):
                return


def _speak_async(text: str) -> None:
    gen = _bump_gen()  # cancels any in-flight pipeline
    playback.stop()
    threading.Thread(
        target=_speak_worker, args=(text, gen), daemon=True, name="puppy_talk"
    ).start()


def _warmup_worker() -> None:
    port = settings.port()
    if server.is_healthy(port):
        emit_info("puppy_talk: pocket-tts sidecar is ready.")
        return
    emit_info(
        "puppy_talk: starting pocket-tts sidecar (first run downloads "
        "the model -- this can take a few minutes)..."
    )
    if server.ensure_server(port, quantize=settings.quantize()):
        emit_info("puppy_talk: pocket-tts sidecar is ready. Woof.")
    else:
        emit_warning(
            "puppy_talk: sidecar failed to start. Check "
            "pocket_tts_server.log next to the plugin."
        )


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


async def _on_stream_event(event_type, event_data, agent_session_id=None):
    live_speech.on_stream_event(event_type, event_data, agent_session_id)


def _on_user_prompt_submit(*args, **kwargs):
    """New prompt: silence any leftover speech from the previous turn."""
    if settings.enabled():
        _stop_speaking()
    live_speech.reset_turn()


def _on_turn_end(agent, prompt, result=None, *, success=True, error=None):
    """Speak the final response. Always returns None (no continuation)."""
    del agent, prompt
    if not settings.enabled() or not success or error is not None or result is None:
        live_speech.reset_turn()
        return None
    if live_speech.flush_turn_end():
        return None  # live mode already spoke while streaming
    text = sanitize(str(getattr(result, "output", "")), settings.max_chars())
    if text:
        _speak_async(text)
    return None


def _on_turn_cancel(prompt, *, reason="cancelled"):
    del prompt, reason
    _stop_speaking()
    live_speech.reset_turn()


def _on_shutdown():
    _stop_speaking()
    server.stop_server()


# ---------------------------------------------------------------------------
# /talk command
# ---------------------------------------------------------------------------


def _cmd_help():
    return [
        (
            "talk",
            "Speak responses via pocket-tts "
            "(on|off|status|stop|restart|live|voice|say)",
        )
    ]


_MEDIA_EXTENSIONS = (
    ".wav", ".mp3", ".mp4", ".m4a", ".mkv", ".webm", ".mov",
    ".flac", ".ogg", ".aac", ".opus",
)


def _looks_local(value: str) -> bool:
    return any(sep in value for sep in "/\\") or value.lower().endswith(
        _MEDIA_EXTENSIONS
    )


def _validate_voice(value: str) -> str | None:
    """Return an error message for a bad built-in/URL voice, or None."""
    if value.startswith(("hf://", "http://", "https://")):
        return None  # let the sidecar judge URLs
    if value in settings.BUILTIN_VOICES:
        return None
    close = difflib.get_close_matches(value, settings.BUILTIN_VOICES, n=3, cutoff=0.6)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    return f"unknown voice '{value}'.{hint} Run /talk voices to list built-ins."


def _import_local_voice(raw_path: str) -> str:
    """Convert + host a local media file; returns the user-facing reply."""
    source = Path(raw_path).expanduser()
    if not source.exists():
        return f"puppy_talk: file not found: {raw_path}"
    emit_info(
        "puppy_talk: importing voice (extracts a 30s mono WAV; first "
        "run downloads ffmpeg via uvx -- give it a minute)..."
    )
    try:
        filename = voice_host.import_voice(source)
    except Exception as exc:
        return f"puppy_talk: voice import failed: {exc}"
    settings.set_value("voice", f"local:{filename}")
    port = settings.voice_host_port()
    if not voice_host.ensure_serving(port):
        return (
            f"puppy_talk: imported '{filename}' but could not serve it on "
            f"port {port}. Change it with /talk set voice_host_port <n>."
        )
    return (
        f"puppy_talk: voice set to local file '{filename}' "
        f"(served at {voice_host.url_for(filename, port)})"
    )


def _handle_talk(command: str, name: str):
    if name != "talk":
        return None
    parts = command.split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else "status"
    arg = parts[2] if len(parts) > 2 else ""

    if sub == "on":
        settings.set_enabled(True)
        threading.Thread(target=_warmup_worker, daemon=True).start()
        return (
            "puppy_talk: enabled for THIS code-puppy instance. "
            "Other instances stay silent unless you /talk on there too."
        )
    if sub == "off":
        settings.set_enabled(False)
        _stop_speaking()
        return "puppy_talk: disabled for this instance."
    if sub == "stop":
        _stop_speaking()
        return "puppy_talk: playback stopped."
    if sub == "restart":
        _stop_speaking()
        if server.owns_server():
            server.stop_server()
            threading.Thread(target=_warmup_worker, daemon=True).start()
            return "puppy_talk: restarting sidecar with current settings..."
        return (
            "puppy_talk: I did not start this sidecar, so I will not kill it. "
            "Stop it yourself, then /talk on."
        )
    if sub == "live":
        mode = arg.strip().lower()
        if mode in ("on", "off"):
            settings.set_value("live", mode == "on")
            return f"puppy_talk: live streaming speech {mode}."
        state = "on" if settings.live() else "off"
        return f"puppy_talk: live mode is {state}. Use /talk live on|off"
    if sub == "set":
        pieces = arg.split(maxsplit=1)
        if len(pieces) != 2:
            pairs = ", ".join(f"{k}={v}" for k, v in settings.snapshot().items())
            return (
                f"puppy_talk settings ({settings.CONFIG_PATH.name}): {pairs} | "
                "usage: /talk set <key> <value>"
            )
        key, value = pieces[0].strip(), pieces[1].strip()
        try:
            settings.set_value(key, value)
        except KeyError:
            return (
                f"puppy_talk: unknown key '{key}'. "
                f"Known keys: {', '.join(settings.DEFAULTS)}"
            )
        except ValueError:
            return f"puppy_talk: could not parse '{value}' for '{key}'."
        return f"puppy_talk: {key} = {settings.snapshot()[key]}"
    if sub == "voices":
        english = settings.BUILTIN_VOICES[:-5]
        other = settings.BUILTIN_VOICES[-5:]
        return (
            "puppy_talk built-in voices (English): "
            + ", ".join(english)
            + " | other languages (need matching sidecar --language): "
            + ", ".join(other)
            + " | also accepts hf:// urls and local wav/mp3 paths. "
            "Audition them at http://127.0.0.1:"
            f"{settings.port()} or https://huggingface.co/kyutai/tts-voices"
        )
    if sub == "voice":
        if not arg:
            return f"puppy_talk: current voice is '{settings.voice()}'."
        candidate = arg.strip().strip('"')
        if _looks_local(candidate):
            return _import_local_voice(candidate)
        problem = _validate_voice(candidate)
        if problem:
            return f"puppy_talk: {problem}"
        settings.set_value("voice", candidate)
        return f"puppy_talk: voice set to '{candidate}'."
    if sub == "say":
        if not arg:
            return "puppy_talk: usage: /talk say <text>"
        _speak_async(sanitize(arg, settings.max_chars()) or arg)
        return "puppy_talk: speaking..."
    if sub == "status":
        port = settings.port()
        healthy = "up" if server.is_healthy(port) else "down"
        state = "on (this session)" if settings.enabled() else "off"
        live = "on" if settings.live() else "off"
        return (
            f"puppy_talk: {state} | live: {live} | voice: {settings.voice()} | "
            f"sidecar: {healthy} (port {port}) | max chars: {settings.max_chars()}"
        )
    return (
        "puppy_talk: unknown subcommand. Use /talk "
        "on|off|status|stop|restart|live|voices|voice <name>|set <k> <v>|say <text>"
    )


register_callback("interactive_turn_end", _on_turn_end)
register_callback("interactive_turn_cancel", _on_turn_cancel)
register_callback("stream_event", _on_stream_event)
register_callback("user_prompt_submit", _on_user_prompt_submit)
register_callback("shutdown", _on_shutdown)
register_callback("custom_command", _handle_talk)
register_callback("custom_command_help", _cmd_help)
