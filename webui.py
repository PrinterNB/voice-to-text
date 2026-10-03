"""Settings + model-manager UI served as a web page on 127.0.0.1 only (stdlib http.server).

The 127.0.0.1 address means no other machine on your network can open it, so no
password is needed. Settings are still saved to config.json, and a save is pushed
into the running app immediately. The same server also hosts a "Models" page that
lists every model and can download or delete them from the local HF cache.

Single source of truth: KEY_VK (virtual key codes) and the model/engine tables are
defined here and imported by app.py, so the UI and the hotkey loop never drift.
"""

import ctypes
import json
import os
import random
import shutil
import threading
import time

from http.server import BaseHTTPRequestHandler, HTTPServer

import webbrowser

import config as config_mod
import recorder
import asr

# ---------------------------------------------------------------------------
# Key codes (shared with app.py)
# ---------------------------------------------------------------------------

KEY_VK = {f"f{i}": 0x70 + i - 1 for i in range(1, 13)}
KEY_VK.update({"ctrl": 0x11, "alt": 0x12, "shift": 0x10})
# Win keys are VK_LWIN/VK_RWIN (0xDB/0xDC) - Windows reuses the [ and \ codes.
KEY_VK.update({"win": 0xDB, "lwin": 0xDB, "rwin": 0xDC})
# Named keys so combinations can include them (e.g. "ctrl+space", "f9+space").
NAMED_KEYS = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "esc": 0x1B, "backspace": 0x08,
    "minus": 0xBD, "equal": 0xBB, "comma": 0xBC, "period": 0xBE, "slash": 0xBF,
    "semic": 0xBA, "quote": 0xDE, "grave": 0xC0, "backslash": 0xDC,
}
KEY_VK.update(NAMED_KEYS)
for _c in range(ord("a"), ord("z") + 1):
    KEY_VK[chr(_c)] = _c - 32
for _d in range(10):
    KEY_VK[str(_d)] = ord(str(_d))

# Ctrl and Win are deliberately NOT offered anywhere (dropdown or typed
# combinations):
# - Ctrl: with live typing a physically held Ctrl turns every typed
#   character into a Ctrl+letter shortcut, and WSH SendKeys has no
#   modifier up/down token to lift it (verified: every {CTRL UP}-style
#   spelling raises).
# - Win: Windows never reports a physically held Win key to the Win32
#   keyboard APIs at all - GetKeyState(0xDB)'s down bit AND the
#   GetKeyboardState physical bit both stay 0 while it is pressed
#   (measured on this machine), so a Win part of a trigger combination
#   could never be detected, no matter how the app is written.
# Both limitations are documented in README; keeping them out of the offer
# set is the honest way to keep every offered trigger reliable.
_MODS = ["alt", "shift"]
_NOT_OFFERED = ("ctrl", "win", "lwin", "rwin")
_NAMED = list(NAMED_KEYS)

SINGLE_KEYS = [f"f{i}" for i in range(1, 13)] + _MODS + _NAMED
SINGLE_KEYS += [chr(c) for c in range(ord("a"), ord("z") + 1)]
SINGLE_KEYS += [str(d) for d in range(10)]

# Combination triggers of any length (modifier pairs, modifier+F, modifier+modifier+F).
# These are convenience presets - you can also type any combination yourself in the
# settings UI; the app accepts a "+"-joined key string of any length.
COMBOS = []
for _a in range(len(_MODS)):
    for _b in range(_a + 1, len(_MODS)):
        COMBOS.append(_MODS[_a] + "+" + _MODS[_b])
for _m in _MODS:
    for _i in range(1, 13):
        COMBOS.append(_m + "+f" + str(_i))
for _a in range(len(_MODS)):
    for _b in range(_a + 1, len(_MODS)):
        for _i in range(1, 13):
            COMBOS.append(_MODS[_a] + "+" + _MODS[_b] + "+f" + str(_i))

TRIGGER_KEYS = SINGLE_KEYS + COMBOS


_LABELS = {
    "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win",
    "space": "Space", "tab": "Tab", "enter": "Enter", "esc": "Esc",
    "backspace": "Backspace", "minus": "-", "equal": "=", "comma": ",",
    "period": ".", "slash": "/", "semic": ";", "quote": "'", "grave": "`",
    "backslash": "\\",
}


def _key_label(name):
    if "+" in name:
        return " + ".join(_key_label(p) for p in name.split("+"))
    if name in _LABELS:
        return _LABELS[name]
    return name.upper()


def key_is_down(vk):
    """True if vk is currently DOWN (physically pressed or set programmatically).
    GetKeyState's down bit is documented as undefined outside Shift/Ctrl/Alt
    - the Win keys in fact never report it - while GetKeyboardState's
    physical bit (0x40) is the documented way to see any physically held
    key (games read it this way). Accept either: SendKeys never sets the
    physical bit, so typed text cannot fake a trigger."""
    if ctypes.windll.user32.GetKeyState(vk) & 0x8000:
        return True
    ks = (ctypes.c_ubyte * 256)()
    ctypes.windll.user32.GetKeyboardState(ks)
    return bool(ks[vk] & 0x40)


def _key_held(name):
    for part in name.split("+"):
        part = part.strip()
        if not key_is_down(KEY_VK.get(part, 0)):
            return False
    return True


def _detect_order():
    """Canonical list of key tokens to watch, one per unique virtual key code,
    ordered modifiers -> F-keys -> named keys -> letters -> digits, so a detected
    combination reads like 'Ctrl + Alt + F9'. The named keys come from NAMED_KEYS
    so every combination the settings page offers can also be detected."""
    order = _MODS + [f"f{i}" for i in range(1, 13)]
    order += _NAMED
    order += [chr(c) for c in range(ord("a"), ord("z") + 1)]
    order += [str(d) for d in range(10)]
    seen = set()
    out = []
    for tok in order:
        vk = KEY_VK.get(tok)
        if vk is None or vk in seen:
            continue
        seen.add(vk)
        out.append((tok, vk))
    return out


def _held_keys():
    """Names of every watched key that is physically held right now."""
    held = []
    for tok, vk in _detect_order():
        if key_is_down(vk):
            held.append(tok)
    return held


def detect_key(timeout):
    """Detect any combination the user is physically holding - as many keys as
    they press. Returns the exact "+"-joined combination, once it has been read
    the same way twice in a row (so partial presses are not misread)."""
    start = time.time()
    while time.time() - start < timeout:
        first = _held_keys()
        if first:
            time.sleep(0.1)
            second = _held_keys()
            # keep only keys still held (drop any released between the two reads)
            stable = [k for k in first if k in second]
            if stable:
                return "+".join(stable)
        time.sleep(0.02)
    return None


# ---------------------------------------------------------------------------
# Model / engine tables (shared with app.py for validation + model manager)
# ---------------------------------------------------------------------------

ENGINE_ORDER = ["whisper", "canary", "parakeet", "vosk", "custom"]
ENGINE_LABELS = {
    "whisper": "OpenAI Whisper (faster-whisper) - fast on CPU",
    "canary": "NVIDIA Canary - multilingual",
    "parakeet": "NVIDIA Parakeet - very accurate English ASR",
    "vosk": "Vosk - TRUE streaming: types word-by-word as you speak (CPU, less accurate, no punctuation)",
    "custom": "Custom model - any Hugging Face ASR model ID",
}

WHISPER_SIZES = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]
WHISPER_SIZE_LABELS = {
    "tiny": "tiny - fastest, least accurate",
    "base": "base - good default",
    "small": "small - better, slower",
    "medium": "medium - slow",
    "large-v3": "large-v3 - best Whisper accuracy, heavy",
    "large-v3-turbo": "large-v3-turbo - fast but large",
}

CANARY_MODELS = [
    # Only models with an HF-native config (a `model_type`) load through
    # transformers. canary-180m-flash and canary-qwen-2.5b ship only .nemo
    # checkpoints and can never load here, so they are not offered.
    ("nvidia/canary-1b-v2", "Canary 1B v2 - 25 languages"),
]
PARAKEET_MODELS = [
    ("nvidia/parakeet-tdt-0.6b-v3", "Parakeet TDT 0.6B v3 - 25 languages"),
    ("nvidia/parakeet-ctc-1.1b", "Parakeet CTC 1.1B - English"),
    ("nvidia/parakeet-rnnt-1.1b", "Parakeet RNNT 1.1B - English"),
]

# Vosk is a Kaldi-based ONLINE (streaming) recognizer - with it, live typing
# is word-level real-time (words are emitted as you speak), unlike the
# re-listen design the offline engines use. Kaldi models come from
# alphacephei.com (NOT Hugging Face); the app downloads the model zip into
# ~/.cache/vosk on first use.
VOSK_MODELS = [
    ("vosk-model-small-en-us-0.15", "Small English (40 MB) - quick, basic accuracy"),
    ("vosk-model-en-us-0.22", "Full English (1.8 GB) - noticeably better accuracy"),
]

# Models shown on the Models page for completeness but NOT offered in the
# settings dropdown: NVIDIA publishes these only as NeMo (.nemo) checkpoints,
# which transformers cannot load.
UNLOADABLE_MODELS = [
    ("canary", "Canary 180M Flash", "nvidia/canary-180m-flash"),
    ("canary", "Canary 1B", "nvidia/canary-1b"),
    ("canary", "Canary Qwen 2.5B", "nvidia/canary-qwen-2.5b"),
]

# One-to-two-line explanation for each model, shown on the Models page.
MODEL_BIO = {
    "Systran/faster-whisper-tiny": "OpenAI Whisper family: speech-to-text transducer, run here with CTranslate2 (fast, all CPU cores). Tiny: smallest and fastest, least accurate.",
    "Systran/faster-whisper-base": "OpenAI Whisper: good default balance of speed and accuracy.",
    "Systran/faster-whisper-small": "OpenAI Whisper: noticeably better accuracy than base, still quick on this machine.",
    "Systran/faster-whisper-medium": "OpenAI Whisper: higher accuracy, slower; a step before large.",
    "Systran/faster-whisper-large-v3": "OpenAI Whisper large-v3: best Whisper accuracy, ~3 GB, multilingual.",
    "Systran/faster-whisper-large-v3-turbo": "OpenAI Whisper large-v3-turbo: large-tier accuracy with a smaller decoder - much faster than large-v3.",
    "nvidia/canary-1b-v2": "NVIDIA Canary 1B v2: fast multilingual speech-to-text covering 25 languages; tuned for natural conversational speech.",
    "nvidia/parakeet-tdt-0.6b-v3": "NVIDIA Parakeet TDT 0.6B v3: highly accurate English ASR that also covers 25 languages; TDT (token-and-duration transducer) decodes quickly.",
    "nvidia/parakeet-ctc-1.1b": "NVIDIA Parakeet CTC 1.1B: English-only model using CTC decoding - simple and fast, but less punctuation-friendly than TDT.",
    "nvidia/parakeet-rnnt-1.1b": "NVIDIA Parakeet RNNT 1.1B: English-only RNN transducer; more accurate than CTC, slower.",
    "nvidia/canary-180m-flash": "NVIDIA Canary 180M Flash: very fast English/Spanish fast-audio model.",
    "nvidia/canary-1b": "NVIDIA Canary 1B (first release): multilingual speech-to-text; v2 is the current version.",
    "nvidia/canary-qwen-2.5b": "NVIDIA Canary Qwen 2.5B: Canary audio encoder paired with a Qwen2.5-LLM decoder - best transcript quality of the Canary line, heaviest to run.",
}

LANGUAGES = [
    ("", "(auto-detect)"),
    ("en", "English"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("de", "German"),
    ("it", "Italian"),
    ("pt", "Portuguese"),
    ("nl", "Dutch"),
    ("pl", "Polish"),
    ("ru", "Russian"),
    ("tr", "Turkish"),
    ("ar", "Arabic"),
    ("hi", "Hindi"),
    ("zh", "Chinese"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
]

OUTPUT_MODES = [
    ("autotype", "Type into focused window"),
    ("clipboard", "Copy to clipboard"),
]


def _repo_for(engine, value):
    """Map a selection to the Hugging Face repo id used to download it."""
    if engine == "whisper":
        return "Systran/faster-whisper-" + str(value)
    return str(value)


def _hf_cache_dir():
    for env in ("HF_HUB_CACHE",):
        p = os.environ.get(env)
        if p:
            return p
    home = os.environ.get("HF_HOME")
    if home:
        return os.path.join(home, "hub")
    return os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")


def _log_note(msg):
    path = os.path.join(os.path.dirname(config_mod.CONFIG_PATH), "errors.log")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def _norm_cfg(raw):
    cfg = dict(config_mod.DEFAULTS)
    raw = raw if isinstance(raw, dict) else {}
    cfg.update(raw)
    # The settings page edits a human-facing "compute" switch; the config key is a bool.
    if "compute" in raw:
        cfg["gpu"] = str(raw.get("compute")).strip().lower() == "gpu"
    else:
        cfg["gpu"] = bool(cfg.get("gpu", False))
    cfg.pop("compute", None)
    # Only a code we actually offer survives. The page's "(auto-detect)" option
    # has an empty <option value>, so clicking it hands back its label text - that
    # must never reach asr as a language code. "" means no preference -> None.
    if not cfg.get("language") or cfg.get("language") not in {c for c, _l in LANGUAGES}:
        cfg["language"] = None
    if not _valid_trigger(cfg.get("trigger_key")):
        cfg["trigger_key"] = "f9"
    mic = cfg.get("input_device", "")
    if isinstance(mic, str):
        s = mic.strip()
        ok = (s == "" or s == "-1" or s.startswith("ds:") or s.startswith("sd:"))
        cfg["input_device"] = "" if (not ok or s == "-1") else s
    else:
        try:
            di = int(mic)
        except (TypeError, ValueError):
            di = -1
        cfg["input_device"] = ("sd:%d" % di) if di >= 0 else ""
    if cfg.get("engine") not in ENGINE_ORDER:
        cfg["engine"] = "whisper"
    hf = str(cfg.get("hf_model") or "")
    engine_presets = {
        "canary": [r for r, _l in CANARY_MODELS],
        "parakeet": [r for r, _l in PARAKEET_MODELS],
    }
    if cfg.get("engine") in engine_presets and hf not in engine_presets[cfg["engine"]]:
        hf = ""  # preset that went unsupported -> fall back to the engine default
    if not hf:
        engine_presets.setdefault(cfg["engine"], [config_mod.DEFAULTS["hf_model"]])
        hf = engine_presets[cfg["engine"]][0]
    cfg["hf_model"] = hf
    # the Vosk engine's model is a Kaldi name from alphacephei.com, kept
    # separate from hf_model (which only ever names HF repos)
    if cfg.get("vosk_model") not in [r for r, _l in VOSK_MODELS]:
        cfg["vosk_model"] = config_mod.DEFAULTS["vosk_model"]
    if cfg.get("output_mode") not in ("autotype", "clipboard"):
        cfg["output_mode"] = "autotype"
    # Live typing is stored as a plain bool; the page's switch maps onto it.
    cfg["live_mode"] = bool(cfg.get("live_mode", False))
    if cfg.get("whisper_model") not in WHISPER_SIZES:
        cfg["whisper_model"] = "base"
    cmds = []
    for cmd in cfg.get("commands") or []:
        if not isinstance(cmd, dict):
            continue
        say = (cmd.get("say") or "").strip()
        if say:
            cmds.append({"say": say, "insert": cmd.get("insert") or ""})
    cfg["commands"] = cmds
    return cfg


def _valid_trigger(name):
    if not isinstance(name, str) or not name:
        return False
    parts = [p.strip() for p in name.split("+") if p.strip()]
    # Ctrl/Win are not offered (see _MODS comment/README): legacy configs
    # that saved one of them reset to the default instead
    return bool(parts) and all(p in KEY_VK and p not in _NOT_OFFERED
                               for p in parts)


def _mic_options():
    """Selectable audio sources: auto + every Windows source DirectShow sees,
    plus any PortAudio input (tokens: '', 'ds:<name>', 'sd:<idx>')."""
    out = [["", "Auto (system default)"]]
    try:
        import recorder

        for name in recorder._list_dshow():
            out.append(["ds:" + name, name])
    except Exception:
        pass
    try:
        import sounddevice

        for i, d in enumerate(sounddevice.query_devices()):
            try:
                if str(d.get("type", "")).upper().startswith("INPUT") and d.get("channels", 0) > 0:
                    name = str(d.get("name", "")) or "input"
                    out.append(["sd:%d" % i, "[PortAudio %d] %s" % (i, name)])
            except Exception:
                continue
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Shared live state (written by app.py's hotkey loop, read by the UI)
# ---------------------------------------------------------------------------

LIVE_STATUS = {"stage": "idle", "detail": "", "ts": ""}

PREFERRED_PORT = 47111

SERVER = None
SETTINGS_URL = None
LIVE_CFG = None


# ---------------------------------------------------------------------------
# HTML pages (modern, self-contained, dark UI)
# ---------------------------------------------------------------------------

_THEME = r"""
:root{color-scheme:dark;}
*{box-sizing:border-box;}
body{font-family:"Segoe UI",system-ui,-apple-system,sans-serif;background:#0e1116;
     color:#e6e9ee;max-width:860px;margin:0 auto;padding:22px 24px 40px;line-height:1.5;}
h1{font-size:1.35rem;font-weight:700;letter-spacing:-.01em;}
h2{font-size:1.05rem;font-weight:650;margin-top:18px;}
h3{font-size:.98rem;font-weight:650;}
.note{font-size:.82rem;color:#9aa4b0;}
code{font-family:"Consolas",monospace;background:#1b2027;padding:1px 5px;border-radius:4px;}
a{color:#6cb1ff;}
#msg{position:sticky;top:16px;background:#16202e;color:#f0f5ff;padding:11px 14px;
     border-radius:10px;font-weight:600;border:1px solid #2b3a52;box-shadow:0 3px 10px #0008;}
#status{position:sticky;top:52px;background:#16202e;color:#f0f5ff;padding:8px 14px;
     border-radius:10px;border:1px solid #2b3a52;font-size:.85rem;}
.chip{display:inline-block;padding:2px 8px;border-radius:6px;font-size:.75rem;font-weight:700;}
.card{background:#141a22;border:1px solid #232c37;border-radius:12px;padding:14px 16px;
      margin:14px 0;}
.row{display:flex;align-items:center;gap:10px;margin:10px 0;}
.lbl{font-weight:600;min-width:190px;}
select,textarea{font:inherit;background:#1b2027;color:#e6e9ee;border:1px solid #34404f;
     border-radius:8px;padding:6px 8px;min-width:130px;}
option{background:#1b2027;color:#e6e9ee;}
.ce{background:#1b2027;border:1px solid #34404f;border-radius:8px;padding:4px 9px;
     min-width:120px;}
textarea.ce{min-width:240px;white-space:pre-wrap;}
.cmdrow{display:flex;align-items:center;gap:10px;margin:10px 0;}
.btns{margin-top:20px;text-align:center;}
.btns button{margin:0 6px;vertical-align:middle;}
button{font:inherit;background:#253041;color:#eef3f8;border:1px solid #3a4a66;
       border-radius:8px;padding:7px 13px;cursor:pointer;}
button:hover{background:#2e3c52;}
table{border-collapse:collapse;font-size:.85rem;}
th,td{padding:6px 9px;border:1px solid #2a3441;text-align:left;vertical-align:middle;}
thead{background:#1a222c;}
tr[data-installed]{background:#10202c;}
.ok{color:#5dd78e;font-weight:700;}
.no{color:#e0667a;font-weight:600;}
"""

SETTINGS_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voice Dictation settings</title><style>__THEME__</style></head>
<body>
<script>window.__INIT__ = __INIT_JSON__;</script>
<h1>Voice Dictation settings</h1>
<div class="note">Local-only page (127.0.0.1) - nobody else can open it, so no password.
Settings save to <code>config.json</code>; the running app uses them right away.</div>
<div id="msg">Ready. Edit anything below, then press "Save settings". Close this tab when
you are done - the page stays available while the app runs. The tray icon turns
<b>red</b> while recording and <b>amber</b> while processing.</div>
<div id="status">Live status: <span>waiting for the trigger key...</span></div>
<form id="f"></form>
<div class="btns">
<button id="btn-detect">Detect my key</button>
<button id="btn-save">Save settings</button>
<button id="btn-test">Test microphone (4 s)</button>
<button id="btn-models">Model manager</button>
</div>
<div class="note">"Detect my key": press it, then physically hold the keys you want for a moment
(a single key like F9, or any combination like Ctrl + F9, or as many keys as you like) - they are
captured automatically. Prefer F-keys / Ctrl / Alt / Shift / Win: holding a letter or digit also
types repeated characters into your document.</div>
<script>
var D = window.__INIT__.data;
var st = window.__INIT__.cfg;
var live = window.__INIT__.live;
st.language = typeof st.language === 'string' ? st.language : '';
st.cmds = (st.commands || []).map(function (o) { return { say: o.say || '', insert: o.insert || '' }; });

var f = document.getElementById('f');
var msgEl = document.getElementById('msg');
var stEl = document.getElementById('status');
var stageLabels = {idle:'idle', recording:'Recording (red icon)',
  processing:'Transcribing (amber icon)', paused:'paused'};

function setMsg(t) { msgEl.textContent = t; }
function paint() {
  var s = stEl.querySelector('span');
  if (!s) { s = document.createElement('span'); stEl.appendChild(s); }
  var label = stageLabels[live.stage] || live.stage;
  s.textContent = label + (live.detail ? ' - ' + live.detail : '');
}
function pollStatus() {
  fetch('/status').then(function(r){return r.json();})
    .then(function(j){ try { for (var k in j) live[k] = j[k]; } catch(e){} paint(); })
    .catch(function(){});
}

setMsg('Ready. Edit anything below, then press "Save settings".');
// Live status auto-refresh while this tab is open.
window.__poll = function () { pollStatus(); setTimeout(window.__poll, 2000); };
window.__poll();
paint();

function post(path, obj, fn) {
  fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(obj)}).then(function (r) { return r.json(); }).then(fn)
    .catch(function (e) { setMsg('Could not reach the settings server (' + e +
      '). The app must be running - start it again, then press Open settings.'); });
}

function keyLabel(k) { return k.replace(/\+/g,' + '); }

var selKey = null, customEl = null, modelBlock = null;
var cmdRows = [];

function selectField(parent, name, field, opts, onChange) {
  var row = document.createElement('div'); row.className = 'row';
  var lab = document.createElement('span'); lab.className = 'lbl'; lab.textContent = name;
  var sel = document.createElement('select');
  sel.onchange = function () { st[field] = sel.value; if (onChange) onChange(); };
  for (var i = 0; i < opts.length; i++) {
    var o = document.createElement('option');
    o.setAttribute('value', opts[i][0]); o.textContent = opts[i][1];
    var cur = (st[field] === null || st[field] === undefined) ? '' : String(st[field]);
    if (opts[i][0] === cur) o.selected = true;
    sel.appendChild(o);
  }
  row.appendChild(lab); row.appendChild(sel); parent.appendChild(row);
  return sel;
}

function fillModel() {
  while (modelBlock.firstChild) modelBlock.removeChild(modelBlock.firstChild);
  customEl = null;
  if (st.engine === 'whisper') selectField(modelBlock, 'Whisper model size:', 'whisper_model', D.whisper_sizes, null);
  else if (st.engine === 'canary') selectField(modelBlock, 'Canary preset:', 'hf_model', D.canary, null);
  else if (st.engine === 'parakeet') selectField(modelBlock, 'Parakeet preset:', 'hf_model', D.parakeet, null);
  else if (st.engine === 'vosk') selectField(modelBlock, 'Vosk model:', 'vosk_model', D.vosk, null);
  else {
    var row = document.createElement('div'); row.className = 'row';
    var lab = document.createElement('span'); lab.className = 'lbl'; lab.textContent = 'Custom model ID:';
    var ta = document.createElement('textarea'); ta.rows = 1; ta.className = 'ce'; ta.value = st.hf_model || '';
    ta.onblur = function () { st.hf_model = ta.value; };
    customEl = ta; row.appendChild(lab); row.appendChild(ta); modelBlock.appendChild(row);
  }
}

function cmdRowOf(o) {
  var row = document.createElement('div'); row.className = 'cmdrow';
  var say = document.createElement('span'); say.className = 'ce'; say.contentEditable = 'true'; say.spellcheck = false; say.textContent = o.say;
  say.onblur = function () { o.say = say.textContent; };
  var ins = document.createElement('span'); ins.className = 'ce'; ins.contentEditable = 'true'; ins.spellcheck = false; ins.textContent = o.insert;
  ins.onblur = function () { o.insert = ins.textContent; };
  var arrow = document.createElement('span'); arrow.textContent = '\u2192';
  var rm = document.createElement('button'); rm.textContent = 'Remove';
  rm.onclick = function () { var idx = st.cmds.indexOf(o); if (idx >= 0) st.cmds.splice(idx, 1);
    for (var k = 0; k < cmdRows.length; k++) if (cmdRows[k] === row) { cmdRows.splice(k, 1); break; }
    row.parentNode.removeChild(row); };
  row.appendChild(say); row.appendChild(arrow); row.appendChild(ins); row.appendChild(rm);
  f.appendChild(row); cmdRows.push(row);
}

function gather() {
  var c = {};
  c.trigger_key = st.trigger_key; c.output_mode = st.output_mode; c.engine = st.engine;
  c.whisper_model = st.whisper_model; c.hf_model = st.hf_model;
  c.vosk_model = st.vosk_model;
  if (st.engine === 'custom' && customEl && customEl.value.trim()) c.hf_model = customEl.value.trim();
  c.language = st.language || null;
  c.input_device = String(st.input_device === null || st.input_device === undefined ? '' : st.input_device);
  c.compute = st.compute || 'cpu';
  // the page keeps 'on'/'off' in its dropdown; config.json stores a bool
  c.live_mode = st.live_mode === 'on' || st.live_mode === true;
  c.commands = [];
  for (var i = 0; i < st.cmds.length; i++) { var o = st.cmds[i]; if (o.say.trim()) c.commands.push({ say: o.say.trim(), insert: o.insert }); }
  return c;
}

selKey = selectField(f, 'Hold-to-talk key:', 'trigger_key', D.keys, null);
var _validKey = {};
for (var _kn = 0; _kn < D.keynames.length; _kn++) _validKey[D.keynames[_kn]] = 1;
function normCombo(v) {
  var t = (v || '').toLowerCase(), parts = [];
  var toks = t.split('+');
  for (var ti = 0; ti < toks.length; ti++) {
    var p = toks[ti].trim();
    if (!p) continue;
    if (p === 'ctrl' || p === 'control' || p === 'win' || p === 'lwin' || p === 'rwin' || p === 'windows') {
      setMsg('Ctrl and Win are not offered: a held Ctrl cannot be lifted while typing, and Windows never reports a held Win key at all - use F-keys, Alt or Shift combinations.');
      return null;
    }
    if (!_validKey[p]) {
      setMsg('That combination has an unknown key - use names like F9, Alt, Shift, Win, Space, Tab, Enter, Esc, A-Z, 0-9 joined with "+".');
      return null;
    }
    parts.push(p);
  }
  return parts.length ? parts.join('+') : null;
}
var customKeyEd = null;
(function () {
  var row = document.createElement('div'); row.className = 'row';
  var lab = document.createElement('span'); lab.className = 'lbl';
  lab.textContent = 'Or type your own combination:';
  var ed = document.createElement('span'); ed.className = 'ce'; ed.contentEditable = 'true'; ed.spellcheck = false;
  var curCombo = String(st.trigger_key || '');
  ed.textContent = (curCombo.indexOf('+') >= 0 && normCombo(curCombo)) ? curCombo : '';
  ed.onblur = function () {
    var v = (ed.textContent || '').trim();
    if (!v) return;
    // normCombo names the exact problem (unknown key vs Ctrl-not-offered)
    var ok = normCombo(v);
    if (ok) st.trigger_key = ok;
  };
  customKeyEd = ed;
  row.appendChild(lab); row.appendChild(ed); f.appendChild(row);
})();
selectField(f, 'Microphone:', 'input_device', D.mics, null);
st.compute = st.gpu ? 'gpu' : 'cpu';
selectField(f, 'Compute device:', 'compute', D.compute, null);
selectField(f, 'Output mode:', 'output_mode', D.outputs, null);
st.live_mode = st.live_mode ? 'on' : 'off';
selectField(f, 'Live typing (type while you speak):', 'live_mode', D.live_mode, null);
var liveHelp = document.createElement('div'); liveHelp.className = 'note';
liveHelp.textContent = ('Works with every engine, but real-time pacing is only as good as the model: OpenAI Whisper '
  + '(any size) stays close to your speech; NVIDIA Canary / Parakeet / Custom models trail more. It always '
  + 're-listens only the last ~6 seconds of audio, so long dictations keep typing instead of stalling after the '
  + 'first sentence, and anything not typed yet completes the moment you release. Needs output mode "type into '
  + 'focused window" - with clipboard output this setting has no effect. '
  + 'Transcription runs on its own thread, so model time never delays your speech: words commit within '
  + 'fractions of a second of what you say. Voice shortcuts are respected while typing live too. '
  + 'Do not dictate with Ctrl or Win in your hand: a held Ctrl turns every typed character into a '
  + 'Ctrl+letter shortcut and WSH cannot lift it, and Windows never reports a held Win key to any '
  + 'API - so neither is offered as a trigger here, typed combinations included. F-keys, Alt and '
  + 'Shift combinations trigger and type reliably.');
f.appendChild(liveHelp);
selectField(f, 'Transcription engine:', 'engine', D.engines, fillModel);
modelBlock = document.createElement('div'); f.appendChild(modelBlock); fillModel();
selectField(f, 'Language:', 'language', D.languages, null);
var head = document.createElement('h3'); head.textContent = 'Voice shortcuts (say this \u2192 insert that)';
f.appendChild(head);
var addBtn = document.createElement('button'); addBtn.textContent = 'Add voice shortcut';
addBtn.onclick = function () { var o = { say: '', insert: '' }; st.cmds.push(o); cmdRowOf(o); };
f.appendChild(addBtn);
for (var i0 = 0; i0 < st.cmds.length; i0++) cmdRowOf(st.cmds[i0]);

document.getElementById('btn-detect').onclick = function () {
  setMsg('Now physically hold the keys you want (any number, up to 12 seconds)...');
  post('/detect-key', { timeout: 12 }, function (j) {
    if (j.key) {
      st.trigger_key = j.key;
      if (j.key.indexOf('+') >= 0) { try { for (var oi = 0; oi < selKey.options.length; oi++) {
          if (selKey.options[oi].value === j.key) { selKey.value = j.key; break; } } } catch (e1) {}
        if (customKeyEd) customKeyEd.textContent = j.key; }
      else { try { selKey.value = j.key; } catch (e2) {} }
      setMsg('Detected "' + keyLabel(j.key) + '" (any number of keys). Press "Save settings" to use it.');
    } else {
      setMsg('No key was detected - hold the keys together and try again.');
    }
  });
};
document.getElementById('btn-save').onclick = function () {
  setMsg('Saving...');
  post('/save', { cfg: gather() }, function (j) {
    setMsg(j.ok ? 'Settings saved - the running app uses them right away.' : 'Save failed: ' + (j.error || 'unknown'));
  });
};
document.getElementById('btn-test').onclick = function () {
  setMsg('Recording 4 seconds - speak now, then wait (first run downloads the model)...');
  post('/test-mic', { cfg: gather() }, function (j) {
    if (j.error) setMsg('Test failed: ' + j.error);
    else if (j.note && j.note.indexOf('no audio') === 0) setMsg('That source returned NO AUDIO at all - it is not a usable microphone; pick another one in this dropdown.');
    else if (j.note === 'silence') setMsg('The microphone recorded SILENCE - pick a different "Microphone" in this UI (default/auto may be the wrong input).');
    else if (j.heard) setMsg('I heard: "' + j.heard + '"');
    else setMsg('Mic captured ' + (j.seconds || 0) + 's at ' + Math.round((j.peak || 0) * 1000) / 10 + '% loudness but nothing was recognized - speak louder, or change engine/language.');
  });
};
document.getElementById('btn-models').onclick = function () {
  var w = window.open('/models.html', '_models');
  if (!w) w = window.open('/models.html', '_blank');
  setMsg('Opened the Model manager in a new tab - pre-download models or delete unused ones there.');
};
</script></body></html>
"""

MODELS_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Models - Voice Dictation</title><style>__THEME__
.modelbar{margin:0 0 8px;}
#busy{margin:10px 0;padding:8px 12px;background:#16202e;border:1px solid #2b3a52;border-radius:10px;font-weight:600;}
table{width:100%;}
thead{background:#16202e;}
button.small{padding:3px 9px;font-size:.76rem;}
.modelbar button,button.small{background:#253041;border:1px solid #3a4a66;border-radius:8px;}
.modelbar button:hover,button.small:hover{background:#2e3c52;}
.bio{font-size:.74rem;color:#98add0;}
</style></head>
<body>
<h1>Local models</h1>
<div class="note">Every model you can select, with whether it is already on your disk.
A "present" model is used fully offline - no download needed. Download pre-fetches a model
(needs internet, one time); Delete removes it from the cache. Cache directory:
<code id="cache">__CACHE__</code></div>
<div class="modelbar"><button id="btn-refresh">Refresh</button></div>
<div id="busy"></div>
<div id="tbl"></div>
<div class="note">Keep this tab open while a download runs - large models (GB-scale) take a
few minutes. Rows at the bottom are any other models found in the cache.</div>
<script>
var rows = [];
var cacheEl = document.getElementById('cache');
var busyEl = document.getElementById('busy');
var busy = function (t) { busyEl.textContent = t; };

function load() {
  busy('Loading models...');
  fetch('/models').then(function(r){return r.json();}).then(function(j){
    rows = j.rows || []; if (j.cache) cacheEl.textContent = j.cache; busy(''); paint();
  }).catch(function(e){ busy('Could not reach models: ' + e); });
}

function paint() {
  var t = document.getElementById('tbl'); t.innerHTML='';
  var table = t.appendChild(document.createElement('table'));
  var thead = table.appendChild(document.createElement('thead'));
  var hdr = thead.appendChild(document.createElement('tr'));
  var cols = ['Model','Engine','Status','Size','Actions'];
  for (var c = 0; c < cols.length; c++) {
    var th = hdr.appendChild(document.createElement('th')); th.textContent = cols[c];
  }
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    var tr = table.appendChild(document.createElement('tr'));
    var c0 = tr.appendChild(document.createElement('td'));
    c0.textContent = r.label + (r.note ? ' - ' + r.note : '');
    if (r.bio) {
      var bd = c0.appendChild(document.createElement('div'));
      bd.className = 'bio';
      bd.textContent = r.bio;
    }
    var c1 = tr.appendChild(document.createElement('td')); c1.textContent = r.engine;
    var c2 = tr.appendChild(document.createElement('td'));
    c2.textContent = r.installed ? 'present' : 'not downloaded';
    var c3 = tr.appendChild(document.createElement('td')); c3.textContent = r.size || '-';
    var c4 = tr.appendChild(document.createElement('td'));
    rowActions(c4, r);
  }
}

function rowActions(cell, r) {
  // A function parameter, not the paint() loop's `var r`: closures made in a
  // loop share one binding, so button handlers built inline would all act on
  // the last row.
  if (r.installed) {
    var delBtn = cell.appendChild(document.createElement('button')); delBtn.className = 'small';
    delBtn.textContent = 'Delete';
    delBtn.onclick = function () { act(r, 'delete'); };
    if (!r.note) {
      var reBtn = cell.appendChild(document.createElement('button')); reBtn.className = 'small';
      reBtn.textContent = 'Re-download';
      reBtn.onclick = function () { act(r, 'download'); };
    }
  } else if (!r.note) {
    var dlBtn = cell.appendChild(document.createElement('button')); dlBtn.className = 'small';
    dlBtn.textContent = 'Download';
    dlBtn.onclick = function () { act(r, 'download'); };
  }
}

function act(r, kind) {
  if (!r.repo) { busy('Custom model: set it as the active model on the settings page to fetch it.'); return; }
  var path = kind === 'download' ? '/models/download' : '/models/delete';
  busy(kind === 'download'
    ? 'Downloading "' + r.label + '" - keep this tab open, this can take several minutes...'
    : 'Deleting "' + r.label + '"...');
  fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({repo: r.repo})})
    .then(function(x){return x.json();})
    .then(function(j){
      if (j.error) busy((kind === 'download' ? 'Download failed: ' : 'Delete failed: ') + j.error);
      else if (kind === 'delete' && j.removed === 0) busy('Nothing on disk for "' + r.label + '" - nothing was deleted.');
      else { busy(kind === 'download' ? 'Downloaded "' + r.label + '".' : 'Deleted "' + r.label + '".'); load(); }
    })
    .catch(function(e){ busy('Request failed: ' + e); });
}

document.getElementById('btn-refresh').onclick = function () { load(); };
load();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _fmt, *_args):
        pass  # no console access logs (pythonw has no stderr anyway)

    def _send_text(self, body):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            init = json.dumps(
                {"cfg": config_mod.load(), "data": _page_data(), "live": dict(LIVE_STATUS)},
                ensure_ascii=False,
            ).replace("</", "<\\/")
            self._send_text(SETTINGS_PAGE.replace("__THEME__", _THEME).replace("__INIT_JSON__", init))
        elif self.path == "/status":
            self._send_json(dict(LIVE_STATUS))
        elif self.path == "/models":
            self._send_json(_models_listing())
        elif self.path == "/models.html":
            self._send_text(MODELS_PAGE.replace("__THEME__", _THEME).replace("__CACHE__", _hf_cache_dir()))
        elif self.path == "/favicon.ico":
            self._send_text("")
        else:
            self._send_json({"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            reader = getattr(self, "rfile", None) or self.r
            raw = reader.read(length)
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        if self.path == "/save":
            try:
                cfg = _norm_cfg(body.get("cfg"))
                live = dict(LIVE_CFG) if LIVE_CFG is not None else config_mod.load()
                live.update(cfg)
                cfg = _norm_cfg(live)
                config_mod.save(cfg)
                if LIVE_CFG is not None:
                    # app.py aliases this very dict as its CFG, so keep the object
                    # and never empty it: one atomic update, then drop stale keys.
                    LIVE_CFG.update(cfg)
                    for _gone in [k for k in LIVE_CFG if k not in cfg]:
                        del LIVE_CFG[_gone]
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})
        elif self.path == "/detect-key":
            try:
                timeout = min(max(float(body.get("timeout") or 10), 1), 30)
            except (TypeError, ValueError):
                timeout = 10
            key = detect_key(timeout)
            self._send_json({"key": key, "error": None if key else "No key detected - try again."})
        elif self.path == "/test-mic":
            cfg = _norm_cfg(body.get("cfg"))
            try:
                audio = recorder.record_fixed(4, device=cfg["input_device"])
                seconds = len(audio) / recorder.SAMPLERATE
                pk = recorder.peak(audio)
                text = asr.transcribe(audio, cfg)
                out = {
                    "heard": text,
                    "seconds": round(seconds, 1),
                    "peak": round(pk, 4),
                    "note": ("no audio returned - that source produced nothing"
                             if seconds == 0 else ("silence" if pk < 0.002 else None)),
                }
                self._send_json(out)
            except Exception as e:
                self._send_json({"error": str(e)})
        elif self.path == "/models/download":
            self._send_json(_model_download(body.get("repo")))
        elif self.path == "/models/delete":
            self._send_json(_model_delete(body.get("repo")))
        else:
            self._send_json({"error": "unknown endpoint"})


def _page_data():
    return {
        "keys": [[k, _key_label(k)] for k in TRIGGER_KEYS],
        "keynames": list(KEY_VK.keys()),
        "mics": [[v, label] for v, label in _mic_options()],
        "engines": [[e, ENGINE_LABELS[e]] for e in ENGINE_ORDER],
        "whisper_sizes": [[s, WHISPER_SIZE_LABELS[s]] for s in WHISPER_SIZES],
        "canary": [[m, label] for m, label in CANARY_MODELS],
        "parakeet": [[m, label] for m, label in PARAKEET_MODELS],
        "vosk": [[m, label] for m, label in VOSK_MODELS],
        "languages": [[c, label] for c, label in LANGUAGES],
        "outputs": [[m, label] for m, label in OUTPUT_MODES],
        "live_mode": [["off", "Off - type after you release"],
                      ["on", "On - type while you speak"]],
        "compute": [["cpu", "CPU - all cores"], ["gpu", "GPU - CUDA (falls back to CPU)"]],
    }


def _models_listing():
    """Presets + any physically-present model not in the preset list."""
    cache = _hf_cache_dir()
    present = {}
    if os.path.isdir(cache):
        try:
            for entry in os.scandir(cache):
                name = entry.name
                if not name.startswith("models--"):
                    continue
                repo = name[len("models--"):].replace("--", "/")
                present[repo] = _dir_bytes(entry.path)
        except OSError:
            pass

    rows = []
    seen = set()

    def add(engine, label, repo, note=""):
        r = str(repo)
        rows.append(
            {
                "label": label,
                "engine": engine,
                "repo": r,
                "note": note,
                "bio": MODEL_BIO.get(r, ""),
                "installed": r in present,
                "size": _fmt_bytes(present[r]) if r in present else "",
            }
        )
        seen.add(r)

    for size in WHISPER_SIZES:
        add("whisper", "Whisper " + size, "Systran/faster-whisper-" + size)
    for repo, label in CANARY_MODELS:
        add("canary", label, repo)
    for repo, label in PARAKEET_MODELS:
        add("parakeet", label, repo)
    # Every other Canary model NVIDIA publishes, flagged honestly: these ship
    # only NeMo (.nemo) checkpoints, which transformers cannot load, so they
    # stay out of the settings dropdown and cannot be downloaded here.
    nemo_note = "NeMo checkpoint only - this app cannot load it"
    for engine, label, repo in UNLOADABLE_MODELS:
        add(engine, label, repo, note=nemo_note)

    # Anything physically present that is not one of the presets above.
    for repo, _b in sorted(present.items()):
        if repo not in seen:
            add("other", repo, repo)

    return {"rows": rows, "cache": cache}


def _dir_bytes(path):
    total = 0
    for _root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(_root, fn))
            except OSError:
                pass
    return total


def _fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def _model_download(repo):
    if not repo:
        return {"error": "no model selected"}
    try:
        from huggingface_hub import snapshot_download
    except Exception:
        return {"error": "huggingface_hub not available; set this model active and run a test instead"}
    try:
        # skip .nemo checkpoint duplicates: the runtime loads only the HF-native
        # files, and the .nemo blobs are the same weights again.
        snapshot_download(repo_id=repo, allow_patterns=["*.safetensors", "*.json", "*.model", "*.txt", "*.bin"])
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


def _model_delete(repo):
    if not repo:
        return {"error": "no model selected"}
    cache = _hf_cache_dir()
    prefix = "models--" + str(repo).replace("/", "--")
    removed = 0
    if os.path.isdir(cache):
        for entry in list(os.scandir(cache)):
            if entry.name == prefix or entry.name.startswith(prefix + "--"):
                try:
                    shutil.rmtree(entry.path)
                    removed += 1
                except Exception as e:
                    return {"error": str(e)}
    return {"ok": True, "removed": removed}


def start_server():
    """Serve the settings + models pages for the lifetime of the app. Returns the
    URL. The serve thread is a daemon so it never blocks the app from exiting."""
    global SERVER, SETTINGS_URL
    for port in [PREFERRED_PORT] + [random.randint(49152, 65535) for _ in range(6)]:
        try:
            srv = HTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            continue
        SERVER = srv
        SETTINGS_URL = f"http://127.0.0.1:{port}/"
        threading.Thread(
            target=srv.serve_forever, name="voice-dictation-settings", daemon=True
        ).start()
        return SETTINGS_URL
    _log_note("settings: could not open a local server port")
    return None


def open_in_browser():
    """Open the running settings page in a browser; returns True on success."""
    if not SETTINGS_URL:
        return False
    if webbrowser.open(SETTINGS_URL):
        return True
    _log_note(f"settings: no browser opened, URL was {SETTINGS_URL}")
    return False


def open_models_in_browser():
    if not SETTINGS_URL:
        return False
    if webbrowser.open(SETTINGS_URL + "models.html"):
        return True
    _log_note(f"models: no browser opened, URL was {SETTINGS_URL}models.html")
    return False


def open_settings_standalone(cfg):
    """Debug entry: run just the settings server + browser; Ctrl+C to exit."""
    global LIVE_CFG
    LIVE_CFG = cfg
    url = start_server()
    if not url:
        return
    open_in_browser()
    print(f"Settings page at {url}; close the tab when done, then press Ctrl+C.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    # let the serve thread leave, so its listening socket is closed properly
    # (shutdown() blocks until serve_forever finishes, which it does here).
    try:
        SERVER.shutdown()
    except Exception:
        pass


if __name__ == "__main__":
    open_settings_standalone(config_mod.load())
