"""Multicopter motor layout diagram, from ArduPilot's APMotorLayout.json (as used by Mission Planner)."""
import json
import math
import os
import time

from .util import CairoWidget

DATA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "APMotorLayout.json")
_LAYOUTS = None

CW_COL = (0.20, 0.72, 0.38)     # clockwise: green
CCW_COL = (0.26, 0.55, 0.96)    # counter-clockwise: blue


def layouts():
    global _LAYOUTS
    if _LAYOUTS is None:
        try:
            with open(DATA) as fh:
                _LAYOUTS = json.load(fh).get("layouts", [])
        except (OSError, ValueError):
            _LAYOUTS = []
    return _LAYOUTS


def find_layout(frame_class, frame_type):
    """Exact match, else the class's first layout (some classes ignore FRAME_TYPE)."""
    try:
        fc, ft = int(frame_class), int(frame_type)
    except (TypeError, ValueError):
        return None
    same_class = [l for l in layouts() if l.get("Class") == fc]
    for l in same_class:
        if l.get("Type") == ft:
            return l
    return same_class[0] if same_class else None


def letter(test_order):
    return chr(64 + int(test_order)) if 1 <= int(test_order) <= 26 else str(test_order)


class MotorDiagram(CairoWidget):
    """Top-down view, front at the top. Each motor shows its output number (big),
    test letter (A, B, …) and spin direction arrow."""
    __gtype_name__ = "MPXMotorDiagram"

    def __init__(self):
        super().__init__(hexpand=True)
        self.set_min_size(320, 380)
        self.layout = None
        self.message = "Connect to a multicopter to see its motor layout."
        self.active = None          # test letter index being spun
        self.active_until = 0.0
        self.dark = True

    def set_layout(self, layout, message=None):
        self.layout = layout
        if message:
            self.message = message
        self.queue_draw()

    def flash(self, test_order, seconds):
        self.active = test_order
        self.active_until = time.monotonic() + seconds
        self.queue_draw()

    def draw(self, cr, w, h):
        fg = (0.92, 0.92, 0.94) if self.dark else (0.15, 0.15, 0.18)
        cr.select_font_face("Sans", 0, 1)
        if not self.layout or not self.layout.get("motors"):
            cr.set_source_rgb(*fg)
            cr.set_font_size(13)
            ext = cr.text_extents(self.message)
            cr.move_to(w / 2 - ext.width / 2, h / 2)
            cr.show_text(self.message)
            return
        motors = self.layout["motors"]
        top, bottom = 70, h - 34          # room for the FRONT marker and the legend
        cx, cy = w / 2, (top + bottom) / 2
        maxr = max(max(abs(m["Roll"]), abs(m["Pitch"])) for m in motors) or 0.5
        r_motor = max(20, min(36, (bottom - top) * 0.11))
        half = min(w / 2 - 20, (bottom - top) / 2) - r_motor - 12
        scale = max(20.0, half / maxr)

        # title
        cr.set_source_rgb(*fg)
        cr.set_font_size(13)
        title = f"{self.layout.get('ClassName', '')} {self.layout.get('TypeName', '')}".replace("_", " ")
        cr.move_to(10, 20)
        cr.show_text(title)
        # front marker
        cr.set_source_rgba(1.0, 0.75, 0.15, 0.95)
        cr.new_path()
        cr.move_to(cx, 8)
        cr.line_to(cx - 10, 24)
        cr.line_to(cx + 10, 24)
        cr.close_path()
        cr.fill()
        cr.set_font_size(11)
        ext = cr.text_extents("FRONT")
        cr.move_to(cx - ext.width / 2, 38)
        cr.show_text("FRONT")
        cr.new_path()

        # positions: x to the right = -Roll, forward = +Pitch (ArduPilot motor factors)
        pts = []
        seen = {}
        for m in motors:
            x = cx - m["Roll"] * scale
            y = cy - m["Pitch"] * scale
            key = (round(x), round(y))
            stack = seen.get(key, 0)
            seen[key] = stack + 1
            pts.append((m, x, y, stack))
        # body and arms
        cr.set_line_width(6)
        cr.set_source_rgba(*fg, 0.35)
        for _m, x, y, _s in pts:
            cr.move_to(cx, cy)
            cr.line_to(x, y)
            cr.stroke()
        cr.set_source_rgba(*fg, 0.55)
        cr.rectangle(cx - 16, cy - 22, 32, 44)
        cr.fill()
        cr.set_source_rgb(1.0, 0.75, 0.15)
        cr.move_to(cx, cy - 18)
        cr.line_to(cx - 7, cy - 6)
        cr.line_to(cx + 7, cy - 6)
        cr.close_path()
        cr.fill()

        active = self.active if time.monotonic() < self.active_until else None
        coax = {k for k, n in seen.items() if n > 1}
        for m, x, y, stack in pts:
            rr = r_motor
            if (round(x), round(y)) in coax:
                # coaxial pair: top motor up-left, bottom motor down-right
                d = r_motor * 0.95
                x += d if stack else -d
                y += d if stack else -d
                rr = r_motor * 0.82
            cw = m.get("Rotation", "CW").upper() == "CW"
            col = CW_COL if cw else CCW_COL
            is_active = active is not None and int(m.get("TestOrder", 0)) == int(active)
            # disc (new_path: text leaves a current point that arc() would join to)
            cr.new_path()
            cr.arc(x, y, rr, 0, 2 * math.pi)
            cr.set_source_rgba(*col, 0.85 if is_active else 0.30)
            cr.fill_preserve()
            cr.set_source_rgb(*((1, 0.8, 0.2) if is_active else col))
            cr.set_line_width(4 if is_active else 2)
            cr.stroke()
            # rotation arrow around the disc
            ra = rr + 7
            a0, a1 = math.radians(-150), math.radians(150)
            cr.set_line_width(2.5)
            cr.set_source_rgb(*col)
            cr.new_path()
            if cw:
                cr.arc(x, y, ra, a0 - math.pi / 2, a1 - math.pi / 2)
                end = a1 - math.pi / 2
                tangent = end + math.pi / 2
            else:
                cr.arc_negative(x, y, ra, a1 - math.pi / 2, a0 - math.pi / 2)
                end = a0 - math.pi / 2
                tangent = end - math.pi / 2
            cr.stroke()
            ex, ey = x + ra * math.cos(end), y + ra * math.sin(end)
            ah = 8
            cr.move_to(ex + ah * math.cos(tangent), ey + ah * math.sin(tangent))
            cr.line_to(ex + ah * 0.6 * math.cos(tangent + 2.4), ey + ah * 0.6 * math.sin(tangent + 2.4))
            cr.line_to(ex + ah * 0.6 * math.cos(tangent - 2.4), ey + ah * 0.6 * math.sin(tangent - 2.4))
            cr.close_path()
            cr.fill()
            # number and letter
            cr.set_source_rgb(*fg)
            cr.set_font_size(max(13, rr * 0.62))
            num = str(m.get("Number"))
            ext = cr.text_extents(num)
            cr.move_to(x - ext.width / 2 - ext.x_bearing, y - 2)
            cr.show_text(num)
            cr.set_font_size(max(10, rr * 0.4))
            lt = letter(m.get("TestOrder", 0))
            ext = cr.text_extents(lt)
            cr.move_to(x - ext.width / 2 - ext.x_bearing, y + rr * 0.55)
            cr.show_text(lt)

        # legend
        cr.set_font_size(11)
        ly = h - 12
        lx = 10
        for txt, col in (("Clockwise", CW_COL), ("Counter-clockwise", CCW_COL)):
            cr.set_source_rgb(*col)
            cr.new_path()
            cr.arc(lx + 6, ly - 4, 6, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgb(*fg)
            cr.move_to(lx + 16, ly)
            cr.show_text(txt)
            lx += cr.text_extents(txt).width + 36
        cr.move_to(lx, ly)
        cr.set_source_rgba(*fg, 0.7)
        cr.show_text("Big number = output (servo) number · letter = motor test button")
