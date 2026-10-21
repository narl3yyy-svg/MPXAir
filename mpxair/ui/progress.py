"""A progress pop-up with a step list, like Mission Planner's connection window.

All methods must be called on the GTK main loop (use util.idle from threads).
"""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .util import label  # noqa: E402


class ProgressDialog(Adw.Dialog):
    def __init__(self, title, on_cancel=None, cancel_label="Cancel"):
        super().__init__(title=title, content_width=460, follows_content_size=True)
        self.on_cancel = on_cancel
        self.finished = False
        self._steps = []
        self._closing = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=24, margin_end=24,
                      margin_top=24, margin_bottom=20)
        top = Gtk.Box(spacing=14)
        self.spinner = Adw.Spinner(width_request=32, height_request=32)
        self.icon = Gtk.Image(pixel_size=32, visible=False)
        top.append(self.spinner)
        top.append(self.icon)
        self.headline = label(title, ["title-3"], wrap=True, hexpand=True)
        top.append(self.headline)
        box.append(top)
        self.detail = label("", ["dim-label"], wrap=True)
        box.append(self.detail)
        self.bar = Gtk.ProgressBar(show_text=True, text="")
        box.append(self.bar)
        self.steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=4)
        box.append(self.steps_box)
        self.button = Gtk.Button(label=cancel_label, halign=Gtk.Align.END, margin_top=6, css_classes=["pill"])
        self.button.connect("clicked", self._button)
        box.append(self.button)
        self.set_child(box)
        self.set_can_close(True)
        self.connect("closed", lambda *_: self._cancel_timer())
        self._pulse_id = GLib.timeout_add(120, self._pulse)
        self._pulsing = True

    # -------------------------------------------------------------- updates
    def _pulse(self):
        if self._pulsing and not self.finished:
            self.bar.pulse()
            return True
        self._pulse_id = None
        return False

    def status(self, headline=None, detail=None):
        if headline is not None:
            self.headline.set_text(headline)
        if detail is not None:
            self.detail.set_text(detail)

    def progress(self, fraction, text=None):
        """fraction None = busy (pulsing)."""
        if fraction is None:
            if not self._pulsing:
                self._pulsing = True
                self._pulse_id = GLib.timeout_add(120, self._pulse)
            self.bar.set_text(text or "")
            return
        self._pulsing = False
        self.bar.set_fraction(max(0.0, min(1.0, fraction)))
        self.bar.set_text(text if text is not None else f"{fraction * 100:.0f} %")

    def step(self, text, state="run"):
        """Add a step (state: run / ok / warn / fail). Returns a handle for update_step."""
        row = Gtk.Box(spacing=8)
        img = Gtk.Image(pixel_size=16)
        lb = label(text, wrap=True, hexpand=True)
        row.append(img)
        row.append(lb)
        self.steps_box.append(row)
        h = (row, img, lb)
        self._steps.append(h)
        self.update_step(h, text, state)
        return h

    def update_step(self, h, text=None, state=None):
        _row, img, lb = h
        if text is not None:
            lb.set_text(text)
        if state is not None:
            icon, css = {"run": ("media-record-symbolic", "dim-label"),
                         "ok": ("object-select-symbolic", "success"),
                         "warn": ("dialog-warning-symbolic", "warning"),
                         "fail": ("dialog-error-symbolic", "error")}[state]
            img.set_from_icon_name(icon)
            img.set_css_classes([css])

    def set_button(self, text, cancel=True):
        self.button.set_label(text)
        if not cancel:
            self.on_cancel = None

    def finish(self, headline, detail="", ok=True, close_after=2.5):
        self.finished = True
        self._pulsing = False
        if ok:
            self.bar.set_fraction(1.0)
            self.bar.set_text("Done")
        self.spinner.set_visible(False)
        self.icon.set_visible(True)
        self.icon.set_from_icon_name("emblem-ok-symbolic" if ok else "dialog-error-symbolic")
        self.icon.set_css_classes(["success" if ok else "error"])
        self.headline.set_text(headline)
        self.detail.set_text(detail)
        self.button.set_label("Close")
        self.button.add_css_class("suggested-action")
        self.on_cancel = None
        if ok and close_after:
            self._closing = GLib.timeout_add(int(close_after * 1000), self._auto_close)

    def _auto_close(self):
        self._closing = None
        self.close()
        return False

    def _cancel_timer(self):
        if self._closing:
            GLib.source_remove(self._closing)
            self._closing = None

    def _button(self, *_):
        cb = self.on_cancel
        self.on_cancel = None
        if cb and not self.finished:
            cb()
        self.close()
