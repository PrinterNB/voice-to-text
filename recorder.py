import re
import shutil
import subprocess
import time

import numpy
import sounddevice

SAMPLERATE = 16000
# Spawned helpers (ffmpeg per capture chunk) must NOT open a console window: on
# Windows each spawn would grab a conhost window and steal keyboard focus.
NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _list_dshow():
    """List the Windows microphone inputs ffmpeg's DirectShow can capture.

    This is what the settings UI shows: unlike PortAudio/sounddevice, DirectShow
    enumerates real microphones (headset mics included)."""
    ff = _ffmpeg_path()
    if not ff:
        return []
    try:
        p = subprocess.run(
            [ff, "-hide_banner", "-f", "dshow", "-list_devices", "true", "-i", "video=none"],
            capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL, creationflags=NO_CONSOLE,
        )
    except Exception:
        return []
    out = []
    for line in (p.stdout + p.stderr).splitlines():
        m = re.search(r'"(.+?)"\s+\(audio\)', line)
        if m:
            out.append(m.group(1))
    return out


def _ffmpeg_path():
    return shutil.which("ffmpeg")


def _list_sd():
    """List input-device indices visible to sounddevice/PortAudio."""
    try:
        devs = sounddevice.query_devices()
    except Exception:
        return []
    out = []
    for i, d in enumerate(devs):
        try:
            if str(d.get("type", "")).upper().startswith("INPUT") and d.get("channels", 0) > 0:
                out.append((i, str(d.get("name", "input"))))
        except Exception:
            continue
    return out


def _pick_sd():
    """Return a sounddevice index preferring anything named like a microphone."""
    for i, name in _list_sd():
        low = name.lower()
        if "mic" in low or "audio in" in low or "microphone" in low:
            return i
    xs = _list_sd()
    return xs[0][0] if xs else None


def _resolve(device):
    """Map a user token to a concrete (backend, value) pair.

    '' / '-1' / None -> auto: use DirectShow on Windows (sees real mics), else
    PortAudio. 'ds:<name>' -> DirectShow. 'sd:<idx>' -> PortAudio index.
    """
    if isinstance(device, str):
        s = device.strip()
        if s and s != "-1":
            if s.startswith("ds:"):
                return ("dshow", s[3:])
            if s.startswith("sd:"):
                try:
                    return ("sd", int(s[3:]))
                except ValueError:
                    pass
            try:
                return ("sd", int(s))
            except ValueError:
                pass
    # auto
    for name in _list_dshow():
        low = name.lower()
        if "mic" in low or "headset" in low or "microphone" in low:
            return ("dshow", name)
    ds = _list_dshow()
    if ds:
        return ("dshow", ds[0])
    return ("sd", _pick_sd())


def _ffmpeg_chunk(name, seconds):
    """One capped DirectShow read. `-t` is an INPUT option (before -i): with no
    cap the DirectShow source closes after ~0.1s on Windows capture filters, so
    every one-shot capture is capped. Multiple input copies in one process do
    NOT work here (the second copy gets no data), so hold-to-talk capture uses
    _dshow_key_held instead: one long capped stream per span, read in pieces.
    A single capped stream with one input does stream continuously to 30s+."""
    ff = _ffmpeg_path()
    if not ff:
        return numpy.zeros(0, dtype=numpy.int16)
    args = [ff, "-hide_banner", "-y", "-t", repr(float(seconds)),
            "-f", "dshow", "-i", f"audio={name}",
            "-vn", "-ac", "1", "-ar", str(SAMPLERATE),
            "-acodec", "pcm_s16le", "-f", "s16le", "pipe:1"]
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=max(10.0, seconds * 4),
                          stdin=subprocess.DEVNULL, creationflags=NO_CONSOLE)
    except Exception:
        return numpy.zeros(0, dtype=numpy.int16)
    return numpy.frombuffer(p.stdout, dtype=numpy.int16)


def _dshow_key_held(name, is_key_held, timeout, chunk=0.5, on_start=None, idle_wait=5.0,
                    release_grace=0.5, on_chunk=None):
    """Capture continuously and keep only the audio while the key is held.

    The stream runs whether or not the key is down, so it is already rolling
    when the key goes down: the piece that contains the press is kept, which
    means listening starts the instant the key is pressed. At the other end,
    release is NOT an immediate cut: the chunk in hand was still recording
    while the key was down, so it is kept too, and the stream keeps running
    `release_grace` seconds past the release - words spoken the moment the
    key came up must survive.

    Capture is ONE long capped dshow stream per 30s span (one capped input
    streams continuously; an uncapped one EOFs after ~0.1s, and multiple input
    copies in one process get no data). stdout is read in `chunk`-sized pieces
    and the key is checked between pieces, so release is caught within ~chunk
    and the stream is terminated the moment the key comes up. One startup per
    span keeps ~95% of real time instead of ~23% for one spawn per chunk.

    `timeout` caps one dictation once the key registers; `idle_wait` caps the
    wait for a press - after that we return held=False so the caller can
    re-read the config (a trigger key changed on the settings page takes
    effect within idle_wait, no app restart). Returns
    (audio, duration_seconds, key_was_held)."""
    parts = []
    total = 0
    started = False
    released = False
    grace_taken = 0
    grace_target = int(release_grace * SAMPLERATE)
    t0 = time.time()
    t_press = t0
    t_release = None
    read_bytes = int(chunk * SAMPLERATE) * 2
    span = 30.0
    dead_spans = 0
    stop = False
    ff = _ffmpeg_path()
    if not ff:
        return numpy.zeros(0, dtype=numpy.float32), 0.0, False
    while not stop:
        now = time.time()
        if not started and now - t0 > idle_wait:
            break
        if started and now - t_press > timeout:
            break
        args = [ff, "-hide_banner", "-y", "-t", repr(span),
                "-f", "dshow", "-i", f"audio={name}",
                "-vn", "-ac", "1", "-ar", str(SAMPLERATE),
                "-acodec", "pcm_s16le", "-f", "s16le", "pipe:1"]
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL,
                                    creationflags=NO_CONSOLE)
        except Exception:
            break
        span_bytes = 0
        try:
            while True:
                data = proc.stdout.read(read_bytes)
                if not data:
                    break  # this span's stream ended; re-spawn if still going
                span_bytes += len(data)
                raw = numpy.frombuffer(data, dtype=numpy.int16)
                if is_key_held():
                    if not started:
                        started = True
                        t_press = time.time()
                        if on_start:
                            on_start()
                    parts.append(raw)
                    total += raw.size
                    if on_chunk:
                        try:
                            on_chunk(raw.astype(numpy.float32) / 32768.0)
                        except Exception:
                            pass  # a live-typing hiccup must not stop capture
                elif started and not released:
                    # Key just came up. The chunk in hand was still recording
                    # while it was down, so keep it (dropping it used to eat
                    # up to a chunk of the speaker's last words), then start
                    # the grace window.
                    released = True
                    t_release = time.time()
                    parts.append(raw)
                    total += raw.size
                    if on_chunk:
                        try:
                            on_chunk(raw.astype(numpy.float32) / 32768.0)
                        except Exception:
                            pass
                elif released:
                    want = grace_target - grace_taken
                    take = raw[:min(want, raw.size)] if want > 0 else raw[:0]
                    parts.append(take)
                    total += take.size
                    if on_chunk and take.size:
                        try:
                            on_chunk(take.astype(numpy.float32) / 32768.0)
                        except Exception:
                            pass
                    grace_taken += raw.size
                    if grace_taken >= grace_target:
                        proc.terminate()  # grace done: cut the long stream short
                        stop = True
                        break
                if started and time.time() - t_press > timeout:
                    proc.terminate()
                    stop = True
                    break
                if not started and time.time() - t0 > idle_wait:
                    proc.terminate()
                    stop = True
                    break
        except Exception:
            stop = True
        if span_bytes == 0:
            dead_spans += 1
            if dead_spans >= 2:
                break  # source produced nothing twice: stop, caller reports it
    if not parts:
        return numpy.zeros(0, dtype=numpy.float32), 0.0, started
    cat = numpy.concatenate(parts)
    # Report how long the key was actually held, not the audio length: the
    # kept audio now also carries the grace tail, and the caller's "heard Xs"
    # note and its too-short guard want the hold time itself.
    dur = (t_release - t_press) if t_release else total / float(SAMPLERATE)
    return cat.astype(numpy.float32) / 32768.0, dur, started


def _sd_fixed(seconds, samplerate, dev):
    frames = []

    def collect(data, _frames, _t, _status):
        frames.append(numpy.frombuffer(data, dtype=numpy.int16))

    with sounddevice.RawInputStream(
        samplerate=samplerate, blocksize=1600, device=dev,
        dtype="int16", channels=1, callback=collect,
    ):
        time.sleep(seconds)
    return _to_float(frames)


def _sd_key_held(is_key_held, timeout, dev, on_start=None, idle_wait=5.0,
                 release_grace=0.5, on_chunk=None):
    """PortAudio twin of _dshow_key_held (same idle_wait/timeout split)."""
    frames = []

    def collect(data, _frames, _t, _status):
        frames.append(numpy.frombuffer(data, dtype=numpy.int16))

    started = False
    t_release = None
    t0 = time.time()
    t_press = t0
    duration = 0.0
    seen = 0
    with sounddevice.RawInputStream(
        samplerate=SAMPLERATE, blocksize=1600, device=dev,
        dtype="int16", channels=1, callback=collect,
    ):
        while True:
            now = time.time()
            if not started and now - t0 > idle_wait:
                break
            if started and now - t_press > timeout:
                break
            if is_key_held():
                if not started:
                    started = True
                    t_press = now
                    if on_start:
                        on_start()
                duration = time.time() - t_press
            elif started:
                # Key came up: keep the stream alive for release_grace so the
                # words of the release moment are still collected.
                if t_release is None:
                    t_release = now
                elif now - t_release > release_grace:
                    break
            else:
                del frames[:]  # idle: drop anything heard before the press
            if started and on_chunk and len(frames) > seen:
                blob = b"".join(frames[seen:])
                seen = len(frames)
                try:
                    on_chunk(numpy.frombuffer(blob, dtype=numpy.int16).astype(
                        numpy.float32) / 32768.0)
                except Exception:
                    pass  # a live-typing hiccup must not stop capture
            time.sleep(0.05)
        deadline = time.time() + 0.25
        while time.time() < deadline:
            time.sleep(0.01)
    if not started:
        return numpy.zeros(0, dtype=numpy.float32), 0.0, False
    if on_chunk and len(frames) > seen:
        # the frames every break skipped: the release moment - the tail
        # release_grace exists for, which live typing must still get
        try:
            on_chunk(numpy.frombuffer(b"".join(frames[seen:]), dtype=numpy.int16).astype(
                numpy.float32) / 32768.0)
        except Exception:
            pass
    return _to_float(frames), duration, started


def record_fixed(seconds, samplerate=SAMPLERATE, device=None):
    mode, val = _resolve(device)
    if mode == "dshow":
        raw = _ffmpeg_chunk(val, seconds)
        return raw.astype(numpy.float32) / 32768.0
    return _sd_fixed(seconds, samplerate, val)


def record_key_held(is_key_held, timeout=600, device=None, on_start=None, idle_wait=5.0,
                    release_grace=0.5, on_chunk=None, stream_chunk=None):
    """Capture continuously; keep audio only while `is_key_held()` is true.

    Capture runs ahead of the press, so listening begins the moment the key
    goes down, and runs `release_grace` seconds past the release, so the words
    of letting go survive. `idle_wait` bounds the wait for a press: when nothing
    was ever held we return quickly so callers can re-read their config (a
    trigger key changed on the settings page applies within idle_wait). `on_start`
    is called once when the key first registers; `on_chunk(float32 chunk)` gets
    every kept chunk as it is captured (held audio plus the grace tail) - live
    typing uses it. `stream_chunk` sets the dshow read/delivery granularity
    (default 0.5 s): live callers pass a smaller value so audio ARRIVES - and
    a live hypothesis is delivered - sooner, which is part of the live-typing
    delay; key release is also caught at this granularity. Returns (audio,
    duration_seconds, key_was_held)."""
    mode, val = _resolve(device)
    if mode == "dshow":
        return _dshow_key_held(val, is_key_held, timeout,
                               chunk=stream_chunk or 0.5, on_start=on_start,
                               idle_wait=idle_wait, release_grace=release_grace,
                               on_chunk=on_chunk)
    return _sd_key_held(is_key_held, timeout, val, on_start, idle_wait,
                        release_grace, on_chunk)


def _to_float(frames):
    if not frames:
        return numpy.zeros(0, dtype=numpy.float32)
    raw = numpy.frombuffer(b"".join(frames), dtype=numpy.int16)
    return raw.astype(numpy.float32) / 32768.0


def peak(audio):
    if audio is None or len(audio) == 0:
        return 0.0
    return float(numpy.abs(audio).max())
