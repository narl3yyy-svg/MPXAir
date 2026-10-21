"""Line graph widget for log review: zoom with the scroll wheel, drag to pan, hover to read."""
import bisect
import math

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .util import CairoWidget  # noqa: E402

PALETTE = [(0.26, 0.55, 0.96), (0.95, 0.42, 0.25), (0.30, 0.78, 0.45), (0.85, 0.35, 0.80),
           (0.98, 0.78, 0.20), (0.25, 0.80, 0.85), (0.75, 0.55, 0.35), (0.6, 0.6, 0.95)]


def nice_step(span, target=8):
    if span <= 0:
        return 1
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


class Graph(CairoWidget):
    __gtype_name__ = "MPXGraph"

    def __init__(self):
        super().__init__(hexpand=True, vexpand=True)
        self.set_min_size(300, 200)
        self.series = []          # (label, xs, ys, color)
        self.events = []          # (x, text) vertical markers (mode changes)
        self.x0 = self.x1 = None
        self.hover = None
        self.dark = True
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._db)
        drag.connect("drag-update", self._du)
        self.add_controller(drag)
        sc = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        sc.connect("scroll", self._scroll)
        self.add_controller(sc)
        mo = Gtk.EventControllerMotion()
        mo.connect("motion", lambda c, x, y: (setattr(self, "hover", x), self.queue_draw()))
        mo.connect("leave", lambda c: (setattr(self, "hover", None), self.queue_draw()))
        self.add_controller(mo)
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._click)
        self.add_controller(click)
        self._drag0 = None

    # -------------------------------------------------------------- data
    def set_series(self, series, events=()):
        self.series = [(lbl, xs, ys, PALETTE[i % len(PALETTE)]) for i, (lbl, xs, ys) in enumerate(series)]
        self.events = list(events)
        self.reset_zoom()

    def reset_zoom(self):
        xs = [s[1] for s in self.series if s[1]]
        if xs:
            self.x0 = min(x[0] for x in xs)
            self.x1 = max(x[-1] for x in xs)
            if self.x1 <= self.x0:
                self.x1 = self.x0 + 1
        else:
            self.x0, self.x1 = 0, 1
        self.queue_draw()

    # -------------------------------------------------------------- input
    def _plot_rect(self):
        w, h = self.get_width(), self.get_height()
        return 60, 12, max(10, w - 72), max(10, h - 40)

    def _db(self, g, x, y):
        self._drag0 = (x, self.x0, self.x1)

    def _du(self, g, dx, dy):
        if self._drag0 is None or self.x0 is None:
            return
        _x, a, b = self._drag0
        _px, _py, pw, _ph = self._plot_rect()
        shift = -dx / pw * (b - a)
        self.x0, self.x1 = a + shift, b + shift
        self.queue_draw()

    def _scroll(self, c, dx, dy):
        if self.x0 is None:
            return True
        px, _py, pw, _ph = self._plot_rect()
        mx = self.hover if self.hover is not None else px + pw / 2
        frac = min(1, max(0, (mx - px) / pw))
        span = self.x1 - self.x0
        f = 1.25 if dy > 0 else 0.8
        center = self.x0 + frac * span
        span *= f
        self.x0 = center - frac * span
        self.x1 = self.x0 + span
        self.queue_draw()
        return True

    def _click(self, g, n, x, y):
        if n == 2:
            self.reset_zoom()

    # -------------------------------------------------------------- draw
    def draw(self, cr, w, h):
        fg = (0.9, 0.9, 0.92) if self.dark else (0.15, 0.15, 0.18)
        grid = (1, 1, 1, 0.08) if self.dark else (0, 0, 0, 0.08)
        px, py, pw, ph = self._plot_rect()
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(10)
        if not self.series:
            cr.set_source_rgb(*fg)
            msg = "Pick values on the left to plot them"
            ext = cr.text_extents(msg)
            cr.move_to(w / 2 - ext.width / 2, h / 2)
            cr.show_text(msg)
            return
        x0, x1 = self.x0, self.x1
        # y range over visible data
        ymin, ymax = math.inf, -math.inf
        for _l, xs, ys, _c in self.series:
            i0 = max(0, bisect.bisect_left(xs, x0) - 1)
            i1 = min(len(xs), bisect.bisect_right(xs, x1) + 1)
            for v in ys[i0:i1]:
                if v == v and abs(v) != math.inf:
                    ymin = min(ymin, v)
                    ymax = max(ymax, v)
        if ymin == math.inf:
            ymin, ymax = 0, 1
        if ymax - ymin < 1e-9:
            ymin -= 1
            ymax += 1
        pad = (ymax - ymin) * 0.06
        ymin -= pad
        ymax += pad

        def X(x):
            return px + (x - x0) / (x1 - x0) * pw

        def Y(y):
            return py + ph - (y - ymin) / (ymax - ymin) * ph

        # grid + axes labels
        cr.set_line_width(1)
        st = nice_step(ymax - ymin, 6)
        v = math.ceil(ymin / st) * st
        while v <= ymax:
            yy = Y(v)
            cr.set_source_rgba(*grid)
            cr.move_to(px, yy)
            cr.line_to(px + pw, yy)
            cr.stroke()
            cr.set_source_rgb(*fg)
            txt = f"{v:g}"
            ext = cr.text_extents(txt)
            cr.move_to(px - 6 - ext.width, yy + 3)
            cr.show_text(txt)
            v += st
        st = nice_step(x1 - x0, 10)
        v = math.ceil(x0 / st) * st
        while v <= x1:
            xx = X(v)
            cr.set_source_rgba(*grid)
            cr.move_to(xx, py)
            cr.line_to(xx, py + ph)
            cr.stroke()
            cr.set_source_rgb(*fg)
            txt = f"{v:g}s" if v < 120 else f"{int(v // 60)}:{int(v % 60):02d}"
            ext = cr.text_extents(txt)
            cr.move_to(xx - ext.width / 2, py + ph + 14)
            cr.show_text(txt)
            v += st
        # events (mode changes)
        cr.save()
        cr.rectangle(px, py, pw, ph)
        cr.clip()
        for ex, text in self.events:
            if x0 <= ex <= x1:
                xx = X(ex)
                cr.set_source_rgba(1, 0.75, 0.2, 0.6)
                cr.set_dash([3, 3])
                cr.move_to(xx, py)
                cr.line_to(xx, py + ph)
                cr.stroke()
                cr.set_dash([])
                cr.move_to(xx + 3, py + 10)
                cr.show_text(str(text))
        # series (min/max per pixel column for speed)
        cr.set_line_width(1.5)
        for _l, xs, ys, col in self.series:
            if not xs:
                continue
            i0 = max(0, bisect.bisect_left(xs, x0) - 1)
            i1 = min(len(xs), bisect.bisect_right(xs, x1) + 1)
            cr.set_source_rgb(*col)
            n = i1 - i0
            if n > pw * 3:
                step = n / pw
                first = True
                k = 0
                while k < pw:
                    a = i0 + int(k * step)
                    b = i0 + int((k + 1) * step)
                    seg = [y for y in ys[a:max(b, a + 1)] if y == y]
                    if seg:
                        xx = px + k
                        lo, hi = Y(min(seg)), Y(max(seg))
                        if first:
                            cr.move_to(xx, lo)
                            first = False
                        cr.line_to(xx, lo)
                        cr.line_to(xx, hi)
                    k += 1
            else:
                cr.move_to(X(xs[i0]), Y(ys[i0]))
                for i in range(i0 + 1, i1):
                    cr.line_to(X(xs[i]), Y(ys[i]))
            cr.stroke()
        cr.restore()
        # legend
        lx = px + 8
        for lbl, _xs, _ys, col in self.series:
            cr.set_source_rgb(*col)
            cr.rectangle(lx, py + 4, 10, 10)
            cr.fill()
            cr.set_source_rgb(*fg)
            cr.move_to(lx + 14, py + 13)
            cr.show_text(lbl)
            lx += cr.text_extents(lbl).width + 30
        # hover readout
        if self.hover is not None and px <= self.hover <= px + pw:
            hx = self.hover
            xv = x0 + (hx - px) / pw * (x1 - x0)
            cr.set_source_rgba(*fg, 0.5)
            cr.move_to(hx, py)
            cr.line_to(hx, py + ph)
            cr.stroke()
            lines = [f"t = {xv:.2f} s"]
            for lbl, xs, ys, col in self.series:
                i = bisect.bisect_left(xs, xv)
                if 0 <= i < len(xs):
                    lines.append((lbl, ys[i], col))
            bx = hx + 10 if hx < px + pw - 180 else hx - 190
            by = py + 24
            cr.set_source_rgba(0.1, 0.1, 0.12, 0.85)
            cr.rectangle(bx, by, 180, 16 * len(lines) + 8)
            cr.fill()
            for k, ln in enumerate(lines):
                yy = by + 16 + k * 16
                if isinstance(ln, str):
                    cr.set_source_rgb(1, 1, 1)
                    cr.move_to(bx + 8, yy)
                    cr.show_text(ln)
                else:
                    lbl, val, col = ln
                    cr.set_source_rgb(*col)
                    cr.move_to(bx + 8, yy)
                    cr.show_text(f"{lbl}: {val:.4g}" if isinstance(val, (int, float)) else f"{lbl}: {val}")
