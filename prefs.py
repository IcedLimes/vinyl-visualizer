"""The settings window. Every change applies to the overlay straight away."""

from gi.repository import Gdk, Gtk

import settings as config


class Prefs(Gtk.Window):
    def __init__(self, overlay):
        super().__init__(title="Vinyl settings")
        self.overlay = overlay
        self.set_resizable(False)
        self._syncing = False
        self._sync = []  # functions that copy the current settings into the widgets

        grid = Gtk.Grid(column_spacing=24, row_spacing=12, margin=20)
        self.add(grid)
        self._grid, self._row = grid, 0

        self._scale("Size", "size", 120, 1200, 10, "{:.0f} px", int)
        self._scale("Spin speed", "rpm", 0, 45, 0.5, "{:.1f} rpm", float)
        self._scale("Spectrum ring", "visualizer", 0, 1, 0.05, "{:.0%}", float)
        self._scale("Beat pulse", "pulse", 0, 1, 0.05, "{:.0%}", float)
        self._combo("Record", "style", [("picture", "Picture disc"),
                                        ("label", "Black vinyl, cover label")])
        self._combo("Track info", "track_info", [("always", "Always"), ("hover", "On hover"),
                                                 ("off", "Off")])
        self._combo("Sits", "layer", [("top", "Above windows"), ("bottom", "On the desktop")])
        self._switch("Tonearm", "tonearm")
        self._switch("Hide while Spotify is closed", "hide_when_closed")

        names = [(name, name) for name in config.presets()] + [("Custom", "Custom")]
        self._combo("Colors", "preset", names, on_change=self._pick_preset)
        self._color("Shadows", "dark")
        self._color("Highlights", "light")
        self._color("Accent", "accent")

        reset = Gtk.Button(label="Reset to defaults", halign=Gtk.Align.END, margin_top=6)
        reset.connect("clicked", lambda _: overlay.reset_settings())
        grid.attach(reset, 0, self._row, 2, 1)
        self.sync()

    def sync(self):
        """Shows the current settings, e.g. after a scroll-resize on the record."""
        self._syncing = True
        for update in self._sync:
            update(self.overlay.settings)
        self._syncing = False

    def _change(self, **values):
        if not self._syncing:
            self.overlay.change(**values)

    def _label(self, text):
        self._grid.attach(Gtk.Label(label=text, halign=Gtk.Align.START), 0, self._row, 1, 1)

    def _attach(self, widget):
        self._grid.attach(widget, 1, self._row, 1, 1)
        self._row += 1

    def _scale(self, text, key, lo, hi, step, fmt, kind):
        self._label(text)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        scale.set_size_request(200, -1)
        scale.set_draw_value(False)
        value = Gtk.Label(width_chars=8, xalign=1)  # fixed width keeps the sliders lined up

        def changed(widget):
            value.set_text(fmt.format(widget.get_value()))
            self._change(**{key: kind(widget.get_value())})

        scale.connect("value-changed", changed)
        self._sync.append(lambda s: scale.set_value(s[key]))
        row = Gtk.Box(spacing=8)
        row.pack_start(scale, True, True, 0)
        row.pack_start(value, False, False, 0)
        self._attach(row)

    def _combo(self, text, key, options, on_change=None):
        self._label(text)
        combo = Gtk.ComboBoxText()
        for value, label in options:
            combo.append(value, label)
        combo.connect("changed", lambda c: (on_change or (lambda v: self._change(**{key: v})))(
            c.get_active_id()))
        self._sync.append(lambda s: combo.set_active_id(s[key]))
        self._attach(combo)

    def _switch(self, text, key):
        self._label(text)
        switch = Gtk.Switch(halign=Gtk.Align.START)
        switch.connect("notify::active", lambda w, _: self._change(**{key: w.get_active()}))
        self._sync.append(lambda s: switch.set_active(s[key]))
        self._attach(switch)

    def _color(self, text, key):
        self._label(text)
        button = Gtk.ColorButton(halign=Gtk.Align.START, use_alpha=False)
        button.connect("color-set", lambda b: self._change(**{key: _hex(b.get_rgba()),
                                                              "preset": "Custom"}))
        self._sync.append(lambda s: button.set_rgba(_rgba(s[key])))
        self._attach(button)

    def _pick_preset(self, name):
        colors = config.presets().get(name)
        if colors and not self._syncing:
            dark, light, accent = colors
            self.overlay.change(preset=name, dark=dark, light=light, accent=accent)


def _rgba(hex_color):
    rgba = Gdk.RGBA()
    rgba.parse(hex_color)
    return rgba


def _hex(rgba):
    return "#{:02x}{:02x}{:02x}".format(*(round(c * 255) for c in (rgba.red, rgba.green, rgba.blue)))
