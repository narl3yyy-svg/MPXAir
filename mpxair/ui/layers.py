"""Map overlay layers shared by the Fly and Plan pages."""
import collections
import math
import time

from ..core.vehicle import haversine
from .mapview import circle_radius_px, draw_home, draw_marker, draw_vehicle, outline_text

FENCE_INCL_POLY, FENCE_EXCL_POLY, FENCE_INCL_CIRC, FENCE_EXCL_CIRC, FENCE_RETURN = 5001, 5002, 5003, 5004, 5000


class VehicleLayer:
    def __init__(self, vehicle, tag=lambda: False):
        self.v = vehicle
        self._tag = tag

    @property
    def tag(self):
        return self._tag()

    def draw(self, cr, view):
        st = self.v.state
        if not st.has_position:
            return
        x, y = view.to_screen(st.lat, st.lon)
        # heading / target lines
        if st.connected:
            for brg, col, ln in ((st.yaw, (1, 0.25, 0.2, 0.9), 70), (st.nav_bearing, (1, 0.8, 0.1, 0.9), 55)):
                if col[1] == 0.8 and st.wp_dist <= 0:
                    continue
                cr.set_source_rgba(*col)
                cr.set_line_width(2)
                cr.move_to(x, y)
                cr.line_to(x + math.sin(math.radians(brg)) * ln, y - math.cos(math.radians(brg)) * ln)
                cr.stroke()
        draw_vehicle(cr, x, y, st.yaw, st.vclass,
                     (0.95, 0.25, 0.2) if st.armed else (0.95, 0.55, 0.15))
        if self.tag:
            draw_tag(cr, x, y + 20, "AIR", (0.85, 0.25, 0.2) if st.armed else (0.85, 0.45, 0.12))


TRAIL_CHOICES = [(10, "10 seconds"), (20, "20 seconds"), (30, "30 seconds"), (60, "1 minute"),
                 (120, "2 minutes"), (300, "5 minutes"), (0, "The whole flight")]


class TrailLayer:
    """The flight trail. Keeps only the last `seconds` of track (0 = everything)."""

    def __init__(self, vehicle, seconds=20, maxlen=20000):
        self.v = vehicle
        self.seconds = seconds
        self.points = collections.deque(maxlen=maxlen)     # (lat, lon, monotonic time)

    def sample(self):
        st = self.v.state
        now = time.monotonic()
        if st.has_position:
            if not self.points or haversine(self.points[-1][0], self.points[-1][1], st.lat, st.lon) >= 0.5:
                self.points.append((st.lat, st.lon, now))
        if self.seconds:
            cutoff = now - self.seconds
            while self.points and self.points[0][2] < cutoff:
                self.points.popleft()

    def clear(self):
        self.points.clear()

    def draw(self, cr, view):
        n = len(self.points)
        if n < 2:
            return
        cr.set_line_width(2.5)
        cr.set_line_cap(1)
        now = time.monotonic()
        prev = None
        for la, lo, t in self.points:
            x, y = view.to_screen(la, lo)
            if prev:
                # older parts fade out when only the last few seconds are kept
                a = 0.9 if not self.seconds else 0.25 + 0.65 * max(0.0, 1 - (now - t) / self.seconds)
                cr.set_source_rgba(0.95, 0.2, 0.8, a)
                cr.move_to(*prev)
                cr.line_to(x, y)
                cr.stroke()
            prev = (x, y)


class HomeLayer:
    def __init__(self, vehicle, getter=None):
        self.v = vehicle
        self.getter = getter

    def draw(self, cr, view):
        pos = self.getter() if self.getter else None
        if pos is None:
            st = self.v.state
            if st.home_lat is None:
                return
            pos = (st.home_lat, st.home_lon)
        x, y = view.to_screen(*pos)
        draw_home(cr, x, y)


def mission_path(items, home=None):
    """Return [(lat, lon, label, item_index)] of the items that are drawn on the map."""
    pts = []
    for i, it in enumerate(items):
        if i == 0 and it.command == 16 and it.frame == 0:
            continue    # item 0 is home on ArduPilot
        if it.has_location:
            pts.append((it.x, it.y, str(i), i))
        elif it.command == 22 and home:          # takeoff: drawn at home
            pass
    return pts


class MissionLayer:
    """Draws a mission. Editable when `editor` (the plan page) is given."""

    def __init__(self, items_getter, home_getter=None, current_getter=None, editor=None):
        self.items = items_getter
        self.home = home_getter
        self.current = current_getter
        self.editor = editor

    def draw(self, cr, view):
        items = self.items()
        if not items:
            return
        home = self.home() if self.home else None
        pts = mission_path(items)
        cur = self.current() if self.current else -1
        # path line (home -> first, wps..., dashed back home if RTL)
        coords = [view.to_screen(la, lo) for la, lo, _l, _i in pts]
        if home and coords and any(it.command == 22 for it in items):
            hx, hy = view.to_screen(*home)
            coords = [(hx, hy)] + coords
        if len(coords) >= 2:
            cr.set_source_rgba(0, 0, 0, 0.4)
            cr.set_line_width(5)
            self._poly(cr, coords)
            cr.stroke()
            cr.set_source_rgba(1.0, 0.85, 0.15, 0.95)
            cr.set_line_width(2.5)
            self._poly(cr, coords)
            cr.stroke()
        if home and pts and any(it.command == 20 for it in items):
            hx, hy = view.to_screen(*home)
            x, y = coords[-1]
            cr.set_dash([6, 6])
            cr.set_source_rgba(1, 1, 1, 0.8)
            cr.set_line_width(2)
            cr.move_to(x, y)
            cr.line_to(hx, hy)
            cr.stroke()
            cr.set_dash([])
        # waypoint radius
        sel = self.editor.selected if self.editor else set()
        for (la, lo, lab, idx), (x, y) in zip(pts, coords[-len(pts):] if pts else []):
            it = items[idx]
            if it.command in (17, 18, 19, 31):
                r = circle_radius_px(view, la, abs(it.p3) or 30)
                cr.set_source_rgba(1, 1, 1, 0.6)
                cr.set_line_width(1.5)
                cr.arc(x, y, r, 0, 2 * math.pi)
                cr.stroke()
            if it.command == 21:
                col = (0.85, 0.3, 0.2)
            elif idx == cur:
                col = (0.2, 0.75, 0.35)
            elif it.command == 82:
                col = (0.55, 0.35, 0.85)
            else:
                col = (0.2, 0.5, 0.95)
            draw_marker(cr, x, y, lab, col, selected=idx in sel)
            if self.editor and view.zoom >= 15:
                cr.select_font_face("Sans", 0, 0)
                outline_text(cr, x + 14, y + 4, f"{it.z:.0f}m", size=10)
        # distances between legs, in plan mode
        if self.editor and len(coords) >= 2 and view.zoom >= 16:
            cr.select_font_face("Sans", 0, 1)
            for (a, b), (pa, pb) in zip(zip(coords, coords[1:]), zip(pts, pts[1:])):
                d = haversine(pa[0], pa[1], pb[0], pb[1])
                outline_text(cr, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2, f"{d:.0f} m",
                             (1, 0.9, 0.5), size=10, center=True)

    @staticmethod
    def _poly(cr, coords):
        cr.move_to(*coords[0])
        for c in coords[1:]:
            cr.line_to(*c)

    # editing hooks
    def hit(self, view, x, y):
        if not self.editor or not self.editor.map_editable():
            return None
        items = self.items()
        best = None
        for la, lo, _l, idx in mission_path(items):
            sx, sy = view.to_screen(la, lo)
            d = math.hypot(sx - x, sy - y)
            if d < 14 and (best is None or d < best[0]):
                best = (d, idx)
        return best[1] if best else None

    def drag(self, view, idx, lat, lon):
        self.editor.move_item(idx, lat, lon, live=True)

    def drag_end(self, view, idx):
        self.editor.move_item(idx, None, None, live=False)

    def clicked(self, view, idx, n):
        self.editor.select_item(idx)


class FenceLayer:
    def __init__(self, items_getter, editor=None):
        self.items = items_getter
        self.editor = editor

    def draw(self, cr, view):
        items = self.items() or []
        i = 0
        while i < len(items):
            it = items[i]
            if it.command in (FENCE_INCL_POLY, FENCE_EXCL_POLY):
                n = max(1, int(it.p1))
                poly = items[i:i + n]
                incl = it.command == FENCE_INCL_POLY
                pts = [view.to_screen(p.x, p.y) for p in poly]
                if pts:
                    cr.move_to(*pts[0])
                    for p in pts[1:]:
                        cr.line_to(*p)
                    cr.close_path()
                    cr.set_source_rgba(0.2, 0.8, 0.3, 0.12) if incl else cr.set_source_rgba(0.9, 0.2, 0.2, 0.18)
                    cr.fill_preserve()
                    cr.set_source_rgba(0.2, 0.85, 0.3, 0.95) if incl else cr.set_source_rgba(0.95, 0.25, 0.2, 0.95)
                    cr.set_line_width(2.5)
                    cr.stroke()
                    if self.editor:
                        for k, (x, y) in enumerate(pts):
                            cr.arc(x, y, 5, 0, 2 * math.pi)
                            cr.set_source_rgb(1, 1, 1)
                            cr.fill()
                i += n
                continue
            if it.command in (FENCE_INCL_CIRC, FENCE_EXCL_CIRC):
                x, y = view.to_screen(it.x, it.y)
                r = circle_radius_px(view, it.x, it.p1)
                incl = it.command == FENCE_INCL_CIRC
                cr.arc(x, y, r, 0, 2 * math.pi)
                cr.set_source_rgba(0.2, 0.8, 0.3, 0.12) if incl else cr.set_source_rgba(0.9, 0.2, 0.2, 0.18)
                cr.fill_preserve()
                cr.set_source_rgba(0.2, 0.85, 0.3, 0.95) if incl else cr.set_source_rgba(0.95, 0.25, 0.2, 0.95)
                cr.set_line_width(2.5)
                cr.stroke()
            elif it.command == FENCE_RETURN:
                x, y = view.to_screen(it.x, it.y)
                draw_marker(cr, x, y, "R", (0.9, 0.55, 0.1), r=9)
            i += 1

    def hit(self, view, x, y):
        if not self.editor or not self.editor.fence_editable():
            return None
        for i, it in enumerate(self.items() or []):
            sx, sy = view.to_screen(it.x, it.y)
            if math.hypot(sx - x, sy - y) < 9:
                return i
        return None

    def drag(self, view, idx, lat, lon):
        it = self.items()[idx]
        it.x, it.y = lat, lon

    def drag_end(self, view, idx):
        self.editor.fence_changed()


class RallyLayer:
    def __init__(self, items_getter, editor=None):
        self.items = items_getter
        self.editor = editor

    def draw(self, cr, view):
        for i, it in enumerate(self.items() or []):
            x, y = view.to_screen(it.x, it.y)
            draw_marker(cr, x, y, "R", (0.95, 0.6, 0.1), r=10)

    def hit(self, view, x, y):
        if not self.editor or not self.editor.rally_editable():
            return None
        for i, it in enumerate(self.items() or []):
            sx, sy = view.to_screen(it.x, it.y)
            if math.hypot(sx - x, sy - y) < 12:
                return i
        return None

    def drag(self, view, idx, lat, lon):
        it = self.items()[idx]
        it.x, it.y = lat, lon

    def drag_end(self, view, idx):
        self.editor.rally_changed()


def draw_tag(cr, x, y, text, bg):
    """Small rounded label under a map icon."""
    cr.select_font_face("Sans", 0, 1)
    cr.set_font_size(9.5)
    ext = cr.text_extents(text)
    w, h = ext.width + 10, 14
    x0, y0 = x - w / 2, y
    r = 6
    cr.new_path()
    cr.arc(x0 + w - r, y0 + r, r, -math.pi / 2, 0)
    cr.arc(x0 + w - r, y0 + h - r, r, 0, math.pi / 2)
    cr.arc(x0 + r, y0 + h - r, r, math.pi / 2, math.pi)
    cr.arc(x0 + r, y0 + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()
    cr.set_source_rgba(*bg, 0.92)
    cr.fill()
    cr.set_source_rgb(1, 1, 1)
    cr.move_to(x - ext.width / 2 - ext.x_bearing, y0 + 10.5)
    cr.show_text(text)


def draw_ground_station(cr, x, y, ok=True):
    """Ground station icon: a blue badge with an antenna mast and radio waves."""
    col = (0.12, 0.55, 0.95) if ok else (0.55, 0.6, 0.68)
    cr.new_path()
    cr.arc(x, y, 15, 0, 2 * math.pi)
    cr.set_source_rgba(0, 0, 0, 0.35)
    cr.set_line_width(5)
    cr.stroke_preserve()
    cr.set_source_rgb(*col)
    cr.fill_preserve()
    cr.set_source_rgb(1, 1, 1)
    cr.set_line_width(2)
    cr.stroke()
    # mast
    cr.set_line_width(2)
    cr.new_path()
    cr.move_to(x, y - 3)
    cr.line_to(x - 6, y + 9)
    cr.move_to(x, y - 3)
    cr.line_to(x + 6, y + 9)
    cr.move_to(x - 4, y + 5)
    cr.line_to(x + 4, y + 5)
    cr.stroke()
    cr.new_path()
    cr.arc(x, y - 4, 2.2, 0, 2 * math.pi)
    cr.fill()
    # waves
    for r in (5.5, 9):
        cr.new_path()
        cr.arc(x, y - 4, r, math.radians(-150), math.radians(-110))
        cr.stroke()
        cr.new_path()
        cr.arc(x, y - 4, r, math.radians(-70), math.radians(-30))
        cr.stroke()


class GroundLayer:
    """The ground station (its own GPS) and a line to the aircraft with the distance."""

    def __init__(self, ground, vehicle, visible=lambda: True):
        self.g = ground
        self.v = vehicle
        self.visible = visible

    def draw(self, cr, view):
        if not self.visible():
            return
        last = self.g.last_known()
        if last is None:
            return
        live = self.g.valid()
        x, y = view.to_screen(last[0], last[1])
        st = self.v.state
        if live and st.has_position and st.connected:
            vx, vy = view.to_screen(st.lat, st.lon)
            cr.new_path()
            cr.set_dash([5, 5])
            cr.set_source_rgba(0.35, 0.8, 1.0, 0.85)
            cr.set_line_width(2)
            cr.move_to(x, y)
            cr.line_to(vx, vy)
            cr.stroke()
            cr.set_dash([])
            d = haversine(self.g.lat, self.g.lon, st.lat, st.lon)
            if math.hypot(vx - x, vy - y) > 90:
                txt = f"{d / 1000:.2f} km" if d >= 1000 else f"{d:.0f} m"
                cr.select_font_face("Sans", 0, 1)
                # 40 % of the way out, nudged off the line so it doesn't sit on other markers
                L = math.hypot(vx - x, vy - y)
                nx, ny = -(vy - y) / L, (vx - x) / L
                mx, my = x + (vx - x) * 0.4 + nx * 12, y + (vy - y) * 0.4 + ny * 12
                outline_text(cr, mx, my, txt, (0.7, 0.92, 1.0), size=11, center=True)
        draw_ground_station(cr, x, y, live)
        if live:
            sats = f" · {self.g.sats} sats" if self.g.sats and self.g.source != "manual" else ""
            draw_tag(cr, x, y + 19, "GROUND" + sats, (0.10, 0.45, 0.85))
        else:
            age = last[2]
            when = f"{age:.0f} s" if age < 90 else f"{age / 60:.0f} min"
            draw_tag(cr, x, y + 19, f"GROUND · no fix · last {when} ago", (0.42, 0.46, 0.52))

    def hit(self, view, x, y):
        return None


class TargetLayer:
    """Guided target marker (shown after 'Fly to here')."""

    def __init__(self):
        self.target = None
        self.since = 0

    def set(self, lat, lon):
        self.target = (lat, lon)
        self.since = time.time()

    def draw(self, cr, view):
        if not self.target:
            return
        x, y = view.to_screen(*self.target)
        cr.set_line_width(2)
        cr.set_source_rgba(0.2, 0.9, 0.4, 0.95)
        for r in (6, 13):
            cr.arc(x, y, r, 0, 2 * math.pi)
            cr.stroke()
        cr.move_to(x - 18, y)
        cr.line_to(x + 18, y)
        cr.move_to(x, y - 18)
        cr.line_to(x, y + 18)
        cr.stroke()


class MeasureLayer:
    def __init__(self):
        self.start = None
        self.end = None

    def draw(self, cr, view):
        if not self.start:
            return
        end = self.end or view.mouse_latlon
        if not end:
            return
        x0, y0 = view.to_screen(*self.start)
        x1, y1 = view.to_screen(*end)
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.set_line_width(2)
        cr.set_dash([8, 4])
        cr.move_to(x0, y0)
        cr.line_to(x1, y1)
        cr.stroke()
        cr.set_dash([])
        from ..core.vehicle import bearing
        d = haversine(self.start[0], self.start[1], end[0], end[1])
        b = bearing(self.start[0], self.start[1], end[0], end[1])
        cr.select_font_face("Sans", 0, 1)
        outline_text(cr, (x0 + x1) / 2 + 8, (y0 + y1) / 2, f"{d:.1f} m  {b:.0f}°", size=12)

    @property
    def track_mouse(self):
        return self.start is not None and self.end is None
