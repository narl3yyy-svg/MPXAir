"""Head-up display: attitude, heading, speed and altitude tapes, status text."""
import math
import time

import cairo

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .util import GPS_FIX, CairoWidget, fmt_dist  # noqa: E402

SKY_TOP = (0.13, 0.40, 0.78)
SKY_BOT = (0.45, 0.70, 0.95)
GND_TOP = (0.47, 0.33, 0.18)
GND_BOT = (0.27, 0.18, 0.09)
WHITE = (1, 1, 1)
YELLOW = (1.0, 0.82, 0.1)
GREEN = (0.3, 0.9, 0.45)
RED = (1.0, 0.3, 0.25)
ORANGE = (1.0, 0.6, 0.15)


class Hud(CairoWidget):
    __gtype_name__ = "MPXHud"

    def __init__(self, vehicle):
        super().__init__(hexpand=True, vexpand=False)
        self.v = vehicle
        self.set_min_size(320, 290)

    def _text(self, cr, x, y, text, size=12, color=WHITE, align="l", bold=True, shadow=True):
        cr.select_font_face("Sans", 0, 1 if bold else 0)
        cr.set_font_size(size)
        ext = cr.text_extents(text)
        if align == "c":
            x -= ext.width / 2 + ext.x_bearing
        elif align == "r":
            x -= ext.width + ext.x_bearing
        if shadow:
            cr.set_source_rgba(0, 0, 0, 0.6)
            cr.move_to(x + 1, y + 1)
            cr.show_text(text)
        cr.set_source_rgb(*color)
        cr.move_to(x, y)
        cr.show_text(text)
        # show_text leaves the pen at the end of the text; clear it so the next arc or
        # line doesn't start with a stray stroke from here
        cr.new_path()
        return ext.width

    def draw(self, cr, w, h):
        st = self.v.state
        cx, cy = w / 2, h / 2
        roll, pitch = st.roll, st.pitch
        ppd = h / 50.0      # pixels per degree of pitch

        # --- horizon
        cr.save()
        cr.rectangle(0, 0, w, h)
        cr.clip()
        cr.translate(cx, cy)
        cr.rotate(-math.radians(roll))
        cr.translate(0, pitch * ppd)
        big = math.hypot(w, h) * 1.5

        g = cairo.LinearGradient(0, -big / 2, 0, 0)
        g.add_color_stop_rgb(0, *SKY_TOP)
        g.add_color_stop_rgb(1, *SKY_BOT)
        cr.rectangle(-big, -big, 2 * big, big)
        cr.set_source(g)
        cr.fill()
        g = cairo.LinearGradient(0, 0, 0, big / 2)
        g.add_color_stop_rgb(0, *GND_TOP)
        g.add_color_stop_rgb(1, *GND_BOT)
        cr.rectangle(-big, 0, 2 * big, big)
        cr.set_source(g)
        cr.fill()
        cr.set_source_rgb(*WHITE)
        cr.set_line_width(2)
        cr.move_to(-big, 0)
        cr.line_to(big, 0)
        cr.stroke()
        # pitch ladder
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(10)
        cr.set_line_width(1.5)
        for p in range(-90, 95, 5):
            if p == 0:
                continue
            y = -p * ppd
            if abs(y + pitch * ppd) > h * 0.36:
                continue
            half = w * (0.12 if p % 10 == 0 else 0.06)
            cr.set_source_rgba(1, 1, 1, 0.9)
            # below the horizon the rungs are dashed (as in Mission Planner)
            cr.set_dash([6, 4] if p < 0 else [])
            cr.new_path()
            cr.move_to(-half, y)
            cr.line_to(half, y)
            cr.stroke()
            cr.set_dash([])
            if p % 10 == 0:
                for sx, al in ((-half - 6, "r"), (half + 6, "l")):
                    self._text(cr, sx, y + 4, str(abs(p)), 10, align=al, shadow=False)
        cr.restore()

        # --- roll arc
        r = min(w, h) * 0.36
        cr.save()
        cr.translate(cx, cy)
        cr.set_source_rgba(1, 1, 1, 0.9)
        cr.set_line_width(1.5)
        cr.new_path()
        cr.arc(0, 0, r, math.radians(-150), math.radians(-30))
        cr.stroke()
        for a in (-60, -45, -30, -20, -10, 0, 10, 20, 30, 45, 60):
            ang = math.radians(a - 90)
            ln = 10 if a % 30 == 0 else 6
            cr.move_to(math.cos(ang) * r, math.sin(ang) * r)
            cr.line_to(math.cos(ang) * (r + ln), math.sin(ang) * (r + ln))
            cr.stroke()
        # roll pointer
        cr.rotate(-math.radians(roll))
        cr.move_to(0, -r + 2)
        cr.line_to(-7, -r + 13)
        cr.line_to(7, -r + 13)
        cr.close_path()
        cr.set_source_rgb(*YELLOW)
        cr.fill()
        cr.restore()

        # --- aircraft symbol
        cr.set_source_rgba(0, 0, 0, 0.5)
        cr.set_line_width(6)
        self._wings(cr, cx, cy, w)
        cr.set_source_rgb(*YELLOW)
        cr.set_line_width(3.5)
        self._wings(cr, cx, cy, w)

        # --- heading tape (top)
        self._heading_tape(cr, w, st)
        # --- speed (left) and altitude (right) tapes
        spd = st.airspeed if st.vclass == "plane" and st.airspeed > 0 else st.groundspeed
        self._tape(cr, 8, h * 0.2, 58, h * 0.6, spd, 2, 10, "left", "GS" if spd == st.groundspeed else "AS")
        self._tape(cr, w - 66, h * 0.2, 58, h * 0.6, st.alt, 5, 20, "right", "ALT")
        self._vsi(cr, w - 6, h * 0.2, h * 0.6, st.climb)

        # --- status texts
        armed = st.armed
        if st.connected:
            self._banner(cr, cx, h * 0.70, "ARMED" if armed else "DISARMED", RED if armed else GREEN)
            if not st.link_ok:
                self._banner(cr, cx, h * 0.58, "NO TELEMETRY", RED)
        else:
            self._banner(cr, cx, h * 0.70, "NOT CONNECTED", (0.85, 0.85, 0.85))
        y = h - 8
        if not st.connected:
            self._text(cr, 8, y, "Connect to a vehicle (top right)", 11, (0.85, 0.85, 0.85))
            return
        self._text(cr, 8, y - 34, st.mode, 15, WHITE)
        bcol = WHITE
        if st.battery_remaining >= 0 and st.battery_remaining < 25:
            bcol = RED
        batt = f"Bat {st.voltage:.1f}V {st.current:.1f}A"
        if st.battery_remaining >= 0:
            batt += f" {st.battery_remaining}%"
        self._text(cr, 8, y - 17, batt, 11, bcol)
        gcol = GREEN if st.gps_fix >= 3 else (ORANGE if st.gps_fix == 2 else RED)
        self._text(cr, 8, y, f"{GPS_FIX.get(st.gps_fix, '?')}  Sats {st.gps_sats}  HDOP {st.gps_hdop:.1f}",
                   11, gcol)
        self._text(cr, w - 8, y - 17, f"WP {st.current_wp}  {fmt_dist(st.wp_dist)}", 11, WHITE, "r")
        lq = st.link_quality
        lcol = GREEN if lq > 90 else (ORANGE if lq > 60 else RED)
        self._text(cr, w - 8, y, f"Link {lq:.0f}%  " + time.strftime("%H:%M:%S"), 11,
                   lcol if st.connected else WHITE, "r")
        # health flags
        flags = []
        if st.connected:
            if max(st.vibe) > 30:
                flags.append(("VIBE", RED if max(st.vibe) > 60 else ORANGE))
            ekf_bad = max(st.ekf_vel_var, st.ekf_pos_h_var, st.ekf_pos_v_var, st.ekf_compass_var)
            if ekf_bad > 0.5:
                flags.append(("EKF", RED if ekf_bad > 0.8 else ORANGE))
            if st.prearm_ok is False and not st.armed:
                flags.append(("PREARM", ORANGE))
        x = w - 8
        for text, col in flags:
            x -= self._text(cr, x, y - 34, text, 11, col, "r") + 10

    def _banner(self, cr, cx, y, text, color):
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(15)
        ext = cr.text_extents(text)
        pw, ph = ext.width + 24, 24
        x0, y0 = cx - pw / 2, y - ph / 2 - 5
        r = ph / 2
        cr.new_sub_path()
        cr.arc(x0 + pw - r, y0 + r, r, -math.pi / 2, math.pi / 2)
        cr.arc(x0 + r, y0 + r, r, math.pi / 2, 3 * math.pi / 2)
        cr.close_path()
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.fill()
        self._text(cr, cx, y + 2, text, 15, color, "c", shadow=False)

    def _wings(self, cr, cx, cy, w):
        span = w * 0.16
        cr.move_to(cx - span - 20, cy)
        cr.line_to(cx - 20, cy)
        cr.line_to(cx - 10, cy + 10)
        cr.move_to(cx + span + 20, cy)
        cr.line_to(cx + 20, cy)
        cr.line_to(cx + 10, cy + 10)
        cr.move_to(cx - 2, cy)
        cr.line_to(cx + 2, cy)
        cr.stroke()

    def _heading_tape(self, cr, w, st):
        hdg = st.yaw if st.connected else 0
        th = 26
        cr.set_source_rgba(0, 0, 0, 0.35)
        cr.rectangle(w * 0.15, 0, w * 0.7, th)
        cr.fill()
        ppd = w * 0.7 / 90.0
        cr.save()
        cr.rectangle(w * 0.15, 0, w * 0.7, th)
        cr.clip()
        cr.set_line_width(1.2)
        names = {0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SW", 270: "W", 315: "NW"}
        start = int(hdg - 50) // 5 * 5
        for d in range(start, int(hdg + 55), 5):
            x = w / 2 + (d - hdg) * ppd
            dn = d % 360
            cr.set_source_rgba(1, 1, 1, 0.85)
            cr.move_to(x, th)
            cr.line_to(x, th - (8 if dn % 15 == 0 else 4))
            cr.stroke()
            if dn % 45 == 0:
                self._text(cr, x, 13, names[dn], 11, YELLOW if dn % 90 == 0 else WHITE, "c", shadow=False)
            elif dn % 15 == 0:
                self._text(cr, x, 13, str(dn), 9, WHITE, "c", bold=False, shadow=False)
        # target bearing marker
        if st.connected and st.wp_dist > 0:
            x = w / 2 + (((st.target_bearing - hdg + 540) % 360) - 180) * ppd
            cr.set_source_rgb(*GREEN)
            cr.move_to(x - 5, th)
            cr.line_to(x + 5, th)
            cr.line_to(x, th - 7)
            cr.close_path()
            cr.fill()
        cr.restore()
        # current heading box
        cr.set_source_rgba(0, 0, 0, 0.75)
        cr.rectangle(w / 2 - 22, th, 44, 18)
        cr.fill()
        self._text(cr, w / 2, th + 14, f"{hdg:03.0f}°", 12, WHITE, "c", shadow=False)

    def _tape(self, cr, x, y, tw, th, value, minor, major, side, label):
        cr.set_source_rgba(0, 0, 0, 0.3)
        cr.rectangle(x, y, tw, th)
        cr.fill()
        ppu = th / (major * 3.0)
        cr.save()
        cr.rectangle(x, y, tw, th)
        cr.clip()
        cr.set_line_width(1.2)
        mid = y + th / 2
        lo = int((value - major * 1.6) // minor) * minor
        v = lo
        while v <= value + major * 1.6:
            yy = mid - (v - value) * ppu
            is_major = abs(v % major) < 1e-6
            ln = 10 if is_major else 5
            cr.set_source_rgba(1, 1, 1, 0.85)
            if side == "left":
                cr.move_to(x + tw, yy)
                cr.line_to(x + tw - ln, yy)
            else:
                cr.move_to(x, yy)
                cr.line_to(x + ln, yy)
            cr.stroke()
            if is_major:
                tx = x + tw - 14 if side == "left" else x + 14
                self._text(cr, tx, yy + 4, f"{v:g}", 10, WHITE, "r" if side == "left" else "l",
                           bold=False, shadow=False)
            v += minor
        cr.restore()
        # value box
        bh = 24
        cr.set_source_rgba(0, 0, 0, 0.85)
        cr.rectangle(x - 2, mid - bh / 2, tw + 4, bh)
        cr.fill()
        cr.set_source_rgb(*WHITE)
        cr.set_line_width(1)
        cr.rectangle(x - 2, mid - bh / 2, tw + 4, bh)
        cr.stroke()
        self._text(cr, x + tw / 2, mid + 6, f"{value:.1f}", 14, WHITE, "c", shadow=False)
        self._text(cr, x + tw / 2, y - 5, label, 10, (0.9, 0.9, 0.9), "c")

    def _vsi(self, cr, x, y, th, climb):
        mid = y + th / 2
        c = max(-5.0, min(5.0, climb))
        hgt = c / 5.0 * th / 2
        cr.set_source_rgb(*(GREEN if c >= 0 else ORANGE))
        cr.rectangle(x - 4, mid - max(0, hgt), 4, abs(hgt))
        cr.fill()
