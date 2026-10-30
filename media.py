"""What's playing, shared by every platform's player watcher: the Track record
and covers, loaded and turned black and white."""

import functools
import io
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


@functools.lru_cache(maxsize=16)  # skipping back and forth stays instant
def load_cover(url):
    data = None
    if SIZE_640 in url:
        try:
            data = download(url.replace(SIZE_640, SIZE_FULL))
        except Exception:  # fall back to the size the player gave us
            pass
    if data is None:
        data = download(url)
    return cover_from_bytes(data)


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
