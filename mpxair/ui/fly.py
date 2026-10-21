"""Fly page (Mission Planner's 'Flight Data' view)."""
import math
import os
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from ..core import modes  # noqa: E402
from ..core.ground import laptop_power, tracker_angles  # noqa: E402
from ..maps import tiles  # noqa: E402
from .hud import Hud  # noqa: E402
from .layers import (TRAIL_CHOICES, FenceLayer, GroundLayer, HomeLayer, MeasureLayer,  # noqa: E402
                     MissionLayer, RallyLayer, TargetLayer, TrailLayer, VehicleLayer)
from .mapview import MapView  # noqa: E402
from ..core.vehicle import haversine  # noqa: E402
from .util import (GPS_FIX, ask_number, background, button, confirm, fmt_dist, fmt_time,  # noqa: E402
                   label, toast)

# name: (title, unit, getter)
def _g(name, default=0.0):
    return lambda s: getattr(s, name, default) or 0.0


# name: (title, unit, getter, section, decimals)
QUICK_FIELDS = {
    # position and height
    "alt": ("Altitude", "m", lambda s: s.alt, "Height and position", 1),
    "alt_msl": ("Altitude (sea level)", "m", lambda s: s.alt_msl, "Height and position", 1),
    "terrain_height": ("Terrain height", "m", lambda s: s.terrain_height or 0, "Height and position", 1),
    "rangefinder": ("Rangefinder", "m", lambda s: s.rangefinder or 0, "Height and position", 2),
    "alt_error": ("Altitude error", "m", lambda s: s.alt_error, "Height and position", 1),
    "lat": ("Latitude", "°", lambda s: s.lat, "Height and position", 6),
    "lon": ("Longitude", "°", lambda s: s.lon, "Height and position", 6),
    # speed and attitude
    "groundspeed": ("Ground speed", "m/s", lambda s: s.groundspeed, "Speed and attitude", 1),
    "groundspeed_kmh": ("Ground speed", "km/h", lambda s: s.groundspeed * 3.6, "Speed and attitude", 0),
    "airspeed": ("Air speed", "m/s", lambda s: s.airspeed, "Speed and attitude", 1),
    "climb": ("Vertical speed", "m/s", lambda s: s.climb, "Speed and attitude", 1),
    "yaw": ("Heading", "°", lambda s: s.yaw, "Speed and attitude", 0),
    "roll": ("Roll", "°", lambda s: s.roll, "Speed and attitude", 1),
    "pitch": ("Pitch", "°", lambda s: s.pitch, "Speed and attitude", 1),
    "throttle": ("Throttle", "%", lambda s: s.throttle, "Speed and attitude", 0),
    # navigation
    "dist_home": ("Distance to home", "m", lambda s: s.dist_home, "Navigation", 0),
    "bearing_home": ("Bearing to home", "°", lambda s: s.bearing_home, "Navigation", 0),
    "wp_dist": ("Distance to waypoint", "m", lambda s: s.wp_dist, "Navigation", 0),
    "current_wp": ("Current waypoint", "", lambda s: s.current_wp, "Navigation", 0),
    "nav_bearing": ("Target bearing", "°", lambda s: s.target_bearing, "Navigation", 0),
    "xtrack_error": ("Cross-track error", "m", lambda s: s.xtrack_error, "Navigation", 1),
    "dist_traveled": ("Distance flown", "m", _g("dist_traveled"), "Navigation", 0),
    "flight_time": ("Flight time", "", lambda s: s.flight_time, "Navigation", 0),
    # battery
    "voltage": ("Battery voltage", "V", lambda s: s.voltage, "Battery", 2),
    "current": ("Battery current", "A", lambda s: s.current, "Battery", 1),
    "battery_remaining": ("Battery left", "%", lambda s: s.battery_remaining, "Battery", 0),
    "battery_used_mah": ("Battery used", "mAh", lambda s: s.battery_used_mah, "Battery", 0),
    "power": ("Power", "W", lambda s: s.voltage * s.current, "Battery", 0),
    # GPS and health
    "gps_sats": ("Satellites", "", lambda s: s.gps_sats, "GPS and health", 0),
    "gps_hdop": ("GPS HDOP", "", lambda s: s.gps_hdop, "GPS and health", 2),
    "vibe_max": ("Vibration (worst axis)", "m/s²", lambda s: max(s.vibe), "GPS and health", 1),
    "ekf_worst": ("EKF variance (worst)", "", lambda s: max(s.ekf_vel_var, s.ekf_pos_h_var, s.ekf_pos_v_var,
                                                            s.ekf_compass_var), "GPS and health", 2),
    "cpu_load": ("Autopilot CPU load", "%", lambda s: s.load, "GPS and health", 0),
    "wind_speed": ("Wind speed", "m/s", lambda s: s.wind_speed, "GPS and health", 1),
    "wind_dir": ("Wind direction", "°", lambda s: s.wind_dir, "GPS and health", 0),
    # link
    "link_quality": ("Link quality", "%", lambda s: s.link_quality, "Telemetry link", 0),
    "rssi": ("Radio RSSI (ground)", "", lambda s: s.rssi, "Telemetry link", 0),
    "remrssi": ("Radio RSSI (aircraft)", "", lambda s: s.remrssi, "Telemetry link", 0),
    "fade_margin": ("Fade margin (worst)", "", lambda s: min(s.rssi - s.noise, s.remrssi - s.remnoise),
                    "Telemetry link", 0),
    "rc_rssi": ("RC receiver RSSI", "", lambda s: s.rc_rssi, "Telemetry link", 0),
    "packets_lost": ("Packets lost", "", lambda s: s.packets_lost, "Telemetry link", 0),
    # ground station
    "dist_ground": ("Distance from ground", "m", _g("dist_ground"), "Ground station", 0),
    "bearing_ground": ("Bearing from ground", "°", _g("bearing_ground"), "Ground station", 0),
    "elev_ground": ("Elevation from ground", "°", _g("elev_ground"), "Ground station", 1),
    "ground_sats": ("Ground GPS satellites", "", _g("ground_sats", 0), "Ground station", 0),
}
DEFAULT_TILES = ["alt", "groundspeed", "dist_home", "climb", "yaw", "voltage", "wp_dist", "gps_sats"]
MIN_TILES, MAX_TILES = 2, 16


def fmt_quick(name, val):
    if name == "flight_time":
        return fmt_time(val)
    dec = QUICK_FIELDS[name][4]
    if name in ("dist_home", "wp_dist", "dist_ground", "dist_traveled") and abs(val) >= 1000:
        return f"{val / 1000:.2f}k"
    return f"{val:.{dec}f}"


class StatTile(Gtk.Button):
    def __init__(self, page, index, field):
        super().__init__(css_classes=["flat", "stat-tile"], hexpand=True)
        self.page = page
        self.index = index
        self.field = field
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.title = label("", ["stat-title"])
        row = Gtk.Box(spacing=4)
        self.value = label("—", ["stat-value"])
        self.unit = label("", ["stat-unit"], valign=Gtk.Align.END)
        self.unit.set_margin_bottom(5)
        row.append(self.value)
        row.append(self.unit)
        box.append(self.title)
        box.append(row)
        self.set_child(box)
        self.set_tooltip_text("Click to choose what this tile shows")
        self.connect("clicked", self._choose)
        self.set_field(field)

    def set_field(self, field):
        self.field = field if field in QUICK_FIELDS else "alt"
        t, unit = QUICK_FIELDS[self.field][:2]
        self.title.set_text(t.upper())
        self.unit.set_text(unit)

    def update(self, st):
        g = QUICK_FIELDS[self.field][2]
        try:
            self.value.set_text(fmt_quick(self.field, g(st)) if st.connected else "—")
        except Exception:
            self.value.set_text("—")

    def _choose(self, *_):
        pop = Gtk.Popover()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sw = Gtk.ScrolledWindow(propagate_natural_height=True, max_content_height=420,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        lb = Gtk.ListBox(css_classes=["navigation-sidebar"])
        section = None
        for name, (t, unit, _g, sec, _d) in QUICK_FIELDS.items():
            if sec != section:
                section = sec
                hdr = Gtk.ListBoxRow(selectable=False, activatable=False)
                hdr.set_child(label(sec.upper(), ["panel-header"], margin_start=6, margin_top=6))
                lb.append(hdr)
            r = Gtk.ListBoxRow()
            r.set_child(label(f"{t}" + (f" ({unit})" if unit else ""), margin_start=14, margin_end=6))
            r.name = name
            lb.append(r)
            if name == self.field:
                lb.select_row(r)

        def picked(_lb, row):
            if getattr(row, "name", None):
                self.set_field(row.name)
                self.page.save_quick()
                pop.popdown()
        lb.connect("row-activated", picked)
        sw.set_child(lb)
        outer.append(sw)
        rm = Gtk.Button(label="Remove this tile", css_classes=["flat", "destructive-action"])
        rm.set_sensitive(len(self.page.tiles) > MIN_TILES)
        rm.connect("clicked", lambda *_: (pop.popdown(), GLib.idle_add(self.page.remove_tile, self)))
        outer.append(rm)
        pop.set_child(outer)
        pop.set_parent(self)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()


class FlyPage(Gtk.Box):
    def __init__(self, shared):
        super().__init__()
        self.shared = shared
        self.v = shared.vehicle
        self.settings = shared.settings
        self.follow_vehicle = True
        self.show_ground = bool(self.settings.get("show_ground", True))
        self.keep_both = False
        self._last_center = 0

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, shrink_start_child=False,
                          resize_start_child=False)
        self.append(paned)

        # ---------------------------------------------------------- left side
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["side-panel"])
        left.set_size_request(400, -1)
        self.hud = Hud(self.v)
        left.append(self.hud)

        self.tabs = Adw.ViewStack(vexpand=True)
        sw = Adw.ViewSwitcher(stack=self.tabs, policy=Adw.ViewSwitcherPolicy.NARROW,
                              margin_top=6, margin_bottom=4, margin_start=6, margin_end=6)
        left.append(sw)
        left.append(self.tabs)
        self.tabs.add_titled_with_icon(self._quick_tab(), "quick", "Quick", "view-grid-symbolic")
        self.tabs.add_titled_with_icon(self._actions_tab(), "actions", "Actions", "media-playback-start-symbolic")
        self.tabs.add_titled_with_icon(self._status_tab(), "airground", "Status", "emblem-ok-symbolic")
        self.tabs.add_titled_with_icon(self._messages_tab(), "messages", "Messages", "chat-bubbles-text-symbolic")
        self.tabs.add_titled_with_icon(self._rc_tab(), "rc", "RC/Servo", "input-gaming-symbolic")
        paned.set_start_child(left)

        # ---------------------------------------------------------- map side
        self.map = MapView(shared.tex_cache, shared.settings)
        self.trail = TrailLayer(self.v, int(self.settings.get("trail_seconds", 20)))
        self.target = TargetLayer()
        self.measure_layer = MeasureLayer()
        self.map.layers = [
            FenceLayer(lambda: shared.fence),
            RallyLayer(lambda: shared.rally),
            MissionLayer(lambda: shared.mission, self._home_pos, lambda: self.v.state.current_wp),
            HomeLayer(self.v),
            self.trail,
            self.target,
            GroundLayer(shared.ground, self.v, lambda: self.show_ground),
            VehicleLayer(self.v, tag=lambda: self.show_ground and shared.ground.valid()),
            self.measure_layer,
        ]
        self.map.on_context = self._context_menu
        self.map.on_click = self._map_click
        self.map.on_moved = self._map_moved
        overlay = Gtk.Overlay()
        overlay.set_child(self.map)
        overlay.add_overlay(self._map_chips())
        overlay.add_overlay(self._map_buttons())
        self.attrib = label("", ["map-attrib"], halign=Gtk.Align.END, valign=Gtk.Align.END)
        overlay.add_overlay(self.attrib)
        self.coord = label("", ["map-attrib", "mono"], halign=Gtk.Align.START, valign=Gtk.Align.END)
        overlay.add_overlay(self.coord)
        overlay.add_overlay(self._gps_card())
        overlay.add_overlay(self._replay_bar())
        self.map.on_hover = lambda la, lo: self.coord.set_text(f" {la:.6f}, {lo:.6f} ")
        paned.set_end_child(overlay)
        paned.set_position(420)
        self._update_attrib()

        shared.hub.on("vehicle-statustext", self._add_message)
        shared.hub.on("vehicle-connected", lambda *_: self._on_connect())
        shared.hub.on("mission-changed", lambda *_: (self._fill_wp_list(), self.map.queue_draw()))
        shared.hub.on("map-provider", lambda pid: (self.map.set_provider(pid), self._update_attrib()))
        shared.hub.on("vehicle-mode", lambda *_: self._fill_modes())

        GLib.timeout_add(100, self._tick)
        self._n = 0

    # ------------------------------------------------------------- quick tab
    def _quick_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=8, margin_end=8,
                      margin_top=4, margin_bottom=8)
        self.quick_grid = Gtk.Grid(column_spacing=6, row_spacing=6, column_homogeneous=True)
        box.append(self.quick_grid)
        self.add_tile_btn = button("Add a tile", self.add_tile, ["flat"], icon="list-add-symbolic",
                                   tooltip="Show another value (click any tile to change what it shows)")
        box.append(self.add_tile_btn)
        fields = [f for f in (self.settings.get("quick_fields") or []) if f in QUICK_FIELDS]
        if len(fields) < MIN_TILES:
            fields = list(DEFAULT_TILES)
        self.tiles = []
        self._build_tiles(fields[:MAX_TILES])
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sc.set_child(box)
        return sc

    def _build_tiles(self, fields):
        child = self.quick_grid.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.quick_grid.remove(child)
            child = nxt
        self.tiles = []
        for i, f in enumerate(fields):
            t = StatTile(self, i, f)
            self.tiles.append(t)
            self.quick_grid.attach(t, i % 2, i // 2, 1, 1)
        self.add_tile_btn.set_sensitive(len(fields) < MAX_TILES)

    def add_tile(self):
        fields = [t.field for t in self.tiles]
        if len(fields) >= MAX_TILES:
            return
        unused = [f for f in QUICK_FIELDS if f not in fields]
        fields.append(unused[0] if unused else "alt")
        self._build_tiles(fields)
        self.save_quick()
        toast("Tile added — click it to choose what it shows")

    def remove_tile(self, tile):
        fields = [t.field for t in self.tiles if t is not tile]
        if len(fields) >= MIN_TILES:
            self._build_tiles(fields)
            self.save_quick()
        return False

    def save_quick(self):
        self.settings.set("quick_fields", [t.field for t in self.tiles])

    # ----------------------------------------------------------- actions tab
    def _actions_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=10,
                      margin_end=10, margin_top=6, margin_bottom=10)
        row = Gtk.Box(spacing=8, homogeneous=True)
        self.arm_btn = button("Arm", self._arm, ["big-action", "destructive-action"],
                              icon="system-shutdown-symbolic")
        row.append(self.arm_btn)
        row.append(button("Takeoff", self._takeoff, ["big-action"], icon="go-up-symbolic"))
        box.append(row)

        self.quick_modes = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4,
                                       min_children_per_line=3, homogeneous=True, column_spacing=6,
                                       row_spacing=6)
        box.append(self.quick_modes)

        box.append(label("FLIGHT MODE", ["panel-header"]))
        mrow = Gtk.Box(spacing=6)
        self.mode_model = Gtk.StringList()
        self.mode_dd = Gtk.DropDown(model=self.mode_model, hexpand=True)
        mrow.append(self.mode_dd)
        mrow.append(button("Set Mode", self._set_mode_dd, ["suggested-action"]))
        box.append(mrow)

        box.append(label("MISSION", ["panel-header"]))
        wrow = Gtk.Box(spacing=6)
        self.wp_model = Gtk.StringList()
        self.wp_dd = Gtk.DropDown(model=self.wp_model, hexpand=True)
        wrow.append(self.wp_dd)
        wrow.append(button("Set WP", self._set_wp))
        box.append(wrow)
        mrow2 = Gtk.Box(spacing=6, homogeneous=True)
        mrow2.append(button("Start Mission", self._start_mission))
        mrow2.append(button("Restart Mission", self._restart_mission))
        box.append(mrow2)

        box.append(label("GUIDED CHANGES", ["panel-header"]))
        g = Gtk.Grid(column_spacing=6, row_spacing=6)
        self.speed_spin = Gtk.SpinButton.new_with_range(0.5, 60, 0.5)
        self.speed_spin.set_value(5)
        self.alt_spin = Gtk.SpinButton.new_with_range(1, 2000, 1)
        self.alt_spin.set_value(self.settings.get("default_alt", 50))
        g.attach(label("Speed (m/s)"), 0, 0, 1, 1)
        g.attach(self.speed_spin, 1, 0, 1, 1)
        g.attach(button("Change Speed", self._change_speed), 2, 0, 1, 1)
        g.attach(label("Altitude (m)"), 0, 1, 1, 1)
        g.attach(self.alt_spin, 1, 1, 1, 1)
        g.attach(button("Change Alt", self._change_alt), 2, 1, 1, 1)
        box.append(g)

        box.append(label("VEHICLE", ["panel-header"]))
        arow = Gtk.Box(spacing=6)
        self.action_model = Gtk.StringList.new([a[0] for a in self._do_actions()])
        self.action_dd = Gtk.DropDown(model=self.action_model, hexpand=True)
        arow.append(self.action_dd)
        arow.append(button("Do Action", self._do_action))
        box.append(arow)
        brow = Gtk.Box(spacing=6, homogeneous=True)
        brow.append(button("Set Home Here", self._set_home_current, tooltip="Make the vehicle's current position home"))
        brow.append(button("Clear Track", lambda: (self.trail.clear(), self.map.queue_draw()),
                           tooltip="Erase the flight trail drawn on the map"))
        box.append(brow)
        secs = [c[0] for c in TRAIL_CHOICES]
        cur = int(self.settings.get("trail_seconds", 20))
        trow = Gtk.Box(spacing=6)
        trow.append(label("Trail on map keeps", hexpand=True))
        self.trail_dd = Gtk.DropDown(model=Gtk.StringList.new([c[1] for c in TRAIL_CHOICES]),
                                     tooltip_text="How much of the flight trail stays on the map. "
                                                  "Older track is cleared automatically.")
        self.trail_dd.set_selected(secs.index(cur) if cur in secs else 1)
        self.trail_dd.connect("notify::selected", lambda d, *_: self.set_trail_seconds(secs[d.get_selected()]))
        trow.append(self.trail_dd)
        box.append(trow)

        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sc.set_child(box)
        self._fill_modes()
        self._fill_wp_list()
        return sc

    def _do_actions(self):
        return [
            ("Calibrate gyro (vehicle still)", lambda: self.v.preflight_calibration(gyro=1)),
            ("Calibrate barometer", lambda: self.v.preflight_calibration(baro=1)),
            ("Reboot autopilot", lambda: self.v.reboot()),
            ("Trigger camera", lambda: self.v.command(203, 0, 0, 0, 0, 1, 0, 0)),
            ("Loiter (unlimited)", lambda: self.v.set_mode("LOITER")),
            ("Return to launch", lambda: self.v.rtl()),
            ("Land", lambda: self.v.land()),
            ("Request home position", lambda: (self.v.request_message(242), (True, "sent"))[1]),
            ("Re-request telemetry streams", lambda: (self.v.request_streams(), (True, "sent"))[1]),
        ]

    def _fill_modes(self):
        vc = self.v.state.vclass
        names = list(modes.COMMON_MODES.get(vc, []))
        all_modes = [n for n in modes.mode_table(vc).values() if n not in names]
        names += all_modes
        cur = self.mode_dd.get_selected_item().get_string() if self.mode_dd.get_selected_item() else None
        self.mode_model.splice(0, self.mode_model.get_n_items(), names)
        if cur in names:
            self.mode_dd.set_selected(names.index(cur))
        # quick buttons
        child = self.quick_modes.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.quick_modes.remove(child)
            child = nxt
        quick = {"copter": ["LOITER", "ALT_HOLD", "AUTO", "GUIDED", "RTL", "LAND", "BRAKE", "STABILIZE"],
                 "plane": ["AUTO", "RTL", "LOITER", "GUIDED", "FBWA", "MANUAL", "CRUISE", "QRTL"],
                 "rover": ["AUTO", "HOLD", "RTL", "GUIDED", "MANUAL", "LOITER", "SMART_RTL", "ACRO"],
                 "sub": ["MANUAL", "STABILIZE", "ALT_HOLD", "POSHOLD", "AUTO", "SURFACE"],
                 "tracker": ["AUTO", "MANUAL", "STOP", "SCAN"]}.get(vc, [])
        for m in quick:
            b = button(m.replace("_", " ").title().replace("Rtl", "RTL"), lambda m=m: self._set_mode(m))
            if m == self.v.state.mode:
                b.add_css_class("suggested-action")
            self.quick_modes.append(b)

    def _fill_wp_list(self):
        items = self.shared.mission
        names = ["Home (0)"] if items else ["(no mission)"]
        for i, it in enumerate(items[1:], 1):
            names.append(f"{i}  {it.name.replace('_', ' ').title()}")
        self.wp_model.splice(0, self.wp_model.get_n_items(), names)
        self.wp_dd.set_selected(min(1, len(names) - 1))

    # ---------------------------------------------------- status (air/ground)
    def _status_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=10,
                      margin_end=10, margin_top=6, margin_bottom=10)
        self.air_rows = {}
        self.gnd_rows = {}
        air = Adw.PreferencesGroup(title="Air status", description="The vehicle")
        for key, title in (("conn", "Vehicle"), ("armed", "Armed / ready"), ("batt", "Battery"),
                           ("gps", "GPS"), ("ekf", "EKF (position estimate)"), ("vibe", "Vibration"),
                           ("link", "Telemetry link"), ("radio", "Telemetry radio"),
                           ("home", "Home"), ("time", "Flight time"), ("wind", "Wind"),
                           ("fw", "Firmware")):
            r = Adw.ActionRow(title=title)
            val = label("—", ["numeric"], xalign=1.0, wrap=True, max_width_chars=26)
            dot = Gtk.Image(icon_name="media-record-symbolic", pixel_size=12)
            r.add_suffix(val)
            r.add_prefix(dot)
            self.air_rows[key] = (val, dot)
            air.add(r)
        box.append(air)
        gnd = Adw.PreferencesGroup(title="Ground status", description="This computer and its links")
        for key, title in (("pos", "Ground position"), ("tovehicle", "Vehicle from ground"),
                           ("power", "Computer battery"), ("internet", "Map server / internet"),
                           ("tlog", "Telemetry recording"), ("rates", "Data rates"),
                           ("follow", "Follow Me"), ("tracker", "Antenna tracker")):
            r = Adw.ActionRow(title=title)
            val = label("—", ["numeric"], xalign=1.0, wrap=True, max_width_chars=26)
            dot = Gtk.Image(icon_name="media-record-symbolic", pixel_size=12)
            r.add_suffix(val)
            r.add_prefix(dot)
            self.gnd_rows[key] = (val, dot)
            gnd.add(r)
        box.append(gnd)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sc.set_child(box)
        return sc

    @staticmethod
    def _set_row(rows, key, text, level=None):
        val, dot = rows[key]
        if val.get_text() != text:
            val.set_text(text)
        for c in ("success", "warning", "error", "dim-label"):
            dot.remove_css_class(c)
        dot.add_css_class({"ok": "success", "warn": "warning", "bad": "error"}.get(level, "dim-label"))

    def _update_status(self):
        st = self.v.state
        A, G = self.air_rows, self.gnd_rows
        if st.connected:
            self._set_row(A, "conn", f"{st.vclass.title()} #{st.sysid} · {st.mode}",
                          "ok" if st.link_ok else "bad")
            if st.armed:
                self._set_row(A, "armed", "ARMED", "warn")
            elif st.prearm_ok is False:
                self._set_row(A, "armed", "Disarmed · not ready (pre-arm checks)", "bad")
            else:
                self._set_row(A, "armed", "Disarmed · ready to arm" if st.prearm_ok else "Disarmed", "ok")
            pct = f" · {st.battery_remaining}%" if st.battery_remaining >= 0 else ""
            blev = "ok"
            if 0 <= st.battery_remaining < 30:
                blev = "warn"
            if 0 <= st.battery_remaining < 15:
                blev = "bad"
            self._set_row(A, "batt", f"{st.voltage:.2f} V · {st.current:.1f} A{pct} · {st.battery_used_mah:.0f} mAh", blev)
            glev = "ok" if st.gps_fix >= 3 and st.gps_hdop < 2 else ("warn" if st.gps_fix >= 2 else "bad")
            self._set_row(A, "gps", f"{GPS_FIX.get(st.gps_fix, '?')} · {st.gps_sats} sats · HDOP {st.gps_hdop:.1f}", glev)
            ev = max(st.ekf_vel_var, st.ekf_pos_h_var, st.ekf_pos_v_var, st.ekf_compass_var)
            self._set_row(A, "ekf", f"worst variance {ev:.2f}", "ok" if ev < 0.5 else ("warn" if ev < 0.8 else "bad"))
            vb = max(st.vibe)
            self._set_row(A, "vibe", f"X {st.vibe[0]:.0f}  Y {st.vibe[1]:.0f}  Z {st.vibe[2]:.0f} · clips {sum(st.clipping)}",
                          "ok" if vb < 30 else ("warn" if vb < 60 else "bad"))
            lq = st.link_quality
            self._set_row(A, "link", f"{lq:.0f}% · {st.packets_lost} lost", "ok" if lq > 90 else ("warn" if lq > 60 else "bad"))
            if st.radio_seen:
                self._set_row(A, "radio", f"RSSI {st.rssi}/{st.remrssi} · noise {st.noise}/{st.remnoise} · buf {st.txbuf}%",
                              "ok" if min(st.rssi, st.remrssi) - max(st.noise, st.remnoise) > 20 else "warn")
            else:
                self._set_row(A, "radio", "No SiK radio status")
            if st.home_lat is not None:
                self._set_row(A, "home", f"{fmt_dist(st.dist_home)} · {st.bearing_home:.0f}°", "ok")
            else:
                self._set_row(A, "home", "Not set yet", "warn")
            self._set_row(A, "time", fmt_time(st.flight_time))
            self._set_row(A, "wind", f"{st.wind_speed:.1f} m/s from {st.wind_dir:.0f}°")
            self._set_row(A, "fw", st.firmware or "—")
        else:
            for k in A:
                self._set_row(A, k, "—")
            self._set_row(A, "conn", "Not connected", "bad")

        g = self.shared.ground
        if g.valid():
            self._set_row(G, "pos", f"{g.lat:.6f}, {g.lon:.6f} · {g.status}", "ok")
            if st.has_position:
                az, el, d = tracker_angles(g.lat, g.lon, g.alt or 0, st.lat, st.lon, st.alt_msl)
                self._set_row(G, "tovehicle", f"{fmt_dist(d)} · bearing {az:.0f}° · elevation {el:.0f}°", "ok")
            else:
                self._set_row(G, "tovehicle", "—")
        else:
            self._set_row(G, "pos", g.status + " (set in Ground screen)", "warn" if g.source != "none" else None)
            self._set_row(G, "tovehicle", "Needs ground position")
        pct, chg = laptop_power()
        if pct is None:
            self._set_row(G, "power", "Mains power (no battery)", "ok")
        else:
            self._set_row(G, "power", f"{pct}%" + (" · charging" if chg else " · on battery"),
                          "ok" if chg or pct > 40 else ("warn" if pct > 15 else "bad"))
        on = tiles.NetState.online
        mode = self.settings.get("map_mode", "online")
        if mode == "offline":
            self._set_row(G, "internet", "Offline mode (cached maps only)", "warn")
        elif on is None:
            self._set_row(G, "internet", "Not checked yet")
        else:
            self._set_row(G, "internet", "Online" if on else "Offline — using cached maps", "ok" if on else "warn")
        if self.v.tlog_path and self.v.tlog:
            try:
                size = os.path.getsize(self.v.tlog_path)
            except OSError:
                size = 0
            self._set_row(G, "tlog", f"{os.path.basename(self.v.tlog_path)} · {size / 1e6:.1f} MB", "ok")
        else:
            self._set_row(G, "tlog", "Not recording" if self.v.is_open else "—")
        if self.v.is_open:
            self._set_row(G, "rates", f"in {self.v.state.rx_bytes / 1024:.0f} kB · out {self.v.state.tx_bytes / 1024:.0f} kB · {self.v.parser.crc_errors} bad packets")
        else:
            self._set_row(G, "rates", "—")
        f = self.shared.follow
        self._set_row(G, "follow", f.status, "ok" if f.active and f.status.startswith("Following") else ("warn" if f.active else None))
        t = self.shared.tracker
        self._set_row(G, "tracker", t.status, "ok" if t.active and t.status.startswith("Tracking") else ("warn" if t.active else None))

    # --------------------------------------------------------- messages tab
    def _messages_tab(self):
        self.msg_view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                     css_classes=["messages-view"], left_margin=10, right_margin=10,
                                     top_margin=6, bottom_margin=6, monospace=False)
        buf = self.msg_view.get_buffer()
        buf.create_tag("time", foreground="#8a8f98")
        buf.create_tag("crit", foreground="#ff5a4e", weight=Pango.Weight.BOLD)
        buf.create_tag("warn", foreground="#f0a020")
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.set_child(self.msg_view)
        self.msg_scroll = sc
        return sc

    def _add_message(self, entry):
        ts, sev, text = entry
        buf = self.msg_view.get_buffer()
        end = buf.get_end_iter()
        buf.insert_with_tags_by_name(end, time.strftime("%H:%M:%S ", time.localtime(ts)), "time")
        tag = "crit" if sev <= 3 else ("warn" if sev == 4 else None)
        end = buf.get_end_iter()
        if tag:
            buf.insert_with_tags_by_name(end, text + "\n", tag)
        else:
            buf.insert(end, text + "\n")
        if buf.get_line_count() > 1000:
            start = buf.get_start_iter()
            cut = buf.get_iter_at_line(200)[1]
            buf.delete(start, cut)
        GLib.idle_add(self._scroll_messages)

    def _scroll_messages(self):
        adj = self.msg_scroll.get_vadjustment()
        adj.set_value(adj.get_upper())
        return False

    # --------------------------------------------------------------- RC tab
    def _rc_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=10, margin_end=10,
                      margin_top=6, margin_bottom=10)
        self.rc_bars = []
        self.servo_bars = []
        g = Gtk.Grid(column_spacing=8, row_spacing=3)
        g.attach(label("Ch", ["panel-header"]), 0, 0, 1, 1)
        g.attach(label("RC input", ["panel-header"]), 1, 0, 1, 1)
        g.attach(label("Servo output", ["panel-header"]), 2, 0, 1, 1)
        for i in range(16):
            g.attach(label(str(i + 1), ["dim-label", "numeric"]), 0, i + 1, 1, 1)
            for col, store in ((1, self.rc_bars), (2, self.servo_bars)):
                lb = Gtk.LevelBar(min_value=800, max_value=2200, hexpand=True, valign=Gtk.Align.CENTER)
                lb.remove_offset_value("low")
                lb.remove_offset_value("high")
                lb.remove_offset_value("full")
                ov = Gtk.Overlay()
                ov.set_child(lb)
                txt = label("", ["caption", "numeric"], xalign=0.5)
                ov.add_overlay(txt)
                g.attach(ov, col, i + 1, 1, 1)
                store.append((lb, txt))
        box.append(g)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sc.set_child(box)
        return sc

    def _update_rc(self):
        st = self.v.state
        for (lb, txt), val in zip(self.rc_bars, st.rc[:16]):
            v = val if 800 <= val <= 2200 else 800
            lb.set_value(v)
            txt.set_text(str(val) if val and val != 65535 else "")
        for (lb, txt), val in zip(self.servo_bars, st.servo[:16]):
            v = val if 800 <= val <= 2200 else 800
            lb.set_value(v)
            txt.set_text(str(val) if val else "")

    # ------------------------------------------------------------ map overlays
    def _map_chips(self):
        box = Gtk.Box(spacing=6, halign=Gtk.Align.START, valign=Gtk.Align.START, margin_start=10,
                      margin_top=10, css_classes=["map-card"])
        self.chip_armed = label("DISCONNECTED", ["armed-chip", "disarmed"])
        self.chip_mode = label("—", ["chip", "info"])
        self.chip_batt = label("BAT", ["chip"])
        self.chip_link = label("LINK", ["chip"])
        self.chip_gnd = label("GND", ["chip"], tooltip_text="Ground station GPS")
        self.chip_alt = label("", ["chip", "numeric"])
        for c in (self.chip_armed, self.chip_mode, self.chip_alt, self.chip_batt, self.chip_link,
                  self.chip_gnd):
            box.append(c)
        return box

    def _map_buttons(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.END,
                      valign=Gtk.Align.START, margin_end=10, margin_top=10)
        self.follow_btn = Gtk.ToggleButton(icon_name="find-location-symbolic", active=True,
                                           tooltip_text="Keep the map centred on the vehicle",
                                           css_classes=["osd", "circular"])
        self.follow_btn.connect("toggled", self._toggle_follow)
        box.append(self.follow_btn)
        self.ground_btn = Gtk.ToggleButton(icon_name="network-wireless-symbolic", active=self.show_ground,
                                           tooltip_text="Show the ground station (its GPS position) on the map",
                                           css_classes=["osd", "circular"])
        self.ground_btn.connect("toggled", self._toggle_ground)
        box.append(self.ground_btn)
        self.both_btn = Gtk.ToggleButton(icon_name="view-dual-symbolic", active=False,
                                         tooltip_text="Keep the aircraft and the ground station both in view",
                                         css_classes=["osd", "circular"])
        self.both_btn.connect("toggled", self._toggle_both)
        box.append(self.both_btn)
        box.append(button("", lambda: self.map.zoom_at(1), ["osd", "circular"], icon="zoom-in-symbolic", tooltip="Zoom in"))
        box.append(button("", lambda: self.map.zoom_at(-1), ["osd", "circular"], icon="zoom-out-symbolic", tooltip="Zoom out"))
        box.append(map_type_button(self.shared))
        box.append(button("", self._fit_mission, ["osd", "circular"], icon="zoom-fit-best-symbolic",
                          tooltip="Show the whole mission"))
        return box

    def _replay_bar(self):
        bar = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=40,
                      css_classes=["map-card"], visible=False)
        self.rp_play = button("", self._replay_toggle, ["flat", "circular"], icon="media-playback-pause-symbolic",
                              tooltip="Play / pause")
        bar.append(self.rp_play)
        self.rp_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.rp_scale.set_size_request(360, -1)
        self.rp_scale.set_draw_value(False)
        self.rp_scale.connect("change-value", self._replay_seek)
        bar.append(self.rp_scale)
        self.rp_time = label("", ["numeric", "caption"])
        bar.append(self.rp_time)
        self.rp_speed = Gtk.DropDown(model=Gtk.StringList.new(["0.5×", "1×", "2×", "5×", "10×", "25×"]))
        self.rp_speed.set_selected(1)
        self.rp_speed.connect("notify::selected", self._replay_speed)
        bar.append(self.rp_speed)
        bar.append(button("", lambda: self.v.disconnect(), ["flat", "circular"], icon="window-close-symbolic",
                          tooltip="Stop replay"))
        self.replay_bar = bar
        return bar

    def _replay_transport(self):
        t = self.v.transport
        return t if getattr(t, "kind", "") == "tlog" else None

    def _replay_toggle(self):
        t = self._replay_transport()
        if t:
            if t.paused and t.index >= len(t.packets):
                t.seek(0)
            t.paused = not t.paused

    def _replay_seek(self, scale, scroll, value):
        t = self._replay_transport()
        if t:
            t.seek(value)
            self.trail.clear()
        return False

    def _replay_speed(self, dd, *_):
        t = self._replay_transport()
        if t:
            t.speed = [0.5, 1, 2, 5, 10, 25][dd.get_selected()]

    def _update_replay(self):
        t = self._replay_transport()
        self.replay_bar.set_visible(t is not None)
        if not t:
            return
        self.rp_scale.set_range(0, max(1, t.duration))
        self.rp_scale.set_value(t.position)
        self.rp_time.set_text(f"{fmt_time(t.position)} / {fmt_time(t.duration)}")
        self.rp_play.set_icon_name("media-playback-start-symbolic" if t.paused else "media-playback-pause-symbolic")

    def _gps_card(self):
        """Aircraft GPS status, bottom-left of the map (where Mission Planner shows it)."""
        card = Gtk.Box(spacing=8, halign=Gtk.Align.START, valign=Gtk.Align.END, margin_start=10,
                       margin_bottom=46, css_classes=["map-card"], visible=False)
        self.gps_icon = Gtk.Image(icon_name="find-location-symbolic", pixel_size=18)
        card.append(self.gps_icon)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.gps_fix_lbl = label("GPS", ["heading"])
        self.gps_detail_lbl = label("", ["caption", "numeric"])
        col.append(self.gps_fix_lbl)
        col.append(self.gps_detail_lbl)
        card.append(col)
        card.set_tooltip_text("The aircraft's GPS. 3D Fix or better is needed for position modes and missions; "
                              "HDOP under 1.5 is good.")
        self.gps_card = card
        return card

    def _update_gps_card(self, st):
        self.gps_card.set_visible(True)
        lvl = "success" if st.gps_fix >= 3 and st.gps_hdop < 2 else ("warning" if st.gps_fix >= 2 else "error")
        self.gps_fix_lbl.set_text(f"GPS: {GPS_FIX.get(st.gps_fix, 'Unknown')}")
        self.gps_fix_lbl.set_css_classes(["heading", lvl])
        self.gps_icon.set_css_classes([lvl])
        detail = f"{st.gps_sats} satellites · HDOP {st.gps_hdop:.1f}"
        if st.gps2_fix is not None:
            detail += f" · GPS2 {GPS_FIX.get(st.gps2_fix, '?')}"
        self.gps_detail_lbl.set_text(detail)

    def _update_attrib(self):
        p = self.map.provider
        a = p.attribution
        self.attrib.set_text(f" {a} ")

    def _fit_mission(self):
        pts = [(it.x, it.y) for it in self.shared.mission if it.has_location]
        st = self.v.state
        if st.has_position:
            pts.append((st.lat, st.lon))
        if pts:
            self.follow_btn.set_active(False)
            self.map.fit(pts)

    def set_trail_seconds(self, seconds):
        self.trail.seconds = seconds
        self.settings.set("trail_seconds", seconds)
        self.trail.sample()
        self.map.queue_draw()
        if getattr(self, "trail_dd", None):
            secs = [c[0] for c in TRAIL_CHOICES]
            if seconds in secs and self.trail_dd.get_selected() != secs.index(seconds):
                self.trail_dd.set_selected(secs.index(seconds))

    def _toggle_follow(self, b):
        self.follow_vehicle = b.get_active()
        if self.follow_vehicle and getattr(self, "both_btn", None) and self.both_btn.get_active():
            self.both_btn.set_active(False)

    def _toggle_ground(self, b):
        self.show_ground = b.get_active()
        self.settings.set("show_ground", self.show_ground)
        self.map.queue_draw()

    def _toggle_both(self, b):
        self.keep_both = b.get_active()
        if self.keep_both:
            self.follow_btn.set_active(False)
            if not self.shared.ground.valid():
                toast("No ground station position yet (choose its source on the Ground tab)")
            self._fit_both(force=True)

    def _fit_both(self, force=False):
        st = self.v.state
        g = self.shared.ground
        pts = []
        if st.has_position:
            pts.append((st.lat, st.lon))
        if g.valid():
            pts.append((g.lat, g.lon))
        if len(pts) == 2:
            # stay put while both are comfortably inside the view
            scr = [self.map.to_screen(*p) for p in pts]
            w, h = self.map.get_width(), self.map.get_height()
            inside = all(70 < x < w - 70 and 70 < y < h - 70 for x, y in scr)
            tiny = math.hypot(scr[0][0] - scr[1][0], scr[0][1] - scr[1][1]) < min(w, h) * 0.18
            far = max(abs(pts[0][0] - pts[1][0]), abs(pts[0][1] - pts[1][1])) > 1e-5
            now = time.monotonic()
            if far and (force or not inside or (tiny and now - getattr(self, "_last_fit", 0) > 3)):
                self._last_fit = now
                self.map.fit(pts, pad=110)
        elif pts:
            self.map.set_center(*pts[0])

    def _update_ground_chip(self):
        g = self.shared.ground
        st = self.v.state
        st.ground_sats = g.sats
        if g.valid() and st.has_position:
            az, el, d = tracker_angles(g.lat, g.lon, g.alt or 0, st.lat, st.lon, st.alt_msl)
            st.dist_ground, st.bearing_ground, st.elev_ground = d, az, el
        if g.source == "none" or not self.show_ground:
            self.chip_gnd.set_visible(False)
            return
        self.chip_gnd.set_visible(True)
        if g.valid():
            txt = "GND GPS" + (f" · {g.sats}" if g.sats else "")
            if st.has_position:
                txt += f" · {fmt_dist(st.dist_ground)}"
            self.chip_gnd.set_text(txt)
            self.chip_gnd.set_css_classes(["chip", "info"])
        else:
            waiting = g.source in ("esp32", "link") and not self.v.is_open
            self.chip_gnd.set_text("GND GPS · not connected" if waiting else "GND GPS · no fix")
            self.chip_gnd.set_css_classes(["chip", "warn"])
        self.chip_gnd.set_tooltip_text(g.status)

    def _track_distance(self):
        """Distance flown while armed (for the 'Distance flown' tile)."""
        st = self.v.state
        if not st.has_position:
            return
        last = getattr(self, "_last_pos", None)
        if st.armed and last:
            d = haversine(last[0], last[1], st.lat, st.lon)
            if 0.3 < d < 200:
                st.dist_traveled = getattr(st, "dist_traveled", 0.0) + d
        if st.armed and not getattr(self, "_was_armed", False):
            st.dist_traveled = 0.0          # new flight
        self._last_pos = (st.lat, st.lon)
        self._was_armed = st.armed

    def _update_chips(self):
        self._update_ground_chip()
        st = self.v.state
        if not st.connected:
            self.chip_armed.set_text("DISCONNECTED")
            self.chip_armed.set_css_classes(["armed-chip", "disarmed"])
            for c in (self.chip_mode, self.chip_batt, self.chip_link, self.chip_alt):
                c.set_visible(False)
            self.gps_card.set_visible(False)
            return
        for c in (self.chip_mode, self.chip_batt, self.chip_link, self.chip_alt):
            c.set_visible(True)
        self.chip_armed.set_text("ARMED" if st.armed else "DISARMED")
        self.chip_armed.set_css_classes(["armed-chip", "armed" if st.armed else "disarmed"])
        self.chip_mode.set_text(st.mode)
        self.chip_alt.set_text(f"{st.alt:.1f} m · {st.groundspeed:.1f} m/s")
        self._update_gps_card(st)
        btxt = f"{st.voltage:.1f} V" + (f" · {st.battery_remaining}%" if st.battery_remaining >= 0 else "")
        self.chip_batt.set_text(btxt)
        lvl = "ok"
        if 0 <= st.battery_remaining < 30:
            lvl = "warn"
        if 0 <= st.battery_remaining < 15:
            lvl = "bad"
        self.chip_batt.set_css_classes(["chip", lvl])
        lq = st.link_quality
        self.chip_link.set_text(f"Link {lq:.0f}%" if st.link_ok else "NO LINK")
        self.chip_link.set_css_classes(["chip", "bad" if not st.link_ok else ("ok" if lq > 90 else "warn")])

    # ----------------------------------------------------------- map actions
    def _home_pos(self):
        st = self.v.state
        if st.home_lat is not None:
            return st.home_lat, st.home_lon
        if self.shared.mission:
            h = self.shared.mission[0]
            if h.x or h.y:
                return h.x, h.y
        return None

    def _map_moved(self):
        if self.map._drag is not None:
            self.follow_btn.set_active(False)
        self.settings.set("map_center", [self.map.lat, self.map.lon], save=False)
        self.settings.set("map_zoom", self.map.zoom, save=False)

    def _map_click(self, lat, lon, n, x, y):
        if self.measure_layer.start and self.measure_layer.end is None:
            self.measure_layer.end = (lat, lon)
            self.map.queue_draw()
        elif self.measure_layer.end:
            self.measure_layer.start = self.measure_layer.end = None
            self.map.queue_draw()

    def _context_menu(self, lat, lon, x, y):
        pop = Gtk.Popover(has_arrow=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        connected = self.v.state.connected

        def add(text, cb, sensitive=True):
            b = Gtk.Button(label=text, css_classes=["flat"], sensitive=sensitive)
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda *_: (pop.popdown(), cb()))
            box.append(b)

        add("Fly to here", lambda: self._fly_to(lat, lon), connected)
        add("Fly to here at altitude…", lambda: self._fly_to(lat, lon, ask=True), connected)
        add("Point camera here", lambda: self._roi(lat, lon), connected)
        add("Set home here", lambda: self._set_home_at(lat, lon), connected)
        box.append(Gtk.Separator())
        add("Measure distance from here", lambda: self._start_measure(lat, lon))
        add("Centre map here", lambda: self.map.set_center(lat, lon))
        add("Set ground station position here", lambda: self._set_ground_here(lat, lon))
        add("Copy coordinates", lambda: self.get_clipboard().set(f"{lat:.7f}, {lon:.7f}"))
        pop.set_child(box)
        pop.set_parent(self.map)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def _start_measure(self, lat, lon):
        self.measure_layer.start = (lat, lon)
        self.measure_layer.end = None
        toast("Click a second point to measure; click again to clear")

    def _set_ground_here(self, lat, lon):
        cfg = dict(self.settings.get("ground_gps") or {})
        cfg.update(source="manual", lat=lat, lon=lon)
        self.settings.set("ground_gps", cfg)
        self.shared.ground.start()
        self.shared.hub.emit("ground-changed")
        toast("Ground station position set")

    def _run(self, desc, fn):
        def done(res):
            ok, msg = res if isinstance(res, tuple) else (bool(res), "")
            toast(f"{desc}: {'done' if ok else msg}")
        background(fn, done, lambda e: toast(f"{desc} failed: {e}"))

    def _fly_to(self, lat, lon, ask=False):
        st = self.v.state
        alt = st.alt if st.alt > 2 else float(self.settings.get("default_alt", 50))

        def go(a):
            self.target.set(lat, lon)
            self.map.queue_draw()
            self._run("Fly to here", lambda: self.v.goto(lat, lon, a))
        if ask or st.alt < 2:
            ask_number(self.get_root(), "Fly to here", "Target altitude above home:", round(alt), go,
                       lo=1, hi=5000, unit="m")
        else:
            go(alt)

    def _roi(self, lat, lon):
        self._run("Point camera", lambda: self.v.command(201, 0, 0, 0, 0, lat, lon, 0))

    def _set_home_at(self, lat, lon):
        confirm(self.get_root(), "Set home here?",
                "RTL will return to this point instead of the launch position.", "Set Home",
                lambda: self._run("Set home", lambda: self.v.set_home(lat, lon, self.v.state.home_alt or 0)))

    def _set_home_current(self):
        confirm(self.get_root(), "Set home to the current position?",
                "RTL will return to where the vehicle is now.", "Set Home",
                lambda: self._run("Set home", lambda: self.v.set_home()))

    # ---------------------------------------------------------- action handlers
    def _need_conn(self):
        if not self.v.state.connected:
            toast("Connect to a vehicle first")
            return False
        return True

    def _arm(self):
        if not self._need_conn():
            return
        st = self.v.state
        if st.armed:
            body = "The motors will stop." + (" The vehicle appears to be flying!" if st.alt > 1 else "")
            confirm(self.get_root(), "Disarm the vehicle?", body, "Disarm",
                    lambda: self._arm_cmd(False), destructive=True)
        else:
            confirm(self.get_root(), "Arm the vehicle?",
                    "The motors may start spinning. Keep clear of the propellers.", "Arm",
                    lambda: self._arm_cmd(True), destructive=True)

    def _arm_cmd(self, arm, force=False):
        def done(res):
            ok, msg = res
            if ok:
                return
            last = [t for _ts, sev, t in list(self.v.statustext)[-6:] if "arm" in t.lower()]
            why = last[-1] if last else msg
            from .util import alert
            alert(self.get_root(), "Arming refused" if arm else "Disarm refused",
                  f"The vehicle said: {why}\n\nForce-{'arming' if arm else 'disarming'} skips the safety checks. "
                  "Only do this if you understand why the check failed.",
                  (("cancel", "Cancel"), ("force", f"Force {'Arm' if arm else 'Disarm'}")),
                  default="cancel", destructive="force",
                  on_response=lambda r: self._arm_cmd(arm, True) if r == "force" else None)
        background(lambda: self.v.arm(arm, force), done)

    def _takeoff(self):
        if not self._need_conn():
            return
        st = self.v.state

        def go(alt):
            def work():
                if st.vclass == "plane":
                    return self.v.set_mode("TAKEOFF")
                if st.mode != "GUIDED":
                    ok, msg = self.v.set_mode("GUIDED")
                    if not ok:
                        return ok, f"could not switch to GUIDED ({msg})"
                if not st.armed:
                    ok, msg = self.v.arm(True)
                    if not ok:
                        return ok, f"could not arm ({msg})"
                    time.sleep(1.0)
                return self.v.takeoff(alt)
            self._run("Takeoff", work)
        ask_number(self.get_root(), "Takeoff",
                   "The vehicle will switch to GUIDED, arm and climb to this height above home.",
                   float(self.settings.get("takeoff_alt", 10)), lambda a: (self.settings.set("takeoff_alt", a), go(a)),
                   lo=1, hi=500, unit="m")

    def _set_mode(self, name):
        if self._need_conn():
            self._run(f"Mode {name}", lambda: self.v.set_mode(name))

    def _set_mode_dd(self):
        it = self.mode_dd.get_selected_item()
        if it:
            self._set_mode(it.get_string())

    def _set_wp(self):
        if not self._need_conn():
            return
        i = self.wp_dd.get_selected()
        self._run(f"Set WP {i}", lambda: self.v.set_current_wp(i))

    def _start_mission(self):
        if not self._need_conn():
            return
        def work():
            st = self.v.state
            if st.vclass == "copter" and not st.armed:
                return False, "arm the vehicle first"
            ok, msg = self.v.set_mode("AUTO")
            if ok and st.vclass == "copter" and st.alt < 1:
                return self.v.mission_start()
            return ok, msg
        self._run("Start mission", work)

    def _restart_mission(self):
        if not self._need_conn():
            return
        confirm(self.get_root(), "Restart the mission?", "The vehicle will go back to waypoint 1 in AUTO.",
                "Restart", lambda: self._run("Restart mission",
                                             lambda: (self.v.set_current_wp(1), self.v.set_mode("AUTO"))[1]))

    def _change_speed(self):
        if self._need_conn():
            s = self.speed_spin.get_value()
            self._run("Change speed", lambda: self.v.change_speed(s, 0 if self.v.state.vclass == "plane" else 1))

    def _change_alt(self):
        if self._need_conn():
            a = self.alt_spin.get_value()
            self._run("Change altitude", lambda: self.v.change_alt(a))

    def _do_action(self):
        if not self._need_conn():
            return
        name, fn = self._do_actions()[self.action_dd.get_selected()]
        confirm(self.get_root(), name + "?", "Send this command to the vehicle now?", "Send",
                lambda: self._run(name, fn))

    # -------------------------------------------------------------------- tick
    def _on_connect(self):
        self._fill_modes()
        self.trail.clear()
        self.follow_btn.set_active(True)

    def _tick(self):
        self._n += 1
        if not self.get_mapped():
            if self._n % 5 == 0:
                self.trail.sample()
            return True
        st = self.v.state
        self.hud.queue_draw()
        if self._n % 2 == 0:
            self.trail.sample()
            if self.keep_both:
                self._fit_both()
            elif self.follow_vehicle and st.has_position:
                x, y = self.map.to_screen(st.lat, st.lon)
                w, h = self.map.get_width(), self.map.get_height()
                # recentre smoothly when the vehicle leaves the middle area
                if not (w * 0.25 < x < w * 0.75 and h * 0.25 < y < h * 0.75) or self._last_center == 0:
                    self.map.set_center(st.lat, st.lon)
                    self._last_center = 1
            self.map.queue_draw()
            for t in self.tiles:
                t.update(st)
            self._update_chips()
            self._track_distance()
            self._update_replay()
            self.arm_btn.get_child().set_label("Disarm" if st.armed else "Arm")
        if self._n % 5 == 0:
            vis = self.tabs.get_visible_child_name()
            if vis == "airground":
                self._update_status()
            elif vis == "rc":
                self._update_rc()
            if self._n % 10 == 0:
                cur = st.mode
                for b in iter_children(self.quick_modes):
                    btn = b.get_child()
                    lbl = btn.get_label() if btn else ""
                    on = lbl and lbl.upper().replace(" ", "_") == cur
                    (btn.add_css_class if on else btn.remove_css_class)("suggested-action")
        return True


def iter_children(w):
    c = w.get_first_child()
    while c:
        yield c
        c = c.get_next_sibling()


def map_type_button(shared):
    mb = Gtk.MenuButton(icon_name="view-paged-symbolic", tooltip_text="Map type",
                        css_classes=["osd", "circular"], direction=Gtk.ArrowType.LEFT)
    pop = Gtk.Popover()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=6, margin_end=6,
                  margin_top=6, margin_bottom=6)
    box.append(label("MAP TYPE", ["panel-header"]))
    group = None
    cur = shared.settings.get("map_provider")
    for p in tiles.PROVIDERS:
        cb = Gtk.CheckButton(label=p.name, group=group, active=p.id == cur)
        if group is None:
            group = cb

        def toggled(btn, pid=p.id):
            if btn.get_active():
                shared.settings.set("map_provider", pid)
                shared.hub.emit("map-provider", pid)
        cb.connect("toggled", toggled)
        box.append(cb)
    box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
    off = Gtk.Button(label="Offline maps…", css_classes=["flat"])
    off.connect("clicked", lambda *_: (pop.popdown(), shared.window.get_application().activate_action("offline-maps", None)))
    box.append(off)
    pop.set_child(box)
    mb.set_popover(pop)
    return mb
