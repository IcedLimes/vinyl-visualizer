"""Audio side: records what Spotify plays from PipeWire (through parec) and
turns it into the spectrum ring's levels and the record's beat pulse."""

import collections
import json
import math
import os
import select
import subprocess
import threading
import time

import numpy as np

RATE = 48000
FFT_SIZE = 2048
HOP = 512               # analysis step: 10.7 ms, two per 1024-sample PipeWire block
HOP_TIME = HOP / RATE
QUANTUM = 1024 / 48000  # PipeWire's default processing block, for latencies given in blocks

# Delays around the analysis, for lining the pulse up with what you hear.
CAPTURE_LATENCY = 0.030  # graph block, capture buffer and analysis window, before a hit is seen
RENDER_LATENCY = 0.033   # waiting for the next frame, the compositor and the display
LEAD = 0.030             # land a touch before the sound: that's where sync feels exact


class Capture:
    """Keeps the newest samples of Spotify's own output stream, so system sounds
    don't move the record. While Spotify has no stream open (it closes it after
    a while paused) it listens to the default speakers and switches back as soon
    as the stream reappears. source="system" always uses the speakers."""

    def __init__(self, source="spotify"):
        self.source = source
        self._ring = np.zeros((RATE, 2), dtype=np.float32)  # stereo: vocals sit in the centre
        self._written = 0          # samples captured so far
        self._last_write = 0.0     # when the newest of them arrived
        self._lock = threading.Lock()
        self._proc = None
        self._stopped = False
        self.output_latency = 0.0  # seconds before what we capture reaches the listener

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._watch_latency, daemon=True).start()

    def stop(self):
        self._stopped = True
        if self._proc:
            self._proc.kill()

    def position(self):
        """(samples captured so far, monotonic time the newest one arrived)."""
        with self._lock:
            return self._written, self._last_write

    def read(self, end, n=FFT_SIZE):
        """The n stereo frames ending at frame number `end`, shape (n, 2)."""
        with self._lock:
            return self._ring.take(np.arange(end - n, end), axis=0, mode="wrap")

    def _push(self, samples):
        samples = samples.reshape(-1, 2)[-len(self._ring):]
        with self._lock:
            idx = np.arange(self._written, self._written + len(samples)) % len(self._ring)
            self._ring[idx] = samples
            self._written += len(samples)
            self._last_write = time.monotonic()

    def _run(self):
        while not self._stopped:
            stream = find_spotify_stream() if self.source == "spotify" else None
            cmd = ["parec", "--raw", "--format=float32le", f"--rate={RATE}", "--channels=2",
                   "--latency-msec=15", "--client-name=vinyl-visualizer"]
            cmd.append(f"--monitor-stream={stream}" if stream is not None
                       else "--device=@DEFAULT_MONITOR@")
            try:
                self._proc = proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                                     stderr=subprocess.DEVNULL)
            except FileNotFoundError:
                print("vinyl: parec is not installed, so the record won't react to music")
                return
            self._read(proc, look_for_spotify=self.source == "spotify" and stream is None)
            proc.kill()
            proc.wait()
            time.sleep(0.5)

    def _read(self, proc, look_for_spotify):
        fd = proc.stdout.fileno()
        leftover = b""
        next_look = time.monotonic() + 2
        while True:
            if look_for_spotify and time.monotonic() > next_look:
                if find_spotify_stream() is not None:
                    return
                next_look = time.monotonic() + 2
            if not select.select([fd], [], [], 0.5)[0]:
                if proc.poll() is not None:
                    return
                continue
            data = os.read(fd, 16384)
            if not data:
                return
            data = leftover + data
            cut = len(data) - len(data) % 8  # whole stereo frames
            leftover = data[cut:]
            self._push(np.frombuffer(data[:cut], dtype=np.float32))


    def _watch_latency(self):
        while not self._stopped:
            self.output_latency = output_latency()
            time.sleep(3)  # picks up a switch between speakers and headphones


def _json(*cmd):
    try:
        return json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout
                          or "null")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def spotify_streams():
    """Spotify's playback streams (PulseAudio sink inputs), active ones first."""
    found = [s for s in _json("pactl", "-f", "json", "list", "sink-inputs") or []
             if any("spotify" in s.get("properties", {}).get(key, "").lower()
                    for key in ("application.name", "application.process.binary"))]
    return sorted(found, key=lambda s: s.get("corked", False))


def find_spotify_stream():
    """Index of Spotify's playback stream, or None."""
    streams = spotify_streams()
    return streams[0]["index"] if streams else None


def output_latency():
    """How long the output Spotify plays on takes to sound what we capture from
    it: Bluetooth adds roughly 100-300 ms, wired outputs a few ms. PipeWire
    knows (pw-dump); pactl reports 0 for Bluetooth there, so it's the fallback."""
    streams = spotify_streams()
    sinks = _json("pactl", "-f", "json", "list", "sinks") or []
    sink = next((s for s in sinks if streams and s.get("index") == streams[0].get("sink")), None)
    if sink is None:
        default = subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                                 text=True).stdout.strip() if sinks else ""
        sink = next((s for s in sinks if s.get("name") == default), None)
    if sink is None:
        return 0.0
    for node in _json("pw-dump", sink["name"]) or []:
        params = (node.get("info") or {}).get("params") or {}
        latencies = [p.get("minNs", 0) / 1e9 + p.get("minQuantum", 0) * QUANTUM
                     for p in params.get("Latency", []) if p.get("direction") == "Input"]
        if latencies:
            return max(latencies)
    return sink.get("latency", {}).get("actual", 0) / 1e6


def display_delay(output_latency, adjust=0.0):
    """How long to hold the visuals back so a hit shows just as it's heard.
    Over Bluetooth (~100-300 ms) that's most of the output latency; on wired
    output the sound is already quicker than the analysis, so it's 0."""
    return max(0.0, output_latency - CAPTURE_LATENCY - RENDER_LATENCY - LEAD + adjust)


class Analyzer:
    """Analyses the captured audio every HOP samples, in audio time, into:

    - levels: 48 log-spaced band levels, 0..1, for the spectrum ring
      (auto-gained so quiet and loud songs both read, smoothed to move calmly);
    - pulse: 0..1, jumping on hits and decaying between them, for the record.
      mode "hits" pulses on what stands out (drums, chords, accents); mode
      "notes" pulses on every note of the lead line, for songs carried by a
      melody that changes pitch without getting louder (a legato synth line).

    Both are kept for the last second and a half with timestamps, so the
    display can show them delayed to match the sound (see display_delay)."""

    BANDS = 48

    # Onsets: a region of the spectrum suddenly getting louder. Whole regions,
    # so a bass line moving between notes barely counts while a kick, a snare
    # or a piano chord does; weighted toward what's on top of the mix; and
    # judged against this song's last 1.5 s, so what keeps repeating in the
    # background fades out and what stands out pulses.
    REGION_SPLITS = (150, 500, 3000)      # Hz: lows | low-mids | mids | highs
    REGION_WEIGHT = (0.6, 0.7, 1.0, 0.2)  # treble low, so fast hi-hats don't make it twitch
    GATE_DB = -70.0                       # below this it's silence: no hits, statistics frozen
    PROMINENCE_DB = 20.0                  # regions this far below the loudest count less, down to 0

    # A hit is a local maximum (±3 hops, ±32 ms) at least 7 hops (75 ms) after
    # the last one. It shows at full size at once and decays from there.
    PRE_MAX, POST_MAX, REFRACTORY = 3, 3, 7
    WARM_UP = 1.5    # s after silence with pulses capped at half
    RELEASE = 0.12   # s, decay time constant

    # Notes mode: new pitches appearing between 300 Hz and 5 kHz (above the
    # bass), each bin against its loudest of the last two hops, loud partials
    # counting most. Every clear one is a note: above 0.8 dB and half the
    # lead's typical note, at least 6 hops (64 ms) after the last, sized
    # against that typical note, with a quicker release so notes stay apart.
    NOTE_RANGE = (300, 5000)
    NOTE_FLOOR = 0.8
    NOTE_REFRACTORY = 6
    NOTE_RELEASE = 0.08

    # Vocals first: lead vocals sit in the centre of a stereo mix, between
    # 200 Hz and 5 kHz. Each bin there counts by how centred it is (alike in
    # both channels and as loud in each, smoothed over 30 ms); when the centre
    # holds enough of that range, someone's singing. While they sing, the
    # record swells with their voice, staying up through held notes and easing
    # down when they stop, and beats still push it further on top. A mono
    # song can't be told apart, so there it stays off.
    VOICE_RANGE = (200, 5000)
    VOICE_HOLD = 0.6                         # how far the voice alone swells it; beats add the rest
    VOICE_SPAN = 18.0                        # dB below the voice's recent peak that still counts
    VOICE_ATTACK, VOICE_RELEASE = 0.05, 0.3  # s: up with a phrase, easing down after it

    def __init__(self, rate=RATE, size=FFT_SIZE, fmin=35.0, fmax=15000.0):
        freqs = np.fft.rfftfreq(size, 1 / rate)
        edges = np.geomspace(fmin, fmax, self.BANDS + 1)
        centers = np.sqrt(edges[:-1] * edges[1:])
        # Each band averages the FFT bins inside it. Low bands narrower than
        # one bin interpolate between their two neighbours instead.
        self.weights = np.zeros((self.BANDS, len(freqs)))
        for b, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
            inside = (freqs >= lo) & (freqs < hi)
            if inside.any():
                self.weights[b, inside] = 1 / inside.sum()
            else:
                i = np.searchsorted(freqs, centers[b])
                t = (centers[b] - freqs[i - 1]) / (freqs[i] - freqs[i - 1])
                self.weights[b, i - 1], self.weights[b, i] = 1 - t, t
        self.window = np.hanning(size).astype(np.float32)
        self.norm = (size / 4) ** 2  # a full-scale sine reads 0 dB
        self.tilt = 4.5 * np.log2(centers / 1000)  # lift the treble so it moves too
        region = np.digitize(centers, self.REGION_SPLITS)
        self.regions = [region == r for r in range(4)]
        # The same regions over FFT bins, for their typical (median) loudness:
        # held notes and voices are a few loud peaks with quiet gaps between,
        # drums are noise that fills the gaps, so a jump in the median is a drum
        # hit even under a singer (harmonic/percussive separation, Fitzgerald 2010).
        in_range = (freqs >= fmin) & (freqs < fmax)
        bin_region = np.digitize(freqs, self.REGION_SPLITS)
        self.region_bins = [np.flatnonzero(in_range & (bin_region == r)) for r in range(4)]
        self.percussive = True
        self.note_bins = (freqs >= self.NOTE_RANGE[0]) & (freqs < self.NOTE_RANGE[1])
        self.voice_bins = (freqs >= self.VOICE_RANGE[0]) & (freqs < self.VOICE_RANGE[1])
        self.mode = "hits"
        self.vocals_first = True

        self.levels = np.zeros(self.BANDS)
        self._ref = -40.0
        self._open = False           # silence gate
        self._opened_at = -1e9
        self._recent = np.zeros(4)   # each region's level a moment ago, dB
        self._recent_typical = np.zeros(4)  # and its typical (median) bin's
        self._mean = 0.0             # onset strength statistics over ~1.5 s
        self._var = 0.0
        self._odf = collections.deque(maxlen=16)   # (time, (hit z-score, note strength))
        self._note_prev = collections.deque(maxlen=2)
        self._notes = collections.deque(maxlen=60)  # recent notes' strengths
        self._centre = None                          # how centred each vocal-range bin is
        self._singing = 0.0                          # centred share of the vocal range
        self._singing_log = collections.deque(maxlen=1875)   # its last 20 s
        self._singing_low = 0.15                     # its usual level without a voice
        self._side = 0.0                             # side vs mid energy: is it stereo?
        self._mid = 1e-12
        self._voice_peak = None                      # the voice's recent loudest, dB
        self._swell = 0.0                            # how far the voice holds the record up
        self._swell_at = 0.0
        self._hop_count = 0
        self._last_hit = -1000
        self._env = 0.0
        self._env_at = 0.0
        self._pulses = collections.deque(maxlen=160)   # (time, pulse): 1.7 s
        self._level_log = collections.deque(maxlen=160)
        self._next = None            # sample number the next hop ends at
        self._clock = 0.0            # audio time of the newest hop

    # ---- feeding -------------------------------------------------------

    def process(self, capture, now, lookahead=False):
        """Analyses every hop captured since the last call. With `lookahead`
        (when the display is delayed anyway), hits are confirmed 3 hops later."""
        post = self.POST_MAX if lookahead else 0
        written, arrived = capture.position()
        if self._next is None or written - self._next > RATE // 4:
            self._next = written  # first call, or fell behind while hidden: start fresh
        while self._next <= written:
            hop_time = max(arrived - (written - self._next) / RATE, self._clock)
            self._hop(capture.read(self._next), hop_time, post)
            self._next += HOP
        # Paused streams stop sending data rather than sending silence, so
        # keep time moving with silent hops: the ring falls and the pulse ends.
        if now - arrived > 0.15:
            self._clock = max(self._clock, now - 0.5)
            silence = np.zeros((FFT_SIZE, 2), dtype=np.float32)
            while self._clock + HOP_TIME <= now:
                self._hop(silence, self._clock + HOP_TIME, post)

    @property
    def quiet(self):
        return float(self.levels.max()) < 0.01

    @property
    def release(self):
        return self.NOTE_RELEASE if self.mode == "notes" else self.RELEASE

    def pulse_at(self, t):
        """The pulse as it was at time t (decaying on from the hop before it)."""
        for at, pulse in reversed(self._pulses):
            if at <= t:
                return pulse * math.exp(-(t - at) / self.release)
        return 0.0

    def levels_at(self, t):
        for at, levels in reversed(self._level_log):
            if at <= t:
                return levels
        return self._level_log[0][1] if self._level_log else self.levels

    # ---- analysis ------------------------------------------------------

    def _hop(self, samples, t, post):
        self._clock = t
        self._hop_count += 1
        left = np.fft.rfft(samples[:, 0] * self.window)
        right = np.fft.rfft(samples[:, 1] * self.window)
        spec = np.abs((left + right) / 2) ** 2 / self.norm  # the mono mix
        db = 10 * np.log10(self.weights @ spec + 1e-12) + self.tilt
        self._update_levels(db)
        self._level_log.append((t, self.levels.copy()))
        audible = 10 * math.log10(spec.sum() + 1e-12) >= self.GATE_DB
        loudness, singing = self._voice(left, right) if audible else (None, 0.0)
        z = self._onset(spec, db, t, singing)
        if z is None:  # silence: the next note starts from scratch
            self._note_prev.clear()
        note = self._note(spec) if z is not None else None
        self._odf.append((t, (z, note, loudness, singing)))
        self._pick(post)

    def _update_levels(self, db):
        # The loudest band sets the scale: rise fast, fall at 6 dB/s, and never
        # boost near-silence into noise.
        peak = db.max()
        if peak > self._ref:
            self._ref += (peak - self._ref) * min(1.0, HOP_TIME * 20)
        else:
            self._ref -= min(self._ref - peak, 6 * HOP_TIME)
        self._ref = max(self._ref, -55.0)
        target = np.clip((db - self._ref + 42) / 42, 0, 1) ** 1.6
        rise, fall = 1 - math.exp(-HOP_TIME / 0.06), 1 - math.exp(-HOP_TIME / 0.3)
        self.levels += (target - self.levels) * np.where(target > self.levels, rise, fall)

    def _onset(self, spec, db, t, singing=0.0):
        """How unusual this hop's onset is for the song (a z-score), or None in silence.
        While someone's singing, drums are also found by how they fill in the
        gaps between the voice's harmonics."""
        if 10 * math.log10(spec.sum() + 1e-12) < self.GATE_DB:
            self._open = False
            return None
        power = 10 ** (db / 10)
        region = np.array([10 * math.log10(power[m].sum() + 1e-12) for m in self.regions])
        region = np.maximum(region, self._ref - 42)
        typical = np.array([10 * math.log10(np.median(spec[b]) * len(b) + 1e-12) for b in self.region_bins])
        typical = np.maximum(typical, region - 20)  # only broadband sound gets near the total
        if not self._open:  # sound again: start from here, not from the silence
            self._open, self._opened_at = True, t
            self._recent = region.copy()
            self._recent_typical = typical.copy()
        follow = 1 - math.exp(-HOP_TIME / 0.08)
        jump = np.clip(region - self._recent, 0.0, 24.0)
        self._recent += (region - self._recent) * follow
        drum = np.clip(typical - self._recent_typical, 0.0, 24.0)
        self._recent_typical += (typical - self._recent_typical) * follow
        if self.percussive and singing > 0:
            # A drum is broadband: the mids and the highs both fill in at once.
            # (Bass spill only lifts the low-mids, hi-hats only the highs.)
            hit = min(drum[2], drum[3]) * singing
            jump[2:] = np.maximum(jump[2:], hit)
        span = self.PROMINENCE_DB
        on_top = np.clip((region - region.max() + span) / span, 0.0, 1.0)
        onset = float(np.sum(jump * self.REGION_WEIGHT * on_top))
        a = 1 - math.exp(-HOP_TIME / 1.5)
        self._mean += (onset - self._mean) * a
        self._var += ((onset - self._mean) ** 2 - self._var) * a
        return (onset - self._mean) / (math.sqrt(self._var) + 0.5)

    def _note(self, spec):
        """How strongly new pitches appeared in the lead's range this hop, dB."""
        mag = np.sqrt(spec[self.note_bins])
        logmag = np.maximum(20 * np.log10(mag + 1e-9), -80.0)
        if not self._note_prev:
            self._note_prev.append(logmag)
        reference = np.max(self._note_prev, axis=0)
        self._note_prev.append(logmag)
        return float(np.maximum(logmag - reference, 0.0) @ mag / (mag.sum() + 1e-12))

    def _voice(self, left, right):
        """(the voice's loudness in dB, how much someone's singing 0..1), from
        the centre of the stereo mix."""
        scale = self.norm ** 0.5
        l, r = left[self.voice_bins] / scale, right[self.voice_bins] / scale
        mid = np.abs(l + r) / 2
        alike = 2 * np.abs(l * np.conj(r)) / (np.abs(l) ** 2 + np.abs(r) ** 2 + 1e-12)
        even = 1 - np.abs(np.abs(l) - np.abs(r)) / (np.abs(l) + np.abs(r) + 1e-12)
        centre = alike ** 8 * even ** 4
        self._centre = centre if self._centre is None else \
            self._centre + (centre - self._centre) * (1 - math.exp(-HOP_TIME / 0.03))
        a = 1 - math.exp(-HOP_TIME / 8.0)
        self._side += (float(np.sum(np.abs(l - r) ** 2)) - self._side) * a
        self._mid += (float(np.sum(np.abs(l + r) ** 2)) - self._mid) * a

        weight = mid * self._centre
        share = float((weight ** 2).sum() / ((mid ** 2).sum() + 1e-12))
        tau = 0.06 if share > self._singing else 0.4  # on quickly, off slowly
        self._singing += (share - self._singing) * (1 - math.exp(-HOP_TIME / tau))
        self._singing_log.append(self._singing)
        if self._hop_count % 10 == 0 and len(self._singing_log) > 200:
            self._singing_low = max(0.1, float(np.percentile(self._singing_log, 20)))
        stereo = self._side / self._mid > 0.005
        singing = min(1.0, max(0.0, (self._singing - self._singing_low - 0.06) / 0.10)) if stereo else 0.0
        return 10 * math.log10(float((weight ** 2).sum()) + 1e-12), singing

    def _pick(self, post):
        """Judges the hop `post` hops back, and records the pulse at its time."""
        c = len(self._odf) - 1 - post
        if c < self.PRE_MAX:
            return
        t, (z, note, loudness, singing) = self._odf[c]
        hop = self._hop_count - 1 - post
        notes = self.mode == "notes"
        value = note if notes else z
        size = 0.0
        if value is not None:
            if notes:
                typical = float(np.percentile(self._notes, 75)) if len(self._notes) >= 8 else 2.0
                over = value > max(self.NOTE_FLOOR, 0.5 * typical)
            else:
                over = value > 0.8
            gap = self.NOTE_REFRACTORY if notes else self.REFRACTORY
            if over and hop - self._last_hit >= gap:
                around = [v[1 if notes else 0] for _, v in list(self._odf)[c - self.PRE_MAX:]]
                if value >= max(a for a in around if a is not None):
                    self._last_hit = hop
                    if notes:
                        self._notes.append(value)
                        size = min(1.0, value / (1.3 * typical))
                    else:
                        size = min(1.0, (value - 0.8) / 1.2)
                    if t - self._opened_at < self.WARM_UP:
                        size = min(size, 0.5)
        self._env = max(self._env * math.exp(-(t - self._env_at) / self.release), size)
        self._env_at = t
        # The voice holds the record up; beats push it the rest of the way.
        hold = self.VOICE_HOLD * self._voice_swell(t, loudness, singing)
        self._pulses.append((t, hold + self._env * (1 - hold)))

    def _voice_swell(self, t, loudness, singing):
        """How far the voice holds the record up at time t, 0..1: its loudness
        against its recent peak, only while someone's singing."""
        dt = min(max(t - self._swell_at, 0.0), 0.1)
        self._swell_at = t
        target = 0.0
        if self.vocals_first and loudness is not None and singing > 0:
            if self._voice_peak is None or loudness > self._voice_peak:
                self._voice_peak = loudness
            level = (loudness - self._voice_peak + self.VOICE_SPAN) / self.VOICE_SPAN
            target = singing * min(1.0, max(0.0, level))
        if self._voice_peak is not None:
            self._voice_peak -= 3.0 * dt  # the peak slowly forgets, so a quieter song still reads
        tau = self.VOICE_ATTACK if target > self._swell else self.VOICE_RELEASE
        self._swell += (target - self._swell) * (1 - math.exp(-dt / tau))
        return self._swell
