"""macOS 13+: hears Spotify's own sound through ScreenCaptureKit, so other
apps and notifications don't move the record. macOS asks once for Screen &
System Audio Recording permission (only the sound is used; the screen part
is kept to a 2x2 image once a second, which is the least it allows)."""

import threading
import time

import numpy as np
import objc
from Foundation import NSObject

from audio import RATE, Capture

try:
    import CoreMedia
    import ScreenCaptureKit as SCK
    from AppKit import NSRunningApplication
    try:
        import dispatch
    except ImportError:
        import libdispatch as dispatch
except ImportError as missing:  # macOS 12 or older, or a build without them
    SCK = None
    _MISSING = missing

SPOTIFY = "com.spotify.client"
NON_INTERLEAVED = 1 << 5  # kAudioFormatFlagIsNonInterleaved


def _protocols():
    try:
        return [objc.protocolNamed("SCStreamOutput"), objc.protocolNamed("SCStreamDelegate")]
    except Exception:
        return []


class VinylAudioReceiver(NSObject, protocols=_protocols() if SCK else []):
    """Gets the stream's sample buffers on our dispatch queue."""

    def stream_didOutputSampleBuffer_ofType_(self, stream, buffer, kind):
        if kind == SCK.SCStreamOutputTypeAudio:
            try:
                self.capture._on_audio(buffer)
            except Exception as e:  # never let an exception escape into the queue
                self.capture._fail(e)

    def stream_didStopWithError_(self, stream, error):
        self.capture._stream_stopped(stream, error)


class SpotifyCapture(Capture):
    def __init__(self, source="spotify"):
        super().__init__(source)
        self._stream = None
        self._pid = None          # the Spotify process being heard
        self._failed = None       # the first error from the callback, reported once
        self._warned = False

    def start(self):
        if SCK is None:
            print(f"vinyl: no ScreenCaptureKit ({_MISSING}): the record won't react to music. "
                  "It needs macOS 13 or newer.")
            return
        self._receiver = VinylAudioReceiver.alloc().init()
        self._receiver.capture = self
        self._queue = dispatch.dispatch_queue_create(b"vinyl.audio", None)
        threading.Thread(target=self._watch, daemon=True).start()

    def stop(self):
        self._stopped = True
        self._end()

    def _watch(self):
        """Starts the stream and keeps it on Spotify: a stream hears one
        running copy of it, so a restarted Spotify needs a new stream."""
        retry_at = 0.0
        while not self._stopped:
            pid = self._spotify_pid() if self.source == "spotify" else 0
            if pid != self._pid or (pid is not None and self._stream is None):
                self._end()
                self._pid = pid
                if pid is not None and time.monotonic() >= retry_at and not self._begin():
                    self._pid = None
                    retry_at = time.monotonic() + 10  # no permission yet, most likely
            time.sleep(2)

    def _spotify_pid(self):
        apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(SPOTIFY)
        return int(apps[0].processIdentifier()) if apps and len(apps) else None

    def _begin(self):
        content, error = _shareable_content()
        if content is None:
            if not self._warned:
                self._warned = True
                print("vinyl: allow Vinyl in System Settings → Privacy & Security → Screen & "
                      f"System Audio Recording so the record can react to music ({error})")
            return False
        displays = content.displays()
        if not displays:
            return False
        if self.source == "spotify":
            apps = [a for a in content.applications() if a.bundleIdentifier() == SPOTIFY]
            if not apps:
                return False
            flt = SCK.SCContentFilter.alloc().initWithDisplay_includingApplications_exceptingWindows_(
                displays[0], apps, [])
        else:
            flt = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(displays[0], [])
        config = SCK.SCStreamConfiguration.alloc().init()
        config.setCapturesAudio_(True)
        config.setSampleRate_(RATE)
        config.setChannelCount_(2)
        config.setExcludesCurrentProcessAudio_(True)
        config.setWidth_(2)
        config.setHeight_(2)
        config.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, 1))
        config.setShowsCursor_(False)
        stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
            flt, config, self._receiver)
        for kind in (SCK.SCStreamOutputTypeAudio, SCK.SCStreamOutputTypeScreen):
            # The screen output is there only because without one, every frame
            # it drops is logged.
            ok, error = stream.addStreamOutput_type_sampleHandlerQueue_error_(
                self._receiver, kind, self._queue, None)
            if not ok:
                print(f"vinyl: couldn't listen to Spotify: {error}")
                return False
        started = threading.Event()
        problem = []

        def on_start(error):
            if error is not None:
                problem.append(error)
            started.set()

        stream.startCaptureWithCompletionHandler_(on_start)
        started.wait(10)
        if problem:
            print(f"vinyl: couldn't listen to Spotify: {problem[0]}")
            return False
        self._stream = stream
        return True

    def _end(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stopCaptureWithCompletionHandler_(lambda error: None)

    def _stream_stopped(self, stream, error):
        if stream is self._stream:
            self._stream = None  # _watch starts a new one
            print(f"vinyl: Spotify's audio stream stopped: {error}")

    def _on_audio(self, buffer):
        n = CoreMedia.CMSampleBufferGetNumSamples(buffer)
        block = CoreMedia.CMSampleBufferGetDataBuffer(buffer)
        if not n or block is None:
            return
        length = CoreMedia.CMBlockBufferGetDataLength(block)
        status, data = CoreMedia.CMBlockBufferCopyDataBytes(block, 0, length, None)
        if status:
            return
        samples = np.frombuffer(bytes(data), dtype=np.float32)
        channels = max(1, min(len(samples) // n, 8))
        samples = samples[:n * channels]
        if _interleaved(buffer):
            frames = samples.reshape(n, channels)
        else:  # the usual: each channel's samples one after the other
            frames = samples.reshape(channels, n).T
        if channels == 1:
            frames = np.repeat(frames, 2, axis=1)
        self._push(np.ascontiguousarray(frames[:, :2]))

    def _fail(self, error):
        if self._failed is None:
            self._failed = error
            print(f"vinyl: couldn't read Spotify's audio: {error!r}")


def _shareable_content():
    """What can be captured, or (None, error) without permission."""
    done = threading.Event()
    found = {}

    def handler(content, error):
        found["content"], found["error"] = content, error
        done.set()

    SCK.SCShareableContent.getShareableContentWithCompletionHandler_(handler)
    done.wait(10)
    return found.get("content"), found.get("error", "no answer")


def _interleaved(buffer):
    try:
        desc = CoreMedia.CMSampleBufferGetFormatDescription(buffer)
        asbd = CoreMedia.CMAudioFormatDescriptionGetStreamBasicDescription(desc)
        if not hasattr(asbd, "mFormatFlags"):
            asbd = asbd[0]
        return not asbd.mFormatFlags & NON_INTERLEAVED
    except Exception:
        return False  # ScreenCaptureKit's audio is non-interleaved float
