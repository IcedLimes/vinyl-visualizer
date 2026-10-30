"""Windows: hears what your speakers play through WASAPI loopback. Windows
doesn't readily let one app's sound be singled out, so this hears
everything, not only Spotify."""

import threading
import time
import warnings

import numpy as np

from audio import HOP, RATE, Capture


class LoopbackCapture(Capture):
    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stopped = True

    def _run(self):
        try:
            import soundcard  # here, so COM starts on this thread, apart from Qt's
        except Exception as e:
            print(f"vinyl: no audio capture ({e}), so the record won't react to music")
            return
        # It warns whenever the speakers go quiet and the stream has a gap.
        warnings.filterwarnings("ignore", category=soundcard.SoundcardRuntimeWarning)
        while not self._stopped:
            try:
                speaker = soundcard.default_speaker()
                loopback = soundcard.get_microphone(speaker.id, include_loopback=True)
                with loopback.recorder(samplerate=RATE, channels=2, blocksize=HOP * 2) as rec:
                    checked = time.monotonic()
                    while not self._stopped:
                        self._push(np.asarray(rec.record(numframes=HOP), dtype=np.float32))
                        if time.monotonic() - checked > 3:  # switched to other speakers?
                            checked = time.monotonic()
                            if soundcard.default_speaker().id != speaker.id:
                                break
            except Exception as e:
                print(f"vinyl: audio capture stopped ({e}); trying again")
                time.sleep(2)
