"""Draws the record with OpenGL (moderngl). The shader in disc.frag does the
drawing; this lays the overlay out and feeds the shader its state."""

import math
from dataclasses import dataclass, field
from pathlib import Path

import moderngl
import numpy as np

VERTEX = """
#version 330
in vec2 pos;
void main() { gl_Position = vec4(pos, 0.0, 1.0); }
"""
FRAGMENT = Path(__file__).with_name("disc.frag").read_text()

BLANK = 18          # gray of the empty record shown while there is no cover
GROOVE_PX = 3.5     # groove spacing on screen
RING_BLUR = np.array([1, 2, 3, 2, 1]) / 9  # softens the ring between neighbouring bands

# Tonearm geometry, in record radii from the record's centre (y up).
PIVOT = (1.17, 0.92)
ARM_LEN = 1.25
ARM_REST = math.radians(-82)            # parked beside the record
GROOVE_START, GROOVE_END = 0.95, 0.38   # stylus radius at the start and end of a track

# With the beat pulse at full strength the record swells by up to this much.
PULSE_MAX = 0.1

# How far the overlay reaches past the record's centre, in record radii: room
# for the spectrum ring and shadows at full swell, plus the tonearm on the right.
REACH = 1.33
REACH_RIGHT_ARM, REACH_TOP_ARM = 1.46, 1.33


@dataclass
class Layout:
    """Overlay size and where the record sits in it, in px (y down)."""
    width: int
    height: int
    cx: float
    cy: float
    radius: float

    @classmethod
    def for_record(cls, diameter, tonearm, text_height):
        r = diameter / 2
        right = REACH_RIGHT_ARM if tonearm else REACH
        top = REACH_TOP_ARM if tonearm else REACH
        return cls(width=math.ceil((REACH + right) * r),
                   height=math.ceil((REACH + top) * r) + text_height,
                   cx=REACH * r, cy=top * r, radius=r)

    def reach(self, x, y):
        """Distance from the record's centre, in record radii."""
        return math.hypot(x - self.cx, y - self.cy) / self.radius


@dataclass
class View:
    """Everything that changes from frame to frame."""
    rot: float = 0.0
    label: float = 0.0            # 0 picture disc → 1 black vinyl, animated between
    dark: tuple = (0.0, 0.0, 0.0)
    light: tuple = (1.0, 1.0, 1.0)
    accent: tuple = (0.85, 0.85, 0.85)
    bands: np.ndarray = field(default_factory=lambda: np.zeros(48))
    ring: float = 0.4
    arm_show: float = 1.0
    arm: float = ARM_REST
    lift: float = 1.0
    hover: float = 0.0
    scale: float = 1.0            # the record's size with the beat pulse
    playing: bool = False
    opacity: float = 1.0


def arm_angle(progress):
    """Tonearm angle that puts the stylus where the track is: the outer groove
    at the start, the inner one at the end."""
    rho = GROOVE_START + (GROOVE_END - GROOVE_START) * min(max(progress, 0.0), 1.0)
    d = math.hypot(*PIVOT)
    k = (rho * rho - d * d - ARM_LEN * ARM_LEN) / (2 * ARM_LEN)
    return math.atan2(PIVOT[1], PIVOT[0]) - math.acos(max(-1.0, min(1.0, k / d)))


class Disc:
    def __init__(self, ctx, cover=None):
        self.ctx = ctx
        self.prog = ctx.program(vertex_shader=VERTEX, fragment_shader=FRAGMENT)
        quad = ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype="f4").tobytes())
        self.vao = ctx.vertex_array(self.prog, [(quad, "2f", "pos")])
        self.covers = [self._texture(cover), self._texture(None)]
        self.front = 0          # which cover is showing, or fading in
        self.fade = 0.0         # 0 shows covers[0], 1 shows covers[1]
        self._set("u_cover_a", 0)
        self._set("u_cover_b", 1)
        self._set("u_pivot", PIVOT)
        self._set("u_arm_len", ARM_LEN)

    @property
    def settled(self):
        return self.fade == float(self.front)

    def set_cover(self, image):
        """Fades to a new cover (a grayscale PIL image), or to a blank record for None."""
        back = 1 - self.front
        self.covers[back].release()
        self.covers[back] = self._texture(image)
        self.front = back

    def update(self, dt):
        step = dt / 0.8
        self.fade = min(1.0, self.fade + step) if self.front else max(0.0, self.fade - step)

    def render(self, size, scale, layout, view):
        """size: framebuffer px; scale: framebuffer px per layout px (HiDPI)."""
        w, h = size
        radius = layout.radius * scale
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._set("u_center", (layout.cx * scale, h - layout.cy * scale))
        self._set("u_radius", radius)
        self._set("u_grooves", 2 * math.pi * radius / GROOVE_PX)
        self._set("u_rot", view.rot)
        self._set("u_fade", self.fade)
        self._set("u_label", view.label * view.label * (3 - 2 * view.label))
        self._set("u_dark", tuple(view.dark))
        self._set("u_light", tuple(view.light))
        self._set("u_accent", tuple(view.accent))
        self._set("u_bands", np.convolve(np.pad(view.bands, 2, mode="edge"), RING_BLUR, "valid"))
        self._set("u_ring", view.ring)
        self._set("u_arm_show", view.arm_show)
        self._set("u_arm", view.arm)
        self._set("u_lift", view.lift)
        self._set("u_hover", view.hover)
        self._set("u_scale", view.scale)
        self._set("u_playing", 1.0 if view.playing else 0.0)
        self._set("u_opacity", view.opacity)
        self.covers[0].use(0)
        self.covers[1].use(1)
        self.vao.render(moderngl.TRIANGLE_STRIP)

    def release(self):
        for obj in (*self.covers, self.vao, self.prog):
            obj.release()

    def _texture(self, image):
        if image is None:
            tex = self.ctx.texture((1, 1), 1, bytes([BLANK]))
        else:
            tex = self.ctx.texture(image.size, 1, image.tobytes())
            tex.build_mipmaps()
            tex.filter = (moderngl.LINEAR_MIPMAP_LINEAR, moderngl.LINEAR)
            tex.anisotropy = 8.0
        tex.repeat_x = tex.repeat_y = False
        return tex

    def _set(self, name, value):
        uniform = self.prog.get(name, None)
        if uniform is None:  # the compiler drops uniforms the shader doesn't use
            return
        if isinstance(value, np.ndarray):
            uniform.write(value.astype("f4").tobytes())
        else:
            uniform.value = value
