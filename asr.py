import json
import os
import threading
import time

import config as config_mod

import numpy

_CACHE = {}


def _threads():
    """Use every core: ctranslate2 defaults to 2 threads, which crawls on a 16-core CPU."""
    return max(1, os.cpu_count() or 1)


def transcribe(audio, cfg):
    """Transcribe float32 audio (16 kHz). Dispatches on cfg["engine"]."""
    gpu = bool(cfg.get("gpu", False))
    if cfg.get("engine") == "vosk":
        # online/streaming engine - see _vosk_* below
        return _vosk(audio, cfg.get("vosk_model") or "vosk-model-small-en-us-0.15")
    if cfg.get("engine", "whisper") == "whisper":
        return _whisper(audio, cfg.get("whisper_model", "base"), cfg.get("language"), gpu)
    model_id = cfg.get("hf_model") or config_mod.DEFAULTS["hf_model"]
    return _hf_transformers(audio, model_id, gpu)


def _whisper(audio, size, language, gpu=False):
    key = ("fw", size, gpu)
    model = _CACHE.get(key)
    if model is None:
        from faster_whisper import WhisperModel

        if gpu:
            try:
                model = WhisperModel(size, device="cuda", compute_type="int8_float16")
            except Exception:
                model = WhisperModel(size, device="cpu", compute_type="int8",
                                     cpu_threads=_threads())
                _CACHE[("fw", size, False)] = model
                _CACHE.pop(key, None)
                key = ("fw", size, False)
        else:
            model = WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=_threads())
        _CACHE[key] = model
    segments, _info = model.transcribe(
        audio,
        language=language or None,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


def _hf_transformers(audio, model_id, gpu=False):
    """Canary / Parakeet / custom HF models through the transformers ASR
    pipeline. The model must have an HF-native config (model_type); repos that
    ship only a NeMo checkpoint (.nemo) cannot be loaded this way - the preset
    list avoids those, and this raises a clear message for anything else."""
    key = ("hf", model_id, gpu)
    pipe = _CACHE.get(key)
    if pipe is None:
        from transformers import pipeline
        import torch

        torch.set_num_threads(_threads())
        dev = 0 if gpu and torch.cuda.is_available() else -1
        try:
            pipe = pipeline("automatic-speech-recognition", model=model_id, device=dev)
        except Exception as e:
            raise OSError(
                "%s cannot be loaded as a transformers ASR model: %s "
                "(models that only ship a .nemo checkpoint are not supported here)" % (model_id, e)
            )
        _CACHE[("hf", model_id, dev == 0)] = pipe
    return _run_hf(pipe, audio)


def _run_hf(pipe, audio):
    out = pipe({"raw": audio, "sampling_rate": 16000})
    return (out["text"] if isinstance(out, dict) else out).strip()


def _to16(audio):
    """The pipeline is float32 in [-1, 1]; Kaldi/Vosk want int16 bytes."""
    return numpy.clip(numpy.asarray(audio, dtype=numpy.float32) * 32768.0,
                      -32768.0, 32767.0).astype(numpy.int16).tobytes()


def _vosk_model(name):
    """Vosk model dir + object: ~/.cache/vosk/<name>, the zip downloaded once
    from alphacephei.com - Kaldi models are NOT on Hugging Face, so this repo
    keeps its own cache next to the HF one."""
    key = ("vosk", name)
    model = _CACHE.get(key)
    if model is not None:
        return model
    import urllib.request
    import zipfile
    import vosk

    base = os.path.join(os.path.expanduser("~"), ".cache", "vosk")
    model_dir = os.path.join(base, name)
    if not os.path.isdir(model_dir):
        os.makedirs(base, exist_ok=True)
        zip_path = os.path.join(base, name + ".zip")
        urllib.request.urlretrieve("https://alphacephei.com/vosk/models/%s.zip" % name,
                                  zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(base)
        os.remove(zip_path)
    model = vosk.Model(model_dir)
    _CACHE[key] = model
    return model


def _vosk(audio, model_name):
    """Whole-audio transcription through Vosk. Kaldi finalizes whole
    utterances: feed everything, SetEos, collect every utterance's text."""
    import vosk

    rec = vosk.KaldiRecognizer(_vosk_model(model_name), 16000.0)
    out = []
    for i in range(0, len(audio), 8000):  # 0.5 s frames; far faster than real time
        # AcceptWaveform returning 1 = an utterance boundary was reached:
        # Result() then hands back that utterance's finalized text.
        if rec.AcceptWaveform(_to16(audio[i:i + 8000])):
            text = json.loads(rec.Result()).get("text")
            if text:
                out.append(text)
    text = json.loads(rec.Result()).get("text")  # flush the in-progress one
    if text:
        out.append(text)
    text = " ".join(out).replace("[no-speech]", " ").replace("[unk]", " ")
    return " ".join(text.split())


class LiveSession:
    """Progressive live typing for engines WITHOUT a native streaming API
    (the Vosk engine HAS one and takes the direct native path: AcceptWaveform
    in, finalized text + partial typed as an append-only diff - no worker, no
    re-listen). For the offline engines (Whisper/Canary/Parakeet/custom)
    transcription runs in a WORKER THREAD that
    constantly re-transcribes the LATEST window as fast as the model can run
    it: feed() never blocks, so capture and the user's speech are never
    delayed by model time - the wall-time pass interval simply becomes the
    model's speed, the shortest thing any engine can offer. Hypotheses are
    picked up (and typed) on the next feed. A worker pass only starts once at
    least STEP seconds of NEW audio arrived, relaxed to PASS_SLACK of a
    slower model's speed so passes cannot stack and fall behind at a growing
    rate. Passes only re-listen the last WINDOW_MAX seconds (sliding
    window): per-pass cost stays bounded even for long dictations, and long
    audio drifts out of re-reading - long-form decoders re-chunk big
    windows and rewrite their own earlier text, which is what stalled the
    old full-window agreement forever after the first sentence. The FIRST
    hypothesis commits immediately (minus the word in flight) so typing
    starts within about a second of speech; each pass APPENDS only what its
    text adds past the longest word run it can match at the end of what is
    already typed - at ANY offset in the pass, tolerating positions the pass
    re-tokenized inside that run (drift must not stall growth) - always
    holding SAFETY_WORDS trailing words back -
    append-only by construction, nothing typed live is ever retracted. A
    pass with zero overlap cannot be aligned and is skipped.

    feed(chunk) returns text newly safe to type ("" most calls);
    finish() stops the worker and flushes the rest;
    stop() just ends the worker (aborted cycles)."""

    SR = 16000
    STEP = 0.25           # MINIMUM seconds of new audio between worker passes
    WINDOW_MAX = 6.0      # SLIDING live window (WhisperLive-style): every
                          # pass only re-listens the last WINDOW_MAX seconds.
                          # Kept SHORT on purpose: a pass costs roughly what
                          # the window costs, and the worker starts a new
                          # pass as soon as its audio arrives - so this size
                          # sets the pass INTERVAL and the wall between the
                          # newest speech and its hypothesis (the live delay
                          # floor). 6 s still re-listens many sentences of
                          # context, enough for word-level alignment to
                          # anchor anywhere, while halving per-pass cost vs
                          # 12 s; long audio drifting out of re-reading also
                          # stops long-form re-chunking ever stalling growth
                          # (the first-then-silence symptom)
    SAFETY_WORDS = 1      # only the word in flight is held back
    PASS_SLACK = 0.8      # a model slower than STEP may start a pass on
                          # slightly LESS new audio: covering dt*0.8 < dt
                          # seconds per dt seconds keeps up with real-time
                          # speech instead of drifting behind it

    def __init__(self, cfg):
        self.cfg = cfg
        self.chunks = []
        self.head = 0      # frames dropped from the front of the window
        self.total = 0     # frames fed so far
        self.committed = ""
        self.last_error = None
        self.lock = threading.Lock()
        self.hyp = None    # latest unconsumed hypothesis from the worker
        self.stop_flag = threading.Event()
        # Vosk is the ONE engine here with a native streaming API
        # (AcceptWaveform/GetResult): Kaldi hands back finalized utterance
        # text plus a "partial" for the word in flight, so it needs none of
        # the worker/re-listen/alignment machinery below - feed() speaks
        # straight into the recognizer and text is typed as an append-only
        # diff of the safe view (final + partial minus SAFETY_WORDS).
        self.native = cfg.get("engine", "whisper") == "vosk"
        if self.native:
            self.worker = None
            self.rec = None        # recognizer made on the first feed
            self.v_final = ""      # finalized utterances (safe text)
            self.v_partial = ""    # current utterance candidate words
            self.typed_raw = ""    # what the native path emitted (prefix)
        else:
            self.worker = threading.Thread(target=self._worker, name="live-asr",
                                          daemon=True)  # never holds the process
            # (a leaked session - e.g. recorder crashed before its stop - must
            # not keep the app or a test alive; stop()/finish() join it normally)
            self.worker.start()

    def stop(self):
        # for cycles that never reach finish() (key never pressed etc.)
        if self.worker is None:
            return  # native Vosk path has no worker: feed() did the work
        self.stop_flag.set()
        try:
            self.worker.join(timeout=10)
        except Exception:
            pass

    def feed(self, audio):
        if audio is None or len(audio) == 0:
            return ""
        if self.native:
            return self._feed_vosk(audio)
        with self.lock:
            self.chunks.append(numpy.asarray(audio, dtype=numpy.float32))
            self.total += len(audio)
        with self.lock:
            h, self.hyp = self.hyp, None
        if h is None:
            return ""
        return self._consume(h)

    def finish(self):
        self.stop_flag.set()
        if self.worker is not None:
            try:
                self.worker.join(timeout=10)  # let a pass in flight finish first
            except Exception:
                pass
        if self.native:
            return self._finish_vosk()
        window = self._window()
        if window.size == 0:
            return ""
        try:
            h = transcribe(window, self.cfg).strip()
        except Exception as e:
            self.last_error = str(e)
            return ""
        return self._consume(h, final=True)

    def _worker(self):
        # feed() never blocks, so new audio arrives at real speech speed:
        # a pass starting after 'need' new frames makes wall interval
        # ~= model run time - as tight as the engine allows
        last_total = 0
        dt = 0.0
        while not self.stop_flag.is_set():
            with self.lock:
                total = self.total
            need = max(self.STEP, dt * self.PASS_SLACK) * self.SR
            if total - last_total < need:
                self.stop_flag.wait(0.05)  # sleeps, but notices stop
                continue
            last_total = total
            try:
                with self.lock:
                    window = self._window()
                if window.size == 0:
                    continue
                t0 = time.perf_counter()
                h = transcribe(window, self.cfg).strip()
                dt = time.perf_counter() - t0
            except Exception as e:
                self.last_error = str(e)
                continue
            with self.lock:
                self.hyp = h

    def _consume(self, h, final=False):
        """Append-only growth with word-level alignment (see the align block
        below the docstring): the longest run at the END of what we typed is
        located ANYWHERE inside this hypothesis - it need not sit at h's
        start, because long-form decoders re-read (differently!) their own
        earlier text on longer windows, and that drift must not stall growth.
        Everything h has AFTER that run is text over recent audio = new, so
        it is appended (minus SAFETY_WORDS trailing words while live).
        A pass whose text overlaps NOTHING typed cannot be describing the
        audio we already typed (passes only ever hear the newest window),
        so it is a gap - a pause longer than the window - and its text is
        newer: append it like any other pass. Append-only by construction:
        nothing typed live is ever retracted."""
        # align: longest word run at the END of what we typed that appears
        # (up to the drift tolerance below) ANYWHERE inside this hypothesis -
        # the run may sit after this pass's re-read of older audio, because
        # long-form decoders re-tokenize their own earlier text on a longer
        # window and that must not stall growth. Everything h has after that
        # run is text over recent audio = new; append-only, so nothing typed
        # is ever retracted.
        cw = self.committed.split()
        hw = h.split()
        # match the longest run at the END of typed text inside h, at ANY
        # offset, tolerating up to a third of the run's positions being
        # re-tokenized (long-form decoders re-read some of their own older
        # text on longer windows - that drift must not break the alignment,
        # but >=2/3 of the same words at one place is strong evidence this
        # pass is looking at the same audio we already typed).
        m_best, o_best, mism_best = 0, 0, 0
        for m in range(min(len(cw), len(hw)), 0, -1):
            tol = m // 3 if m >= 3 else 0
            tail = cw[len(cw) - m:]
            best_o, best_mism = None, m
            for o in range(0, len(hw) - m + 1):
                mism = 0
                for a, b in zip(tail, hw[o:o + m]):
                    if a != b:
                        mism += 1
                        if mism > tol:
                            break
                if mism <= tol and mism < best_mism:
                    best_o, best_mism = o, mism
            if best_o is not None:
                m_best, o_best, mism_best = m, best_o, best_mism
                break
        if self.committed and not m_best:
            # the typed tail is in NO part of this pass: the window has
            # slid PAST that audio - a long pause inside one dictation
            # (older than the window) - so everything this pass heard is
            # newer text than anything typed: a gap, not a conflict.
            # (This also subsumes the old full-text flush fallback: if h
            # began with the typed text, its end WOULD appear in h, so a
            # zero-match h never contains it.)
            words = list(hw)
        else:
            words = hw[o_best + m_best:]
        if not final:
            if len(words) <= self.SAFETY_WORDS:
                return ""
            words = words[:-self.SAFETY_WORDS]
        if not words:
            return ""
        new = " ".join(words)
        out = ((" " if self.committed else "") + new)
        self.committed = (self.committed + " " + new).strip()
        return out

    def _vosk_rec(self):
        if self.rec is None:
            try:
                import vosk

                model = _vosk_model(self.cfg.get("vosk_model")
                                   or "vosk-model-small-en-us-0.15")
                self.rec = vosk.KaldiRecognizer(model, 16000.0)
            except Exception as e:
                self.last_error = str(e)
                raise
        return self.rec

    def _feed_vosk(self, audio):
        try:
            rec = self._vosk_rec()
            # 1 = utterance finished (Result: finalized text), 0 = still in
            # progress (PartialResult: the candidate words in flight)
            done = rec.AcceptWaveform(_to16(audio))
            res = json.loads(rec.Result() if done else rec.PartialResult())
        except Exception as e:
            self.last_error = str(e)
            return ""
        if "text" in res:
            self.v_final = (self.v_final + " " + res["text"]).strip()
            self.v_partial = ""
        else:
            self.v_partial = res.get("partial") or ""
        return self._emit_vosk(final=False)

    def _finish_vosk(self):
        try:
            rec = self._vosk_rec()
            res = json.loads(rec.Result())  # flushes the in-progress utterance
            if res.get("text"):
                self.v_final = (self.v_final + " " + res["text"]).strip()
        except Exception as e:
            self.last_error = str(e)
        self.v_partial = ""
        return self._emit_vosk(final=True)

    def _emit_vosk(self, final):
        """Append-only diff typing of the safe view = finalized utterances +
        partial minus SAFETY_WORDS trailing words (the word in flight). When
        an utterance finalizes, Kaldi may re-think a word of the partial
        (rare): on such a rewrite only the part past the SHARED PREFIX is
        typed, so what was typed live is never retracted."""
        view = (self.v_final + " " + self.v_partial).strip()
        if not final:
            words = view.split()
            if len(words) <= self.SAFETY_WORDS:
                return ""
            view = " ".join(words[:-self.SAFETY_WORDS])
        if view.startswith(self.typed_raw):
            n = len(self.typed_raw)
        else:
            n = 0
            for a, b in zip(self.typed_raw, view):
                if a != b:
                    break
                n += 1
        delta = view[n:]
        if not delta:
            return ""
        out = ((" " if self.typed_raw else "") + delta)
        self.typed_raw = view
        return out

    def _window(self):
        if not self.chunks:
            return numpy.zeros(0, dtype=numpy.float32)
        cat = numpy.concatenate(self.chunks)
        cap = int(self.WINDOW_MAX * self.SR)
        skip = max(self.head, cat.size - cap)
        if skip:
            cat = cat[skip:]
        self.head = skip
        # storage follows the window: drop chunks fully before it, or a long
        # dictation keeps every frame and every pass costs the whole session
        while len(self.chunks) > 1 and len(self.chunks[0]) <= self.head:
            self.head -= len(self.chunks.pop(0))
        return cat


