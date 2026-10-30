"""The settings window. Every change applies to the overlay straight away."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QHBoxLayout, QLabel, QPushButton, QSlider, QWidget)

import settings as config


class Prefs(QDialog):
    def __init__(self, overlay):
        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle("Vinyl settings")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.overlay = overlay
        self._syncing = False
        self._sync = []  # functions that copy the current settings into the widgets

        self._form = form = QFormLayout(self)
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(10)
        self._scale("Size", "size", 120, 1200, 10, "{:.0f} px", int)
        self._scale("Spin speed", "rpm", 0, 45, 0.5, "{:.1f} rpm", float)
        self._scale("Spectrum ring", "visualizer", 0, 1, 0.05, "{:.0%}", float)
        self._scale("Beat pulse", "pulse", 0, 1, 0.05, "{:.0%}", float)
        self._scale("Sync offset", "sync_offset", -100, 400, 5, "{:+.0f} ms", int)
        self._combo("Pulse follows", "follow", [("hits", "Hits (drums, accents)"),
                                                ("notes", "Every note of the lead")])
        self._combo("Record", "style", [("picture", "Picture disc"),
                                        ("label", "Black vinyl, cover label")])
        self._combo("Track info", "track_info", [("always", "Always"), ("hover", "On hover"),
                                                 ("off", "Off")])
        self._combo("Sits", "layer", [("top", "Above windows"), ("bottom", "On the desktop")])
        self._switch("Vocals first", "vocals_first")
        self._switch("Tonearm", "tonearm")
        self._switch("Hide while Spotify is closed", "hide_when_closed")

        names = [(name, name) for name in config.presets()] + [("Custom", "Custom")]
        self._combo("Colors", "preset", names, on_change=self._pick_preset)
        self._color("Shadows", "dark")
        self._color("Highlights", "light")
        self._color("Accent", "accent")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        reset = buttons.addButton("Reset to defaults", QDialogButtonBox.ButtonRole.ResetRole)
        reset.clicked.connect(lambda: overlay.reset_settings())
        buttons.rejected.connect(self.close)
        form.addRow(buttons)
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

    def _scale(self, text, key, lo, hi, step, fmt, kind):
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, round((hi - lo) / step))
        slider.setMinimumWidth(200)
        value = QLabel()
        value.setMinimumWidth(value.fontMetrics().horizontalAdvance("+400 ms  "))
        value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        def changed(position):
            number = lo + position * step
            value.setText(fmt.format(number))
            self._change(**{key: kind(round(number, 4))})

        def show(s):
            slider.setValue(round((s[key] - lo) / step))
            value.setText(fmt.format(s[key]))

        slider.valueChanged.connect(changed)
        self._sync.append(show)
        row = QHBoxLayout()
        row.addWidget(slider, 1)
        row.addWidget(value)
        self._form.addRow(text, row)

    def _combo(self, text, key, options, on_change=None):
        combo = QComboBox()
        for value, label in options:
            combo.addItem(label, value)
        act = on_change or (lambda v: self._change(**{key: v}))
        combo.currentIndexChanged.connect(lambda i: act(combo.itemData(i)))
        self._sync.append(lambda s: combo.setCurrentIndex(max(0, combo.findData(s[key]))))
        self._form.addRow(text, combo)

    def _switch(self, text, key):
        box = QCheckBox()
        box.toggled.connect(lambda on: self._change(**{key: on}))
        self._sync.append(lambda s: box.setChecked(s[key]))
        self._form.addRow(text, box)

    def _color(self, text, key):
        button = QPushButton()
        button.setFixedSize(56, 24)

        def pick():
            color = QColorDialog.getColor(QColor(self.overlay.settings[key]), self, text)
            if color.isValid():
                self._change(**{key: color.name(), "preset": "Custom"})

        def show(s):
            button.setStyleSheet(f"background-color: {s[key]}; border: 1px solid #808080;")

        button.clicked.connect(pick)
        self._sync.append(show)
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(button)
        row.addStretch(1)
        self._form.addRow(text, holder)

    def _pick_preset(self, name):
        colors = config.presets().get(name)
        if colors and not self._syncing:
            dark, light, accent = colors
            self.overlay.change(preset=name, dark=dark, light=light, accent=accent)
