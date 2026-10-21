"""Small GTK helpers shared by every page."""
import threading
import traceback

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402


def idle(fn, *args):
    """Run fn(*args) on the GTK main loop (safe to call from any thread)."""
    def _run():
        try:
            fn(*args)
        except Exception:
            traceback.print_exc()
        return False
    GLib.idle_add(_run)


def background(work, done=None, error=None):
    """Run work() in a thread; then done(result) or error(exc) on the main loop."""
    def _run():
        try:
            res = work()
        except Exception as e:
            traceback.print_exc()
            if error:
                idle(error, e)
            return
        if done:
            idle(done, res)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


class Toaster:
    overlay = None

    @classmethod
    def show(cls, text, timeout=3):
        if cls.overlay is None:
            print("[mpxair]", text)
            return
        t = Adw.Toast.new(GLib.markup_escape_text(text))
        t.set_timeout(timeout)
        cls.overlay.add_toast(t)


def toast(text, timeout=3):
    if threading.current_thread() is threading.main_thread():
        Toaster.show(text, timeout)
    else:
        idle(Toaster.show, text, timeout)


def alert(parent, heading, body, buttons=(("ok", "OK"),), default="ok", destructive=None,
          on_response=None, extra=None):
    d = Adw.AlertDialog.new(heading, body)
    for rid, label in buttons:
        d.add_response(rid, label)
    if destructive:
        d.set_response_appearance(destructive, Adw.ResponseAppearance.DESTRUCTIVE)
    if default:
        d.set_default_response(default)
        if default != destructive and default not in ("cancel", "close"):
            d.set_response_appearance(default, Adw.ResponseAppearance.SUGGESTED)
    d.set_close_response(buttons[0][0] if len(buttons) > 1 else default)
    if extra is not None:
        d.set_extra_child(extra)
    if on_response:
        d.connect("response", lambda _d, r: on_response(r))
    d.present(parent)
    return d


def confirm(parent, heading, body, ok_label, on_ok, destructive=False):
    alert(parent, heading, body, (("cancel", "Cancel"), ("ok", ok_label)), default="ok",
          destructive="ok" if destructive else None,
          on_response=lambda r: on_ok() if r == "ok" else None)


def ask_number(parent, heading, body, value, on_ok, lo=-1e6, hi=1e6, step=1, digits=0, unit=""):
    adj = Gtk.Adjustment(value=value, lower=lo, upper=hi, step_increment=step, page_increment=step * 10)
    spin = Gtk.SpinButton(adjustment=adj, digits=digits, numeric=True, hexpand=True)
    box = Gtk.Box(spacing=8)
    box.append(spin)
    if unit:
        box.append(Gtk.Label(label=unit, css_classes=["dim-label"]))
    spin.connect("activate", lambda *_: None)
    alert(parent, heading, body, (("cancel", "Cancel"), ("ok", "OK")), default="ok",
          on_response=lambda r: on_ok(spin.get_value()) if r == "ok" else None, extra=box)
    GLib.idle_add(spin.grab_focus)


def file_dialog(parent, title, save=False, filters=(), initial_name=None, on_file=None, folder=None):
    from gi.repository import Gio
    d = Gtk.FileDialog(title=title, modal=True)
    if filters:
        store = Gio.ListStore.new(Gtk.FileFilter)
        for name, patterns in filters:
            f = Gtk.FileFilter(name=name)
            for p in patterns:
                f.add_pattern(p)
            store.append(f)
        d.set_filters(store)
        d.set_default_filter(store.get_item(0))
    if initial_name:
        d.set_initial_name(initial_name)
    if folder:
        d.set_initial_folder(Gio.File.new_for_path(folder))

    def done(dlg, res):
        try:
            f = dlg.save_finish(res) if save else dlg.open_finish(res)
        except GLib.Error:
            return
        if f and on_file:
            on_file(f.get_path())
    if save:
        d.save(parent, None, done)
    else:
        d.open(parent, None, done)


def label(text="", css=(), xalign=0.0, wrap=False, **kw):
    lb = Gtk.Label(label=text, xalign=xalign, css_classes=list(css), **kw)
    if wrap:
        lb.set_wrap(True)
    return lb


def button(text, cb=None, css=(), icon=None, tooltip=None, **kw):
    if icon and text:
        b = Gtk.Button(css_classes=list(css), **kw)
        content = Adw.ButtonContent(icon_name=icon, label=text)
        b.set_child(content)
    elif icon:
        b = Gtk.Button(icon_name=icon, css_classes=list(css), **kw)
    else:
        b = Gtk.Button(label=text, css_classes=list(css), **kw)
    if tooltip:
        b.set_tooltip_text(tooltip)
    if cb:
        b.connect("clicked", lambda *_: cb())
    return b


def clear_box(box):
    child = box.get_first_child()
    while child:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def fmt_dist(m):
    if m is None:
        return "—"
    if abs(m) >= 1000:
        return f"{m / 1000:.2f} km"
    return f"{m:.0f} m"


def fmt_time(seconds):
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    mnt, s = divmod(rem, 60)
    return f"{h}:{mnt:02d}:{s:02d}" if h else f"{mnt:02d}:{s:02d}"


GPS_FIX = {0: "No GPS", 1: "No Fix", 2: "2D Fix", 3: "3D Fix", 4: "DGPS", 5: "RTK Float",
           6: "RTK Fixed", 7: "Static", 8: "PPP"}


# ------------------------------------------------------------ cairo drawing
# PyGObject's cairo bridge (python3-gi-cairo) is not installed on stock Ubuntu,
# so custom drawing renders into a pycairo image surface that is uploaded to
# GTK as a texture. This keeps MPXAir free of extra system packages.
import cairo  # noqa: E402

gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Graphene  # noqa: E402


def cairo_texture(width, height, scale, draw):
    """Run draw(cr) on a fresh ARGB surface and return a Gdk.Texture."""
    pw, ph = max(1, int(width * scale)), max(1, int(height * scale))
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, pw, ph)
    surf.set_device_scale(scale, scale)
    cr = cairo.Context(surf)
    draw(cr)
    surf.flush()
    data = GLib.Bytes.new(bytes(surf.get_data()))
    fmt = Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED
    return Gdk.MemoryTexture.new(pw, ph, fmt, data, surf.get_stride())


def snapshot_cairo(widget, snap, draw, x=0, y=0, w=None, h=None):
    w = widget.get_width() if w is None else w
    h = widget.get_height() if h is None else h
    if w <= 0 or h <= 0:
        return
    tex = cairo_texture(w, h, widget.get_scale_factor(), draw)
    snap.append_texture(tex, Graphene.Rect().init(x, y, w, h))


class CairoWidget(Gtk.Widget):
    """A widget whose content is drawn by self.draw(cr, width, height)."""
    __gtype_name__ = "MPXCairoWidget"

    def __init__(self, **kw):
        super().__init__(**kw)
        self._min_w, self._min_h = 50, 50

    def set_min_size(self, w, h):
        self._min_w, self._min_h = w, h
        self.queue_resize()

    def do_measure(self, orientation, for_size):
        m = self._min_w if orientation == Gtk.Orientation.HORIZONTAL else self._min_h
        return m, m, -1, -1

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        snapshot_cairo(self, snap, lambda cr: self.draw(cr, w, h))

    def draw(self, cr, w, h):
        pass
