# puppy_talk

> **Proof of concept** -- tossed together in one evening. It works (and
> is fun), but expect rough edges: minimal error handling in places, no
> test suite, and APIs/settings may change without ceremony.

A [Code Puppy](https://github.com/mpfaffenberger/code_puppy) plugin that
reads the agent's responses aloud using
[pocket-tts](https://github.com/kyutai-labs/pocket-tts) (Kyutai's
100M-param, CPU-only text-to-speech model). Speech starts while the
response is still streaming, and you can clone any voice from a local
audio or video file.

## Features

- **Live narration** -- speaks complete sentences while the LLM is still
  typing the response (typically under a second to first audio)
- **Voice cloning** -- point it at any local wav/mp3/mp4/mkv and it
  extracts a voice prompt, hosts it locally, and speaks with that voice
- **Zero dependencies added to code-puppy** -- TTS runs in a
  `uvx pocket-tts serve` sidecar in its own environment; playback uses
  OS facilities (winmm waveOut on Windows, afplay/paplay elsewhere)
- **Per-instance activation** -- with multiple code-puppy instances
  running, only the ones where you typed `/talk on` speak
- **Interruptible** -- new prompts, cancelled turns, and `/talk stop`
  silence the voice in ~60ms

## Requirements

- [Code Puppy](https://github.com/mpfaffenberger/code_puppy)
  (tested with v0.0.634+; uses the `interactive_turn_end`,
  `stream_event`, and `custom_command` plugin hooks)
- [uv](https://docs.astral.sh/uv/) on PATH (`uvx` manages the TTS
  sidecar and the one-time audio converter)
- Windows, macOS, or Linux. Streaming playback works out of the box on
  Windows (winmm waveOut, stdlib). On macOS/Linux, install the optional
  [`sounddevice`](https://pypi.org/project/sounddevice/) package into
  code-puppy's environment for the same low-latency streaming (e.g.
  `uvx --with sounddevice code-puppy`); without it, speech falls back
  to buffered per-sentence playback via `afplay`/`paplay`/`aplay`/`ffplay`
- Disk/network for first run: the sidecar env downloads CPU torch
  (~120 MB) plus the TTS model

## Install

Pick your platform -- one line, copy, paste, run. Then **restart
code-puppy** and the `/talk` command is available. Plugins live at
`~/.code_puppy/plugins/`; the zip contains a single top-level
`puppy_talk/` folder, so extract, don't nest.

### macOS / Linux

```bash
curl -fsSL https://github.com/weegens-aaron/puppy_talk/releases/latest/download/puppy-talk.zip -o /tmp/puppy-talk.zip && unzip -o /tmp/puppy-talk.zip -d ~/.code_puppy/plugins/
```

### Windows (PowerShell)

```powershell
Invoke-WebRequest -Uri https://github.com/weegens-aaron/puppy_talk/releases/latest/download/puppy-talk.zip -OutFile $env:TEMP\puppy-talk.zip; Expand-Archive -Force $env:TEMP\puppy-talk.zip -DestinationPath ~\.code_puppy\plugins\
```

### Manual download (any platform)

1. Go to the [Releases page](https://github.com/weegens-aaron/puppy_talk/releases/latest)
2. Download **`puppy-talk.zip`** from the assets
3. Extract so `puppy_talk/` lands directly inside `~/.code_puppy/plugins/`
4. Restart code-puppy

### Verify your download (optional)

Every release publishes `puppy-talk.zip.sha256` next to the zip.

```bash
# macOS / Linux (after the install one-liner)
curl -fsSL https://github.com/weegens-aaron/puppy_talk/releases/latest/download/puppy-talk.zip.sha256 -o /tmp/puppy-talk.zip.sha256
( cd /tmp && shasum -a 256 -c puppy-talk.zip.sha256 )   # prints "puppy-talk.zip: OK"
```

```powershell
# Windows PowerShell (after the install one-liner)
Invoke-WebRequest -Uri https://github.com/weegens-aaron/puppy_talk/releases/latest/download/puppy-talk.zip.sha256 -OutFile $env:TEMP\puppy-talk.zip.sha256
$expected = (Get-Content $env:TEMP\puppy-talk.zip.sha256).Split(' ')[0]
$actual = (Get-FileHash $env:TEMP\puppy-talk.zip -Algorithm SHA256).Hash
if ($actual -eq $expected) { 'OK: checksum matches' } else { Write-Error 'CHECKSUM MISMATCH -- do not install' }
```

If verification fails, don't install -- re-download or report it.

### Upgrade / Uninstall

| Action | How |
|---|---|
| **Upgrade** | re-run the install one-liner -- it always pulls the latest release. Your `puppy_talk.json` and `voices/` survive: the zip doesn't contain them and extraction only overwrites files present in the archive |
| **Uninstall** | delete `~/.code_puppy/plugins/puppy_talk/` |

## Quickstart

```
/talk on            # enable speech in THIS code-puppy instance
/talk say hello     # test the pipeline
```

The first `/talk on` starts the sidecar, which downloads its
environment and model -- give it a few minutes once. After that,
startup takes seconds and responses are narrated automatically.

## Commands

| Command | What it does |
|---|---|
| `/talk on` / `/talk off` | enable/disable speech in this instance (never persisted) |
| `/talk status` | settings, live mode, voice, sidecar health |
| `/talk stop` | interrupt current speech |
| `/talk restart` | restart the sidecar (applies `quantize`, new auth, etc.) |
| `/talk live on\|off` | speak during streaming vs only after the turn ends |
| `/talk voice <name\|url\|path>` | set the voice (validated; typos get suggestions) |
| `/talk voices` | list the 26 built-in voices |
| `/talk set [key value]` | show or edit persisted settings |
| `/talk say <text>` | speak arbitrary text |

## Configuration

Preferences persist in `puppy_talk.json` in this directory (created on
first change; only overrides are stored). Edit with
`/talk set <key> <value>`, or by hand (picked up on restart).

| Key | Default | Meaning |
|---|---|---|
| `voice` | `alba` | built-in name, `hf://` / `http(s)://` url, or `local:<file>` |
| `port` | `8917` | TTS sidecar port |
| `voice_host_port` | `8918` | local voice file server port |
| `live` | `true` | speak sentences while the response streams |
| `live_min_start` | `300` | chars a streaming reply must reach before live speech starts |
| `max_chars` | `1500` | length cap for turn-end speech (sentence-boundary aware) |
| `quantize` | `false` | run the sidecar with int8 weights (~27% faster on x86); apply with `/talk restart` |

Activation (`/talk on`) is process-local state and is not stored.

## Voices

### Built-in catalog

26 precomputed voices work out of the box -- run `/talk voices` for the
list (21 English + `giovanni`/`lola`/`juergen`/`rafael`/`estelle` for
other languages, which need the sidecar started with a matching
`--language`). Audition them in the sidecar's web UI at
`http://127.0.0.1:8917` or at the
[kyutai/tts-voices](https://huggingface.co/kyutai/tts-voices) library.

### Custom voices (cloning) -- one-time setup

Cloning from arbitrary audio requires pocket-tts's gated weights:

1. Accept the terms at
   [huggingface.co/kyutai/pocket-tts](https://huggingface.co/kyutai/pocket-tts)
   (free Hugging Face account required)
2. `uvx hf auth login` -- paste a token from
   huggingface.co/settings/tokens. If you use a *fine-grained* token, it
   must have "read access to public gated repos" enabled; a classic
   **Read** token needs nothing extra.
3. `/talk restart`

Then clone from any local media file -- audio or video:

```
/talk voice C:\clips\my_favorite_narrator.mp4
```

The plugin extracts the **first 30 seconds** as a mono WAV (via
`uvx static-ffmpeg`, downloaded on first use), stores it in `./voices/`
with a content hash in the filename, and serves it to the sidecar over
`http://127.0.0.1:8918`. Re-importing an unchanged file is instant.

Tips:
- The model clones **pace and energy**, not just timbre. A calm clip
  gives you a calm narrator; a hyped intro gives you hype; a
  fast-talker sample is a legitimate "playback speed" knob.
- Use 10-30s of one person speaking with minimal background noise.
  Since only the first 30s are used, trim your source accordingly.
- The first request after a sidecar start processes the voice prompt
  (~5-10s); after that the state is cached per URL and speech is fast.

## How it works

```
code-puppy plugin hooks                 sidecars (own uv envs)
-----------------------                 ----------------------
stream_event ──> live_speech ──┐
interactive_turn_end ──────────┼──> server.py ── HTTP ──> uvx pocket-tts serve
user_prompt_submit / cancel ───┘         │                (port 8917, model+cache)
                                         v
                               playback.py (waveOut / afplay)
voice files: voice_host.py ── serves ./voices/ on 8918 ──> fetched+cached by sidecar
```

Three speech paths, fastest first:

1. **live** -- buffers streamed text deltas from the main agent (subagents
   are ignored), speaks complete sentences as they accumulate. This is
   speculative: text followed by a tool call was interim, so speech
   cancels; replies under `live_min_start` chars stay silent until the
   turn confirms them, which filters interim preambles.
2. **streaming** (turn end) -- WAV is decoded off the live HTTP response
   and PCM is pushed to the sound card as the sidecar generates it
   (waveOut on Windows, `sounddevice`/PortAudio elsewhere when installed).
3. **pipeline** (fallback) -- sentence-group N plays while N+1
   synthesizes.

All speech runs on daemon threads -- the REPL never blocks. Multiple
code-puppy instances share one sidecar; if the owning process exits, the
next instance to speak respawns it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Nothing plays | `/talk status` -- is speech on and the sidecar up? First run downloads for minutes; watch `pocket_tts_server.log` |
| `HTTP 400` on synthesis | invalid voice name/url -- `/talk voices` |
| `HTTP 500` with a custom voice | gated cloning weights missing -- do the one-time setup above, then `/talk restart` |
| `/talk restart` refuses | another process owns the sidecar; kill whatever listens on the port, then `/talk on` |
| Sidecar exits instantly (bind error 10048) | port taken -- `/talk set port <n>` and `/talk on` |
| Voice sounds wrong | the clone mirrors the prompt clip: background noise, music, or multiple speakers in the first 30s will bleed in |

## Development

- `register_callbacks.py` -- plugin entry point: hooks + `/talk` command
- `live_speech.py` -- speculative sentence-by-sentence speech during streaming
- `server.py` -- sidecar lifecycle, HTTP client, WAV header repair
- `playback.py` -- playback backends (waveOut + sounddevice streaming, file players) with stop support
- `voice_host.py` -- voice import (ffmpeg via uvx) + local HTTP voice server
- `sanitize.py` -- markdown -> speakable text, sentence chunking
- `settings.py` -- JSON-backed config, process-local activation
- `scripts/build-release.sh` -- builds the release zip from the
  allowlist in `scripts/ship-manifest.txt` (run in bash/Git Bash);
  version comes from `__init__.py`

Keep files under 600 lines; playback backends and speech policy are
deliberately separate modules.

## Credits & License

- [pocket-tts](https://github.com/kyutai-labs/pocket-tts) by Kyutai Labs (MIT)
- [Code Puppy](https://github.com/mpfaffenberger/code_puppy) by Michael Pfaffenberger (MIT)
- puppy_talk is MIT licensed -- see [LICENSE](LICENSE)

Voice cloning note: clone voices you have the right to use. Cloned
voices of real people are for personal/parody use at your own risk.
