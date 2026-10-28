"""Audio side: records what Spotify plays from PipeWire (through parec) and
turns it into the levels that drive the record's shape."""

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


class Capture:
    """Keeps the newest samples of Spotify's own output stream, so system sounds
    don't move the record. While Spotify has no stream open (it closes it after
    a while paused) it listens to the default speakers and switches back as soon
    as the stream reappears. source="system" always uses the speakers."""

    def __init__(self, source="spotify"):
        self.source = source
        self._ring = np.zeros(RATE, dtype=np.float32)
        self._pos = 0
        self._last_write = 0.0
        self._lock = threading.Lock()
        self._proc = None
        self._stopped = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stopped = True
        if self._proc:
            self._proc.kill()

    def latest(self, n=FFT_SIZE):
        """The newest n samples; silence if nothing arrived recently (paused
        streams stop sending data instead of sending zeros)."""
        with self._lock:
            if time.monotonic() - self._last_write > 0.15:
                return np.zeros(n, dtype=np.float32)
            return self._ring.take(np.arange(self._pos - n, self._pos), mode="wrap")

    def _push(self, samples):
        samples = samples[-len(self._ring):]
        with self._lock:
            idx = np.arange(self._pos, self._pos + len(samples)) % len(self._ring)
            self._ring[idx] = samples
            self._pos = (self._pos + len(samples)) % len(self._ring)
            self._last_write = time.monotonic()

    def _run(self):
        while not self._stopped:
            stream = find_spotify_stream() if self.source == "spotify" else None
            cmd = ["parec", "--raw", "--format=float32le", f"--rate={RATE}", "--channels=1",
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
            cut = len(data) - len(data) % 4
            leftover = data[cut:]
            self._push(np.frombuffer(data[:cut], dtype=np.float32))


def find_spotify_stream():
    """Index of Spotify's playback stream (a PulseAudio sink input), or None."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"],
                             capture_output=True, text=True, timeout=2).stdout
        streams = json.loads(out or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    found = [s for s in streams
             if any("spotify" in s.get("properties", {}).get(key, "").lower()
                    for key in ("application.name", "application.process.binary"))]
    found.sort(key=lambda s: s.get("corked", False))  # an active stream first
    return found[0]["index"] if found else None


class Analyzer:
    """Turns samples into 48 log-spaced band levels, 0..1 (auto-gained so quiet
    and loud songs both read well, and smoothed so the ring moves calmly), and
    a beat pulse, 0..1, that jumps on bass hits and settles between them."""

    BANDS = 48

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
        self.bass = centers < 150
        self.levels = np.zeros(self.BANDS)
        self.pulse = 0.0
        self._ref = -40.0
        self._bass = 0.0      # bass envelope: quick to rise, quick to fall
        self._bass_avg = 0.0  # its recent average

    def update(self, samples, dt):
        spec = np.abs(np.fft.rfft(samples * self.window)) ** 2 / self.norm
        db = 10 * np.log10(self.weights @ spec + 1e-12) + self.tilt

        # The loudest band sets the scale: rise fast, fall at 6 dB/s, and never
        # boost near-silence into noise.
        peak = db.max()
        if peak > self._ref:
            self._ref += (peak - self._ref) * min(1.0, dt * 20)
        else:
            self._ref -= min(self._ref - peak, 6 * dt)
        self._ref = max(self._ref, -55.0)
        target = np.clip((db - self._ref + 42) / 42, 0, 1) ** 1.6

        rise, fall = 1 - math.exp(-dt / 0.06), 1 - math.exp(-dt / 0.3)
        self.levels += (target - self.levels) * np.where(target > self.levels, rise, fall)

        # The pulse is the bass envelope above its recent average, so kicks
        # stand out and a steady bass line doesn't keep the record swollen.
        bass = float(target[self.bass].mean())
        tau = 0.025 if bass > self._bass else 0.16
        self._bass += (bass - self._bass) * (1 - math.exp(-dt / tau))
        self._bass_avg += (bass - self._bass_avg) * (1 - math.exp(-dt / 0.6))
        self.pulse = min(1.0, max(0.0, (self._bass - self._bass_avg) / 0.3))

    @property
    def quiet(self):
        return float(self.levels.max()) < 0.01
