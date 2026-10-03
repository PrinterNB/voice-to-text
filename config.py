import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULTS = {
    "trigger_key": "f9",
    "output_mode": "autotype",
    "engine": "whisper",
    "whisper_model": "base",
    "hf_model": "nvidia/canary-1b-v2",
    "language": None,
    "commands": [],
    # Microphone source: "" = auto-pick, "ds:<name>" = Windows/DirectShow source
    # (the real mic list), "sd:<idx>" = PortAudio input index.
    "input_device": "",
    # Transcription compute: false = CPU only (every core), true = CUDA GPU
    # (falls back to CPU when no GPU is available).
    "gpu": False,
    # Live typing: type recognized words while still holding the key (works
    # with every engine; only stays in sync with fast models). Off by default.
    "live_mode": False,
    # Vosk engine (true streaming ASR): Kaldi model name, downloaded from
    # alphacephei.com into ~/.cache/vosk on first use.
    "vosk_model": "vosk-model-small-en-us-0.15",
}


def load():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                cfg.update(stored)
        except (OSError, ValueError, TypeError):
            pass  # corrupt config.json must not brick the app -> defaults
    return cfg


def save(cfg):
    tmp = CONFIG_PATH + ".tmp"  # same directory, so os.replace is atomic
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)
