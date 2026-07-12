# Technical Research: Building a pocket-tts "read the final response aloud" plugin for Code Puppy

- **Date**: 2026-07-11 (system date)
- **Researcher**: Rowsdower  (code-puppy-0a23f3)
- **Local ground truth**: code-puppy v0.0.634 installed at
  `C:\Users\weege\AppData\Local\uv\cache\archive-v0\...\site-packages\code_puppy`
  (inspected directly — Tier 1 source), Python 3.13.13, Windows.

## Executive Summary

- **Question**: What is the best way to build a Code Puppy plugin that speaks
  the agent's *final* response (once, at the end of each interactive turn)
  using kyutai-labs/pocket-tts?
- **Key Finding**: Code Puppy has a first-class user-plugin system
  (`~/.code_puppy/plugins/<name>/register_callbacks.py`) with an
  `interactive_turn_end` hook that fires exactly once per completed
  interactive turn and receives the pydantic-ai run result — the final
  response text is `str(getattr(result, "output", ""))`. pocket-tts (MIT,
  7.3k, active) is a 100M-param CPU-only model with a streaming Python API
  (`generate_audio_stream`) and, alternatively, a FastAPI server
  (`pocket-tts serve`, `POST /tts`).
- **Recommendation**: Build a user plugin in `~/.code_puppy/plugins/puppy_talk/`
  registering an `interactive_turn_end` callback (plus a `/talk` custom
  command for on/off). For audio: **in-process pocket-tts +
  `sounddevice.OutputStream`** if you're willing to add CPU torch to the
  code_puppy environment; otherwise the **`pocket-tts serve` sidecar over
  HTTP** keeps dependencies out of code_puppy's uv tool env entirely.

## The Two Halves of the Problem

### Half 1: Hooking "final response on end of interactive turn" in Code Puppy

**Verified against installed source (v0.0.634) — exact hook exists.**

Code Puppy's callback registry (`code_puppy/callbacks.py`, ~1362 lines)
defines 60+ phases. The relevant one is literally named
`interactive_turn_end`:

```python
# code_puppy/callbacks.py (v0.0.634, line 1271)
async def on_interactive_turn_end(
    agent,
    prompt: str,
    result: Any = None,
    *,
    success: bool = True,
    error: Optional[BaseException] = None,
) -> List[Any]:
    """Fired after an interactive prompt run completes.

    Plugins may return a continuation request dict ...
    The CLI owns execution; plugins own policy.
    """
```

Trigger site (`code_puppy/cli_runner.py`, ~line 1200): fired once after the
interactive REPL turn finishes, after the response has been rendered, with
`result` being the pydantic-ai run result whose `.output` is the final
response text — the CLI itself renders `agent_response = result.output`
(cli_runner.py ~line 1131). It is **not** fired for cancelled turns
(`interactive_turn_cancel` fires instead) and it is **not** fired per
stream-chunk (that's `stream_event`) — so this hook precisely matches the
"just the final response" requirement.

**Precedent inside the codebase**: the built-in `wiggum` plugin registers
this exact hook and extracts the response text with:

```python
# code_puppy/plugins/wiggum/register_callbacks.py, line 185
return str(getattr(result, "output", "")) if result is not None else None
...
register_callback("interactive_turn_end", _on_interactive_turn_end)
```

**Plugin loading convention** (`code_puppy/plugins/__init__.py`):

- Built-in plugins live in the package; **user plugins** live in
  `~/.code_puppy/plugins/<plugin_name>/` and MUST contain a
  `register_callbacks.py` file, which is imported at startup
  (`_load_user_plugins`). Directories starting with `_` or `.` are skipped.
- Callbacks are registered via
  `from code_puppy.callbacks import register_callback`.
- Both sync and async callbacks are supported; each callback is wrapped in
  try/except for error isolation (a crashing TTS callback won't kill the REPL,
  but slow *blocking* work in it will stall the loop — see Considerations).
- The target directory `C:\Users\weege\.code_puppy\plugins\puppy_talk\`
  already exists (currently just a `.gitignore`). 

**Toggle + UX hooks** (all verified in installed source):

- `custom_command` + `custom_command_help` phases let the plugin add a
  `/talk on|off|voice <name>` command. Return-value contract (documented in
  `plugins/example_custom_command/register_callbacks.py`): `None` = not ours,
  `True` = handled, `str` = display-only message.
- `code_puppy.config.get_value(key)` / `set_config_value(key, value)` persist
  settings (e.g., `talk_enabled`, `talk_voice`) across sessions.
- `interactive_turn_cancel` can be used to stop playback when the user
  cancels a turn mid-speech.
- **Important**: return `None` from the `interactive_turn_end` callback —
  returning a dict is interpreted as a *continuation request* that re-runs
  the agent (that's how wiggum loops). A TTS plugin must not do that.

### Half 2: Generating and playing the speech with pocket-tts

**pocket-tts facts** (via qa-kitten; official repo/docs/pyproject):

| Attribute | Value |
|---|---|
| Repo | https://github.com/kyutai-labs/pocket-tts — 7,357, 739 forks |
| Activity | Last commit 2026-06-23 (repo created 2026-01) — active |
| License | MIT |
| Version | 2.1.0 on PyPI (`pip install pocket-tts`) |
| Model | 100M params, CPU-only (2 cores), no GPU needed |
| Python | 3.10–3.14 ( matches local 3.13.13) |
| Heavy dep | `torch>=2.5.0` (CPU build is sufficient) |
| Output | 24 kHz mono PCM as torch tensor |
| Latency | ~200 ms to first audio chunk; ~6× real-time on M4 (per README) |
| Streaming | `generate_audio_stream()` yields chunks as decoded |
| Quantization | optional int8 (`pocket-tts[quantize]`): ~48% less RAM, ~27% faster on x86 per README |
| Voices | built-ins (`alba`, `giovanni`, `lola`, ...) or any wav / `hf://kyutai/tts-voices/...`; voice states exportable to `.safetensors` for instant reload |

Canonical in-process usage (from official README/docs):

```python
from pocket_tts import TTSModel

model = TTSModel.load_model()                      # once, lazily
voice = model.get_state_for_audio_prompt("alba")   # once; cache as .safetensors
for chunk in model.generate_audio_stream(voice, text):
    ...  # 1-D float tensor chunks @ model.sample_rate (24000)
```

**Server alternative** (`pocket-tts serve`, FastAPI/uvicorn, default
`localhost:8000`): `POST /tts` with form fields `text` and optional
`voice_url`, returns chunked `audio/wav`; `GET /health` for liveness.
Verified against `pocket_tts/main.py` and the serve docs page.

## Options Found

### Option A: In-process — pocket-tts + sounddevice inside the plugin  recommended if deps are acceptable

- **What it is**: `register_callbacks.py` lazily loads `TTSModel` (first use,
  in a background thread), strips markdown from `result.output`, streams
  chunks from `generate_audio_stream()` into a `sounddevice.OutputStream`
  (24 kHz, mono, float32) on a dedicated playback thread.
- **Audio playback**: `sounddevice` 0.5.5 (released 2026-01-23, active,
  PortAudio bundled, **Windows wheels for win32/amd64/arm64 — no compiler**)
  supports exactly this via `OutputStream.write(numpy_chunk)`.
  Rejected alternatives: `simpleaudio` (archived, last release 2019),
  `pyaudio` (0.2.14 from 2023; Windows wheel situation is painful).
  `miniaudio` 1.71 (2026-04-29) is a viable backup.
- **Tradeoffs**:
  -  Lowest latency (~200 ms to first sound), no subprocess management,
    true streaming, works offline after first model download.
  -  Must install `pocket-tts` + `sounddevice` **into code_puppy's own
    environment**. Code Puppy here is installed via a uv tool/uvx environment
    (uv cache path), so that means
    `uv tool install code-puppy --with pocket-tts --with sounddevice`
    (re-run after upgrades) — user plugins have no dependency manifest of
    their own.
  -  Pulls CPU torch (hundreds of MB) into the agent's env; small risk of
    version friction with code_puppy's pinned deps (both need pydantic>=2 —
    compatible today).
  -  Model load takes seconds on first use → lazy-load off the main loop.
- **Established pattern for optional deps**: guard imports and instruct the
  user, exactly like the built-in `dbos_durable_exec` plugin does
  (`except ImportError: ... "Install with: pip install 'code-puppy[durable]'"`).

### Option B: Sidecar — `pocket-tts serve` + tiny HTTP client in the plugin

- **What it is**: run `pocket-tts serve` (via `uvx pocket-tts serve`) as a
  separate process; the plugin POSTs the final response text to
  `http://localhost:8000/tts` and plays the returned chunked WAV.
- **Tradeoffs**:
  -  Zero heavy deps in code_puppy's env — `httpx` is already a code_puppy
    dependency; `uvx` manages pocket-tts's env separately. Model stays warm
    across code_puppy restarts.
  -  Cleanest isolation; survives code_puppy upgrades untouched.
  -  Still needs *some* playback path: either `sounddevice` (same dep
    question, though it's a tiny pure-wheel install) or zero-dep fallbacks —
    write WAV to a temp file and play with `winsound.PlaySound` (stdlib,
    Windows-only) / `afplay` (macOS) / `aplay`/`paplay` (Linux). The
    temp-file route loses streaming (wait for full WAV) but keeps the plugin
    dependency-free.
  -  Sidecar lifecycle: plugin should auto-start it on `startup` (background
    subprocess), poll `GET /health`, and not double-start. More moving parts.

### Option C: Fire-and-forget CLI — `uvx pocket-tts generate` per turn

- **What it is**: shell out `uvx pocket-tts generate --text "..." --voice alba`
  to produce a WAV, then play it with the OS player.
- **Tradeoffs**:  simplest possible code, zero deps.  model loads from
  scratch *every turn* (seconds of delay before any audio), no streaming,
  awkward quoting/length limits for long responses on Windows command lines.
  Fine for a prototype; annoying daily.

## Industry Perspective

- The official pocket-tts README explicitly pitches the in-process API as the
  primary integration path ("generating audio is just a pip install and a
  function call away") and ships `serve` as the decoupled alternative; both
  are documented first-class, so A vs. B is a deployment-preference call,
  not a support-risk call.
- For playing raw PCM from Python, the actively maintained consensus choice
  is `sounddevice` (PortAudio wheels, `OutputStream` designed for exactly
  this chunked-write pattern per its official examples, e.g.
  `examples/play_stream.py`). `simpleaudio` is archived and `pyaudio` is
  stagnant — both fail the currency test.
- Inside Code Puppy itself, the strongest signal is precedent: built-in
  plugins (`wiggum`, `herdr`) already consume `interactive_turn_end`, and
  `dbos_durable_exec` demonstrates the sanctioned optional-dependency
  pattern. Following existing in-repo conventions is the lowest-risk path.

## Recommended Architecture (Option A, with B as fallback)

```
~/.code_puppy/plugins/puppy_talk/
├── register_callbacks.py   # hooks: interactive_turn_end, interactive_turn_cancel,
│                           #        custom_command(+help), startup (warm model opt-in)
├── tts_engine.py           # lazy TTSModel singleton, voice-state cache (.safetensors),
│                           #        text sanitizer (strip code blocks/markdown/emoji)
└── playback.py             # sounddevice OutputStream on a worker thread, stop() support
```

Key design points (each traced to a verified constraint):

1. **Hook**: `register_callback("interactive_turn_end", _speak)`; in `_speak`,
   bail out fast when `not success`, `error is not None`, result is `None`,
   or the `talk_enabled` config flag is off. Extract text via
   `str(getattr(result, "output", ""))` (wiggum idiom). **Return `None`.**
2. **Never block the turn**: do model load, generation, and playback on a
   daemon thread; the callback should return immediately. The REPL prompt
   comes back while the puppy talks.
3. **Sanitize before speaking**: strip fenced code blocks (replace with
   "code block omitted"), markdown syntax, and URLs; optionally cap length.
   Reading raw markdown aloud is misery.
4. **Interrupt support**: register `interactive_turn_cancel` (and handle a
   new turn starting) to `stop()` current playback — nobody wants two
   overlapping robot voices.
5. **Config**: `talk_enabled`, `talk_voice` via
   `code_puppy.config.get_value/set_config_value`; `/talk` custom command to
   flip them at runtime.
6. **Graceful degradation**: `try: import pocket_tts, sounddevice
   except ImportError:` → register only a `/talk` command that prints install
   instructions (`uv tool install code-puppy --with pocket-tts --with sounddevice`),
   mirroring `dbos_durable_exec`.
7. **Voice-state caching**: on first use, export the voice state with
   `export_model_state(...)` to `~/.code_puppy/plugins/puppy_talk/voice.safetensors`
   — official docs state reloading a `.safetensors` state skips computation.

## Considerations for Your Decision

- **Maturity**: Both projects are production-active (code_puppy: commits
  hours old, 2,030 commits, 645; pocket-tts: v2.1.0, commit 2026-06-23,
  7.3k). The `interactive_turn_end` API is used by shipped built-in plugins,
  so it's unlikely to churn silently.
- **Environment reality (the big fork in the road)**: this code_puppy runs
  from a **uv tool environment**. Option A requires `--with` injections that
  must be repeated on upgrade; Option B keeps envs fully separate at the cost
  of sidecar lifecycle code. If you upgrade code_puppy often and hate re-adding
  `--with`, pick B.
- **Latency budget**: A ≈ 200 ms to first audio (after one-time model load);
  B adds HTTP+WAV-buffering overhead (still ~1–2 s, model stays warm);
  C pays full model load every turn.
- **Windows specifics**: everything checked ships Windows wheels
  (torch CPU, sounddevice, pocket-tts is pure Python). `winsound` is a
  zero-dep Windows fallback for Option B/C.
- **Team fit**: it's your personal plugin dir — Option A's simplicity inside
  one process is worth the env coupling unless it bites you.

## Gaps and Caveats

- **Exact first-load time** of pocket-tts on this machine (model download
  ~100M params from HuggingFace + torch init) was not benchmarked — measure
  before deciding whether to warm the model on `startup` vs. first use.
- **torch/pydantic co-habitation** with code-puppy 0.0.634's pins was
  reasoned from version ranges, not tested with an actual `uv tool install
  --with`. Do a dry run.
- The `interactive_turn_end` hook fires only in the **interactive REPL**
  path (cli_runner). If Aaron also uses the TUI/ACP modes, verify whether the
  same phase fires there (not traced in this pass; `on_message` /
  `agent_run_result` phases exist as alternates).
- pocket-tts English voices/languages verified; quality/pronunciation for
  code-speak ("npm", "async") is subjective — try a couple of voices.
- Star/commit figures are as-reported by GitHub on 2026-07-11.

## Sources

**Tier 1 — read directly from installed package (code-puppy v0.0.634, accessed 2026-07-11):**
- `code_puppy/callbacks.py` — PhaseType literal (line 58), `on_interactive_turn_end` (line 1271), `register_callback` contract
- `code_puppy/cli_runner.py` — trigger site & continuation loop (lines ~1101–1230), `result.output` rendering
- `code_puppy/plugins/__init__.py` — `_load_user_plugins` convention (`register_callbacks.py` per plugin dir)
- `code_puppy/plugins/wiggum/register_callbacks.py` — `interactive_turn_end` consumer + `result.output` extraction (line 185)
- `code_puppy/plugins/example_custom_command/register_callbacks.py` — custom command return-value contract
- `code_puppy/plugins/dbos_durable_exec/register_callbacks.py` — optional-dependency ImportError pattern
- `code_puppy/config` — `get_value` / `set_config_value` (verified via import)

**Tier 1 — official docs/repos (retrieved via qa-kitten, accessed 2026-07-11):**
- kyutai-labs: pocket-tts README/repo, https://github.com/kyutai-labs/pocket-tts (7,357; last commit 2026-06-23)
- kyutai-labs: pocket-tts Python API docs, https://kyutai-labs.github.io/pocket-tts/API%20Reference/python-api/
- kyutai-labs: `pocket_tts/main.py` (serve endpoints `/tts`, `/health`), https://github.com/kyutai-labs/pocket-tts/blob/main/pocket_tts/main.py
- kyutai-labs: pyproject.toml (v2.1.0, deps, extras), https://raw.githubusercontent.com/kyutai-labs/pocket-tts/main/pyproject.toml
- kyutai-labs: LICENSE (MIT), https://raw.githubusercontent.com/kyutai-labs/pocket-tts/main/LICENSE
- Kyutai tech report/blog, https://kyutai.org/blog/2026-01-13-pocket-tts ; paper https://arxiv.org/abs/2509.06926
- mpfaffenberger: code_puppy repo & plugins tree, https://github.com/mpfaffenberger/code_puppy (645; commit activity within hours of 2026-07-11)
- spatialaudio: python-sounddevice 0.5.5 (2026-01-23), https://github.com/spatialaudio/python-sounddevice/ ; PyPI https://pypi.org/project/sounddevice/ ; example https://raw.githubusercontent.com/spatialaudio/python-sounddevice/master/examples/play_stream.py
- irmen: pyminiaudio 1.71 (2026-04-29), https://github.com/irmen/pyminiaudio

**Rejected sources (Tech-CRAAP failures):**
- simpleaudio — archived project, last release 2019-11 (currency, maintenance)
- pyaudio — last release 2023-11, no reliable Windows wheels (currency, accuracy for Windows)
