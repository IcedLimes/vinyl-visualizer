"""What's playing, shared by every platform's player watcher: the Track record
and covers, loaded and turned black and white."""

import functools
import io
import re
import urllib.request
from dataclasses import dataclass

from PIL import Image, ImageOps

COVER_SIZE = 1024

# Spotify's CDN serves each cover at several sizes. The players report the
# 640px one; this prefix is the full-size original.
SIZE_640 = "ab67616d0000b273"
SIZE_FULL = "ab67616d000082c1"


@dataclass(frozen=True)
class Track:
    id: str = ""          # the player's id for the track, e.g. /com/spotify/track/...
    title: str = ""
    artist: str = ""
    art_url: str = ""
    length: float = 0.0  # seconds; 0 when unknown


def load_cover(url):
    if url.startswith("file://"):
        # A copy the player wrote for this track (Electron apps do). It may
        # reuse the name for the next one, so it isn't cached.
        return cover_from_bytes(download(url))
    return _load_remote(url)


@functools.lru_cache(maxsize=16)  # skipping back and forth stays instant
def _load_remote(url):
    for bigger in larger_sizes(url):
        try:
            return cover_from_bytes(download(bigger))
        except Exception:  # not at that size: try the next, then what the player gave us
            pass
    return cover_from_bytes(download(url))


def larger_sizes(url):
    """The same cover at a larger size, where the address says how to ask:
    Spotify's full-size original, YouTube Music's art at 1200 px, a video's
    full-resolution thumbnail."""
    if SIZE_640 in url:
        return [url.replace(SIZE_640, SIZE_FULL)]
    if "googleusercontent.com" in url or "ggpht.com" in url:
        bigger = re.sub(r"=w\d+-h\d+", "=w1200-h1200", url)
        bigger = re.sub(r"=s\d+", "=s1200", bigger)
        return [bigger] if bigger != url else []
    if "ytimg.com/vi" in url:
        bigger = re.sub(r"/(hq720|sddefault|hqdefault|mqdefault|default)\.jpg", "/maxresdefault.jpg", url)
        return [bigger] if bigger != url else []
    return []


def cover_from_bytes(data):
    return to_black_and_white(Image.open(io.BytesIO(data)))


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "vinyl-visualizer"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read()


def to_black_and_white(image):
    image = ImageOps.exif_transpose(image).convert("L")
    image = ImageOps.fit(image, (COVER_SIZE, COVER_SIZE), Image.LANCZOS)
    return ImageOps.autocontrast(image, cutoff=0.5)
