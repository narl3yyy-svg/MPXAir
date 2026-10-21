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


GPS_FIX = {0: "No GPS", 1: "No Fix", 2: "2D Fix", 3: "3D Fix", 4: "3D Fix + DGPS", 5: "RTK Float",
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


# ---------------------------------------------------------------- readable choices
def readable(row):
    """Make an existing Adw.ComboRow show its full value (under the title) and wrap
    long names in its list. The value text is set here rather than with
    Adw's use-subtitle, which leaves it blank while the first option is selected."""
    row.set_use_subtitle(False)
    row.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
    lf = Gtk.SignalListItemFactory()
    lf.connect("setup", lambda f, it: it.set_child(Gtk.Label(xalign=0, wrap=True, max_width_chars=48,
                                                             margin_top=2, margin_bottom=2)))
    lf.connect("bind", lambda f, it: it.get_child().set_text(it.get_item().get_string()))
    row.set_list_factory(lf)
    # the chosen value is shown in full under the title, so the row's own (cut-off) copy
    # on the right is left empty
    ef = Gtk.SignalListItemFactory()
    ef.connect("setup", lambda f, it: it.set_child(Gtk.Label()))
    row.set_factory(ef)

    def show(*_):
        item = row.get_selected_item()
        row.set_subtitle(GLib.markup_escape_text(item.get_string()) if item is not None else "")
    row.connect("notify::selected", show)
    row.connect("notify::model", show)
    show()
    return row


def combo_row(title, labels, selected=0, on_change=None, tooltip=None, tag=None):
    """An Adw.ComboRow that never cuts its value off: the chosen option is shown in
    full under the title, and the open list wraps long option names."""
    row = readable(Adw.ComboRow(title=title, model=Gtk.StringList.new(list(labels)),
                                enable_search=len(labels) > 12))
    if 0 <= selected < len(labels):
        row.set_selected(selected)
    if tag:
        row.add_suffix(Gtk.Label(label=tag, css_classes=["dim-label", "caption", "mono"]))
    if tooltip:
        row.set_tooltip_text(tooltip)
    if on_change:
        row.connect("notify::selected", lambda r, *_: on_change(r.get_selected()))
    return row


class RadioRows:
    """Radio-button rows with a title and a one-line explanation, added to an
    Adw.PreferencesGroup. options: [(id, title, explanation)]."""

    def __init__(self, grp, options, current, on_change=None):
        self.ids = [o[0] for o in options]
        self.checks = []
        self.rows = []
        self.on_change = on_change
        first = None
        for oid, title, sub in options:
            chk = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if first is None:
                first = chk
            else:
                chk.set_group(first)
            row = Adw.ActionRow(title=title, subtitle=sub or "", activatable_widget=chk)
            row.set_subtitle_lines(3)
            row.add_prefix(chk)
            chk.set_active(oid == current)
            chk.connect("toggled", self._toggled)
            grp.add(row)
            self.checks.append(chk)
            self.rows.append(row)
        if not any(c.get_active() for c in self.checks):
            self.checks[0].set_active(True)

    def _toggled(self, chk):
        if chk.get_active() and self.on_change:
            self.on_change(self.ids[self.get_selected()])

    def get_selected(self):
        for i, c in enumerate(self.checks):
            if c.get_active():
                return i
        return 0

    def get_id(self):
        return self.ids[self.get_selected()]

    def set_selected(self, i):
        self.checks[i].set_active(True)


def short_description(text, limit=220):
    """First sentence or two of a parameter description, for tooltips and subtitles."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    dot = cut.rfind(". ")
    return (cut[:dot + 1] if dot > 60 else cut.rstrip() + "…")
