"""JSON-backed config for puppy_talk.

Persisted preferences live in puppy_talk.json next to the plugin --
NOT in code-puppy's shared puppy.cfg. Activation (/talk on) remains
process-local and is never persisted anywhere.
"""

import json
import threading
from pathlib import Path

from puppy_talk import server
from puppy_talk.sanitize import DEFAULT_MAX_CHARS

CONFIG_PATH = Path(__file__).parent / "puppy_talk.json"

DEFAULT_VOICE = "alba"  # the sidecar preloads this voice at startup

# Built-in voice names, mirrored from pocket-tts v2.1.0
# (pocket_tts/utils/utils.py :: _ORIGINS_OF_PREDEFINED_VOICES).
BUILTIN_VOICES = (
    "alba", "anna", "azelma", "bill_boerst", "caro_davy", "charles",
    "cosette", "eponine", "eve", "fantine", "george", "jane", "javert",
    "jean", "marius", "mary", "michael", "paul", "peter_yearsley",
    "stuart_bell", "vera",
    # non-English (need the matching sidecar --language):
    "giovanni", "lola", "juergen", "rafael", "estelle",
)

# Defaults double as the schema: keys and value types.
DEFAULTS = {
    "voice": DEFAULT_VOICE,
    "port": server.DEFAULT_PORT,
    "voice_host_port": 8918,
    "live": True,
    "live_min_start": 300,
    "max_chars": DEFAULT_MAX_CHARS,
    "quantize": False,
}

# Activation is PROCESS-LOCAL, never persisted: with multiple code-puppy
# instances running, only the instance where /talk on was typed speaks.
_session_enabled = False

_lock = threading.Lock()
_cache: dict | None = None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _load_file() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _get(key: str):
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load_file()
        return _cache.get(key, DEFAULTS[key])


def set_value(key: str, value) -> None:
    """Persist one preference, coercing to the schema type."""
    global _cache
    if key not in DEFAULTS:
        raise KeyError(f"unknown puppy_talk setting: {key}")
    default = DEFAULTS[key]
    if isinstance(default, bool):
        value = str(value).strip().lower() in ("true", "1", "on", "yes")
    elif isinstance(default, int):
        value = int(value)
    else:
        value = str(value)
    with _lock:
        data = _load_file()
        data[key] = value
        CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        _cache = data


def snapshot() -> dict:
    """Current effective settings (defaults overlaid with the file)."""
    return {key: _get(key) for key in DEFAULTS}


# ---------------------------------------------------------------------------
# Typed accessors
# ---------------------------------------------------------------------------


def enabled() -> bool:
    return _session_enabled


def set_enabled(value: bool) -> None:
    global _session_enabled
    _session_enabled = value


def voice() -> str:
    return str(_get("voice")).strip() or DEFAULT_VOICE


def request_voice() -> str:
    """Voice to send per-request; empty for the server's warm default.

    Local voices ("local:<file>") resolve to a URL on the plugin's own
    voice server so the sidecar can fetch AND cache the voice state.
    """
    v = voice()
    if v == DEFAULT_VOICE:
        return ""
    if v.startswith("local:"):
        from puppy_talk import voice_host  # deferred: avoids import cycle

        p = voice_host_port()
        voice_host.ensure_serving(p)
        return voice_host.url_for(v[len("local:") :], p)
    return v


def voice_host_port() -> int:
    return int(_get("voice_host_port"))


def port() -> int:
    return int(_get("port"))


def live() -> bool:
    """Speak sentences while the final response is still streaming."""
    return bool(_get("live"))


def live_min_start() -> int:
    """Chars a streaming text part must reach before live speech starts."""
    return int(_get("live_min_start"))


def max_chars() -> int:
    return int(_get("max_chars"))


def quantize() -> bool:
    return bool(_get("quantize"))
