# Voice Dictation — hold-to-talk speech-to-text for Windows

100% local speech-to-text. No cloud, no GPU — everything runs on your CPU and RAM.

You mouse-click into a text field, hold a key while you talk, release it, and the
transcription is typed into that window. Say a configured shortcut phrase like
"insert email" and your email (or credit card number, address…) is inserted instead.

## How it works

| Part | Implementation |
| --- | --- |
| Speech recognition | [faster-whisper](https://github.com/DeepInsider/faster-whisper) (OpenAI Whisper, CTranslate2, all CPU cores — plus NVIDIA Canary and NVIDIA Parakeet as alternative engines), with a CPU/GPU toggle in the tray menu |
| Tray icon | `pystray` — the app lives in your system tray, no terminal window |
| Screen-corner icon | while **listening** (recording) and while **transcribing**, a small icon appears in the top-right corner of your screens — red = recording, amber = transcribing — and disappears the rest of the time, so you can watch state even though Windows 11 hides tray icons |
| Hold-to-talk | any F-key / Alt / Shift / letter / digit or a combination of them (no Ctrl, no Win — see the Trigger key entry), configurable in the settings UI — or click "Detect my key" and physically press the shortcut you want (default `F9`) |
| Microphone | Windows DirectShow sources via `ffmpeg` (the real system mic list), with `sounddevice`/PortAudio as a fallback |
| Output | types into the focused window (Windows Script Host `SendKeys` via `cscript.exe`) or copies to the clipboard — configurable |
| Settings | web page served on `http://127.0.0.1:47111` — reachable **only from this machine** (no other LAN user can open it, so no password is needed), saved to `config.json` |

## Setup

Double-click **`install.bat`** — it creates the virtual environment, installs the
core and optional (Canary/Parakeet) dependencies, and generates the tray icon.

The recorder captures audio through **ffmpeg** (DirectShow) whenever PortAudio
sees no usable input — this is what makes the "Microphone" dropdown show every
Windows source. Keep a recent `ffmpeg` on your `PATH` (it is a system tool, not
a Python package); without it the app still works through PortAudio inputs only.

Or step by step:

```bat
:: run from this repository's folder
python -m venv .venv
call .venv\Scripts\activate.bat
pip install -r requirements.txt
:: optional, only if you want the Canary/Parakeet engines:
pip install -r requirements-optional.txt
```

## Run

Double-click **`VoiceDictation.bat`** — the installer creates it next to
`install.bat` and it launches the app windowless: the icon appears in your system
tray. Copy `VoiceDictation.bat` to your Desktop, a folder, or a USB stick — it
stores absolute paths, so a copy works from anywhere.

- **Right-click the tray icon** → menu: Open settings / **Model manager** / Pause
  listening / Resume listening / **Use GPU (CUDA)** / **Use CPU only** / Test
  microphone / Quit. The GPU/CPU pair is a toggle: exactly one is marked checked,
  picking which device transcribes (GPU falls back to CPU when no CUDA GPU
  exists); the choice persists in `config.json`. "Open settings" and
  "Model manager" just open the relevant page in your browser and return
  immediately — the tray menu keeps working while the page is open, and closing
  the browser tab is all it takes to be done.
- While the app is running, you can also open the settings page directly in your
  browser at `http://127.0.0.1:47111` (typing `127.0.0.1` **without** that port
  hits port 80, not the app — that shows "didn't send any data").
- To run just the settings page (debugging): `python app.py --settings` — it
  prints the URL; press Ctrl+C in the terminal to quit.
- Status colors: gray = idle, **red = listening to you**, amber = transcribing,
  back to gray when the text was inserted.
- Debugging: run `.venv\Scripts\python.exe app.py` in a terminal to see errors.

## Using it

1. Mouse-click into the textbox you want to dictate into (Word, an email draft,
   any app) so it has keyboard focus.
2. Press and **hold** your trigger key (default F9) and talk — listening starts
   the instant you press: capture runs continuously, so the moment the key
   registers, audio is already rolling and your first words are caught.
3. **Release** it. The text — with your voice shortcuts already replaced — is
   typed into that window.

Speak the shortcut phrases you configured, e.g. "insert email" or "insert card
number" — they are matched in the transcript (case-insensitive) and replaced with
whatever you set.

## Settings (web page)

The settings page is opened from the tray menu (or `python app.py --settings`),
or — while the app is running — by typing `http://127.0.0.1:47111` in your
browser. It is served on `127.0.0.1`, so only this machine can view it
— no username/password needed. Press **Save settings** to write `config.json`
(a running app picks the changes up immediately); when you are done, just close
the tab — the page stays available for the whole time the app runs.

- **Trigger key** — pick from the dropdown (F1–F12, Alt, Shift, letter/digit),
  or press **Detect my key** and then physically hold **any number of keys together**
  for a moment (a single key, a combination like `Alt + F9`, or as many keys as you
  like): the exact keys you are holding are captured and used as your trigger
  combination. Prefer F-keys: holding a letter or digit also types repeated characters
  into your document.
  You can also choose a **combination** (e.g. `Alt + F9` or `Shift + Alt`) — pick one
  from the dropdown, or **type your own combination of any number of keys** in the
  "Or type your own combination" box (keys joined with `+`): the listener starts
  recording only when *all* parts are physically held at once, and stops when any
  one is released. Named keys are allowed by name: `Space`, `Tab`, `Enter`, `Esc`,
  `Backspace`, `Minus`, `Comma`, `Period`, `Slash` (e.g. `Alt + Space`, `F9 + Space`).
  **Ctrl and Win are not offered at all** (the dropdown and the typed-combination
  box reject them, each with the reason): a held Ctrl turns live-typed text into
  Ctrl+letter shortcuts and WSH cannot lift it, and Windows **never reports a
  physically held Win key to any Win32 API** - `GetKeyState(0xDB)`'s down bit
  AND the `GetKeyboardState` physical bit both stay 0 while it is pressed
  (measured on this machine), so a Win part of a trigger could never be
  detected no matter how the app is written. F-keys, Alt, Shift and their
  combinations trigger reliably.
- **Microphone** — choose which audio source the recorder uses. The dropdown lists
  every Windows microphone/input the OS exposes (through DirectShow — the same list
  the OS shows, headset mics included), plus any PortAudio input when present.
  "Auto (system default)" picks the best real microphone. If one source records
  silence, switch to another in this dropdown and re-run "Test microphone" — the
  test now says exactly what each source produced ("no audio returned" vs
  "recorded silence"), so you can prove the problem is the source, not the app.
  Listing the Windows sources requires `ffmpeg` on `PATH` (the recorder captures
  through ffmpeg's DirectShow interface when PortAudio sees no usable input).
- **Compute device** — `CPU - all cores` (default) or `GPU - CUDA`: which device
  transcribes. This is the same switch as the tray menu's "Use GPU (CUDA)" /
  "Use CPU only" pair — page and tray write the same setting, and GPU falls back
  to CPU when the machine has no CUDA GPU.
- **Engine** — model options shown change automatically to match the engine you
  select:
  - `OpenAI Whisper (faster-whisper)` — fast on CPU; shows a model-size dropdown:
    `tiny`, `base`, `small`, `medium`, `large-v3`, `large-v3-turbo`
    (accuracy vs speed/RAM; `base` is a good start, `small` is noticeably
    better).
  - `NVIDIA Canary` — multilingual; shows the Canary presets that Hugging Face
    mirrors with a transformers-native config (1B v2, 25 languages). NVIDIA repos
    that publish only a NeMo checkpoint (`.nemo`) — Canary 180M Flash, Canary
    Qwen 2.5B, plain Canary 1B — cannot be loaded by transformers and are not
    offered.
  - `NVIDIA Parakeet` — very accurate English ASR (TDT 0.6B v3 also covers
    25 languages); shows Parakeet presets. CPU-friendly.
  - `Custom model` — shows a free-text field for any Hugging Face ASR model ID.
- **Language** — auto-detect by default; pick explicitly for better accuracy
  (Whisper engine only — the Canary/Parakeet models auto-detect and always
  ignore this field).
- **Output mode** — type into the focused window, or copy to clipboard.
- **Live typing (type while you speak)** — off by default. With output mode
  "type into the focused window" and this ON, recognized words are typed into
  the window **while you are still talking**: transcription runs in its own
  thread, always re-transcribing only the **last ~6 seconds** of audio at
  the model's own speed (a bounded re-listen window: per-pass cost stays
  small - the pass INTERVAL and the delay to the newest speech are set by
  this window, so a short one is what puts typing within about one word of
  your speech - and long audio drifting out of re-reading is what stops
  long-form models from rewriting their own earlier text forever, the old
  "first sentence then silence" symptom). The first words commit as soon as
  the first pass finishes, and later passes only ADD what their text
  contributes past what is already typed - each pass is aligned by word
  against the end of the typed text (at any offset, tolerating the
  re-tokenized positions long-form models drift into their own older text,
  and treating a pause longer than the window as a gap to append after),
  append-only by construction, so nothing typed live is ever retracted;
  the word you are still saying is always held back (none of these models
  has a native streaming API, so this re-listen-and-align is what makes it
  safe).
  Works with every engine; OpenAI Whisper (any size) stays roughly in sync
  with your speech, while **Canary / Parakeet / Custom models lag behind**
  and catch up the moment you release. Voice shortcuts ("say X → insert Y")
  are honored while typing live too. With clipboard output this setting has
  no effect. **Ctrl simply does not work with live typing, and is not offered
  at all** (the dropdown and the typed-combination box both reject it): with
  text being typed while your hand still holds the trigger, a physically
  held Ctrl turns every character into a Ctrl+letter shortcut, and Windows
  Script Host's SendKeys has no modifier up/down token to lift a held Ctrl
  (verified: every `{CTRL UP}`-style spelling raises an error) - the
  combination is not supported, so Ctrl was removed from the offer set
  rather than shipping a broken option (Win is removed the same way: its
  held state is not reported by Windows at all). Use F-keys or combinations
  without Ctrl/Win; Alt and Shift remain selectable, but any held modifier
  does modify live-typed text, so F-keys stay the safest choice.
- **Voice shortcuts** — "say this → insert that" rows (e.g. `insert email` →
  your email): press "Add voice shortcut", type both sides in the boxes,
  "Remove" deletes a row.
- **Test microphone** — records 4 s and shows what was recognized (useful for
  debugging mic/engine setup). It now distinguishes a dead source ("no audio
  returned") from a live-but-quiet one ("recorded SILENCE").
- **Live status** — the settings page also shows, in real time, where the hold-to-talk
  pipeline currently is (idle / recording / transcribing / "nothing recognized" /
  "too short"). If a cycle produces no text, look at this line to see where it stopped.

Transcription always uses **every CPU core** (ctranslate2's thread count is set to
`os.cpu_count()`), so a 16-core machine stops crawling at 10%. Low usage **while
recording** is expected: capture is I/O-bound, the cores get used during the
transcription step. The **Use GPU (CUDA)** tray-menu toggle switches the engine to
CUDA (`int8_float16`) when the ctranslate2 build has CUDA; without a GPU it falls
back to CPU and says so.

A separate **Models** page lives at `http://127.0.0.1:47111/models.html`: it lists every
model (Whisper sizes, Canary, Parakeet presets, **plus every other Canary model NVIDIA
publishes**, flagged "NeMo checkpoint only - this app cannot load it": those ship only
`.nemo` checkpoints and are not selectable in the settings dropdown) plus anything else
physically present in the HF cache, with installed/present status and size, and lets you
**Download** (pre-fetch a model, needs internet once) or **Delete** it. Models already
present are used offline.

Models are downloaded once from Hugging Face/CT2 on first use and cached
(`~/.cache`). After that, inference is fully offline.

## Troubleshooting

- **Tray icon missing after a few seconds** — Windows 11 hides third-party tray
  icons by default; use TopBar/Barrel/ExplorerCoreFix, or just run the app as a
  window (`python app.py`). You can still watch the app's state without the tray
  icon: the screen-corner icon appears in the top-right corner of your displays
  while recording/transcribing (its color shows which).
- **Nothing typed into your app** — output mode "type into focused window" needs
  the target window to have keyboard focus: click into it first, right next to
  where you want the text. Alternatively set output mode to clipboard and paste
  with Ctrl+Ctrl+C.
- **Text arrives but is wrong/garbled** — switch engine/size in the settings UI.
- **Changed the trigger key?** No restart needed — the app re-reads its settings
  every cycle, and the new key combination takes effect within a few seconds.
- Errors are appended to `errors.log` next to this README.
- `config.json` is plain text — it stores your email/card number; keep the folder
  private.
- Canary 1B v2 and `large-v3` need a lot of RAM; stay on `base`/`small` or
  the smaller models if your machine is modest.
- The Canary/Parakeet engines need `librosa` (listed in `requirements-optional.txt`);
  without it transformers raises "ParakeetFeatureExtractor requires the librosa library".
- Parakeet CTC/RNNT presets are English-only; use TDT 0.6B v3 or Whisper for
  other languages.
