"""Ground page: ground-station position, Follow Me and antenna tracker."""
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..core.ground import tracker_angles  # noqa: E402
from ..core.transport import BAUD_RATES, TransportError, list_serial_ports  # noqa: E402
from .util import alert, button, combo_row, confirm, fmt_dist, label, readable, toast  # noqa: E402

SOURCES = [
    ("none", "Not used", "MPXAir doesn't know where you are."),
    ("board", "A ground station on the telemetry link",
     "Your ground station's own GPS, read over the same connection as the aircraft "
     "(for example an RFD900 + ESP32 board). Choose its type below."),
    ("nmea", "Separate USB GPS receiver",
     "A GPS plugged in on its own USB/serial port, sending NMEA."),
    ("geoclue", "This computer's location",
     "Ubuntu's location service (Wi-Fi or a built-in GPS). Turn on Location in Ubuntu's Settings."),
    ("manual", "A fixed position you type in",
     "For a ground station that doesn't move."),
]
TRACKER_TYPES = [("maestro", "Pololu Maestro servo controller"),
                 ("ardutracker", "Serial pan/tilt (!!!PAN:…,TLT:… in PWM)"),
                 ("degrees", "Serial pan/tilt (!!!PAN:…,TLT:… in degrees×10)")]


class SourcePicker:
    """Radio-button rows (title + explanation) instead of a cramped dropdown."""

    def __init__(self, grp, sources, current):
        self.ids = [x[0] for x in sources]
        self.checks = []
        self.on_change = None
        first = None
        for sid, title, sub in sources:
            chk = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if first is None:
                first = chk
            else:
                chk.set_group(first)
            row = Adw.ActionRow(title=title, subtitle=sub, activatable_widget=chk)
            row.set_subtitle_lines(2)
            row.add_prefix(chk)
            chk.set_active(sid == current)
            chk.connect("toggled", lambda c: c.get_active() and self.on_change and self.on_change())
            grp.add(row)
            self.checks.append(chk)
        if not any(c.get_active() for c in self.checks):
            self.checks[0].set_active(True)

    def get_selected(self):
        for i, c in enumerate(self.checks):
            if c.get_active():
                return i
        return 0

    def set_selected(self, i):
        self.checks[i].set_active(True)


def port_model():
    ports = [d for d, _ in list_serial_ports()]
    return ports, Gtk.StringList.new([f"{d}" for d in ports] or ["(no serial ports found)"])


class GroundPage(Gtk.Box):
    def __init__(self, shared):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.shared = shared
        self.s = shared.settings
        self.v = shared.vehicle
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_start=24, margin_end=24,
                      margin_top=18, margin_bottom=24)
        clamp = Adw.Clamp(maximum_size=860)
        clamp.set_child(box)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sc.set_child(clamp)
        self.append(sc)
        box.append(label("Ground station", ["title-1"]))
        box.append(label("Tell MPXAir where you (and your antenna) are. This is used for the Ground status "
                         "panel, Follow Me and the antenna tracker.", ["dim-label"], wrap=True))
        box.append(self._position_group())
        box.append(self._board_group())
        box.append(self.settings_grp)
        self._vis()
        box.append(self._follow_group())
        box.append(self._tracker_group())
        GLib.timeout_add(500, self._tick)

    # ------------------------------------------------------------- position
    def _position_group(self):
        cfg = dict(self.s.get("ground_gps") or {})
        grp = Adw.PreferencesGroup(title="Ground position")
        grp.set_description("Where does the ground station's position come from?")
        src = cfg.get("source", "none")
        src = {"esp32": "board", "link": "board"}.get(src, src)
        self.src = SourcePicker(grp, SOURCES, src)
        self.ports, model = port_model()
        self.gps_port = Adw.ComboRow(title="GPS serial port", model=model)
        if cfg.get("device") in self.ports:
            self.gps_port.set_selected(self.ports.index(cfg["device"]))
        self.gps_baud = Adw.ComboRow(title="GPS baud rate", model=Gtk.StringList.new([str(b) for b in BAUD_RATES]))
        self.gps_baud.set_selected(BAUD_RATES.index(int(cfg.get("baud", 9600))) if int(cfg.get("baud", 9600)) in BAUD_RATES else 0)
        readable(self.gps_port)
        grp.add(self.gps_port)
        grp.add(self.gps_baud)
        self.m_lat = self._spin("Latitude", -90, 90, 7, cfg.get("lat", 0))
        self.m_lon = self._spin("Longitude", -180, 180, 7, cfg.get("lon", 0))
        self.m_alt = self._spin("Altitude above sea level (m)", -500, 9000, 1, cfg.get("alt", 0))
        for r in (self.m_lat, self.m_lon, self.m_alt):
            grp.add(r)
        self.pos_status = Adw.ActionRow(title="Status", subtitle="—")
        grp.add(self.pos_status)
        btns = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        btns.append(button("Use the vehicle's home", self._use_home,
                           tooltip="Copy the vehicle's home position into the fixed position"))
        btns.append(button("Apply", self._apply_pos, ["suggested-action"]))
        btns.set_valign(Gtk.Align.CENTER)
        grp.set_header_suffix(btns)
        self.src.on_change = self._source_changed
        self.shared.hub.on("ground-changed", self._sync_source)
        return grp

    # ------------------------------------------------- ground station type
    def _board_group(self):
        from ..core import groundprofiles as gp
        cfg = self.s.get("ground_gps") or {}
        grp = Adw.PreferencesGroup(title="Ground station type",
                                   description="How MPXAir reads the GPS of a ground station that shares the "
                                               "telemetry link. Each type has its own settings.")
        self.profile_ids = list(gp.ORDER)
        cur = cfg.get("board_profile", "rfd_esp32")
        self.type_row = combo_row("Type", [gp.PRESETS[i]["name"] for i in self.profile_ids],
                                  self.profile_ids.index(cur) if cur in self.profile_ids else 0,
                                  self._profile_changed,
                                  tooltip="Pick the kind of ground station you have, then check its settings below")
        grp.add(self.type_row)
        self.about_row = Adw.ActionRow(title="About this type", use_markup=False)
        self.about_row.set_subtitle_lines(6)
        grp.add(self.about_row)
        self.board_row = Adw.ActionRow(title="Ground station", subtitle="Not seen yet", use_markup=False)
        grp.add(self.board_row)
        self.board_follow = Adw.SwitchRow(
            title="Ground station sends its position to the aircraft",
            subtitle="For ArduPilot's Follow mode (set FOLL_ENABLE=1 and FOLL_SYSID=255 on the aircraft).")
        self.board_follow.connect("notify::active", self._board_follow_toggled)
        self._board_follow_sync = False
        grp.add(self.board_follow)
        self.raw_row = Adw.ActionRow(title="Last message from the ground station", subtitle="—", use_markup=False)
        self.raw_row.set_subtitle_lines(4)
        self.raw_row.add_css_class("property")
        grp.add(self.raw_row)
        self.board_grp = grp
        self.settings_grp = Adw.PreferencesGroup(title="Settings for this type")
        sbtns = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        sbtns.append(button("Reset", self._reset_profile, tooltip="Put this type's settings back to the defaults"))
        sbtns.append(button("Test now", self._test_profile,
                            tooltip="Send the poll command once and show what comes back"))
        sbtns.append(button("Save", self._save_profile, ["suggested-action"]))
        self.settings_grp.set_header_suffix(sbtns)
        self.settings_grp.set_description("Building your own ground station? The manual explains each type and "
                                          "has example firmware.")
        help_row = Adw.ActionRow(title="How to connect your own ground station", activatable=True,
                                 subtitle="Opens the online manual")
        help_row.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        help_row.connect("activated", lambda *_: self._open_help())
        grp.add(help_row)
        self.profile_rows = {}
        self._fill_profile()
        return grp

    def _open_help(self):
        from ..app import open_docs
        open_docs(self.get_root(), "custom-ground-station.html")

    def _current_pid(self):
        return self.profile_ids[self.type_row.get_selected()]

    def _profile_changed(self, i):
        cfg = dict(self.s.get("ground_gps") or {})
        cfg["board_profile"] = self.profile_ids[i]
        self.s.set("ground_gps", cfg)
        self._fill_profile()
        if cfg.get("source") == "board":
            self.shared.ground.start()

    def _fill_profile(self):
        from ..core import groundprofiles as gp
        pid = self._current_pid()
        p = gp.profile(self.s, pid)
        self.about_row.set_subtitle(p["about"])
        has_follow = bool(p.get("follow_on"))
        self.board_follow.set_visible(has_follow)
        self.board_row.set_visible(p["kind"] == "text_json")
        for row in self.profile_rows.values():
            self.settings_grp.remove(row[0])
        self.profile_rows = {}
        for key in gp.EDITABLE[p["kind"]]:
            title, help_, typ = gp.FIELDS[key]
            val = p.get(key)
            if typ == "bool":
                row = Adw.SwitchRow(title=title, subtitle=help_, active=bool(val), use_markup=False)
                getter = row.get_active
            else:
                row = Adw.ActionRow(title=title, subtitle=help_, use_markup=False)
                row.set_subtitle_lines(3)
                entry = Gtk.Entry(text="" if val is None else f"{val:g}" if isinstance(val, float) else str(val),
                                  valign=Gtk.Align.CENTER, width_chars=16, max_width_chars=22)
                entry.add_css_class("mono")
                row.add_suffix(entry)
                getter = entry.get_text
            row.set_tooltip_text(help_)
            self.settings_grp.add(row)
            self.profile_rows[key] = (row, getter, typ)

    def _collect_profile(self):
        from ..core import groundprofiles as gp
        pid = self._current_pid()
        kind = gp.PRESETS[pid]["kind"]
        out = {}
        for key, (_row, getter, typ) in self.profile_rows.items():
            v = getter()
            try:
                if typ == "bool":
                    out[key] = bool(v)
                elif typ == "float":
                    out[key] = float(v)
                elif typ == "int":
                    out[key] = int(float(v or 0))
                elif typ == "field" and kind == "text_csv":
                    out[key] = int(v) if str(v).strip() else 0
                else:
                    out[key] = str(v)
            except ValueError:
                raise ValueError(f"“{gp.FIELDS[key][0]}” must be a number.")
        if kind in ("text_json", "text_csv") and not out.get("prefix"):
            raise ValueError("The reply prefix can't be empty: MPXAir uses it to find the board's replies.")
        return pid, out

    def _save_profile(self):
        from ..core import groundprofiles as gp
        try:
            pid, values = self._collect_profile()
        except ValueError as e:
            alert(self.get_root(), "Can't save these settings", str(e))
            return
        gp.save_profile(self.s, pid, values)
        cfg = dict(self.s.get("ground_gps") or {})
        if cfg.get("source") == "board" and cfg.get("board_profile") == pid:
            self.shared.ground.start()
        toast("Ground station settings saved")

    def _reset_profile(self):
        from ..core import groundprofiles as gp
        pid = self._current_pid()
        gp.reset_profile(self.s, pid)
        self._fill_profile()
        if (self.s.get("ground_gps") or {}).get("board_profile") == pid:
            self.shared.ground.start()
        toast("Settings put back to the defaults")

    def _test_profile(self):
        from ..core import groundprofiles as gp
        g = self.shared.ground
        if (self.s.get("ground_gps") or {}).get("source") != "board":
            self.src.set_selected([x[0] for x in SOURCES].index("board"))
        if not self.v.is_open:
            alert(self.get_root(), "Not connected",
                  "Connect to the ground station first (top right), then press Test now.")
            return
        p = gp.profile(self.s, self._current_pid())
        if p.get("poll") and p["kind"] in ("text_json", "text_csv"):
            self.v.send_text(p["poll"], gp.unescape(p.get("line_end", "\\r")))
            toast(f"Sent “{p['poll']}” — the reply appears below")
        else:
            toast("This type doesn't send anything; MPXAir is listening for the ground station's data")


    def _source_changed(self):
        self._vis()
        if SOURCES[self.src.get_selected()][0] in ("none", "board", "geoclue"):
            self._apply_pos(quiet=True)

    def _sync_source(self, *_):
        want = (self.s.get("ground_gps") or {}).get("source", "none")
        pid = (self.s.get("ground_gps") or {}).get("board_profile")
        if pid in getattr(self, "profile_ids", []) and self._current_pid() != pid:
            self.type_row.set_selected(self.profile_ids.index(pid))
        ids = [x[0] for x in SOURCES]
        if want in ids and self.src.get_selected() != ids.index(want):
            cb = self.src.on_change
            self.src.on_change = None
            self.src.set_selected(ids.index(want))
            self.src.on_change = cb
            self._vis()

    def _board_follow_toggled(self, row, *_):
        if self._board_follow_sync:
            return
        if not self.shared.ground.board_follow(row.get_active()):
            toast("Connect to the ground station board first")
            self._board_follow_sync = True
            row.set_active(False)
            self._board_follow_sync = False
        else:
            toast("Ground position to aircraft: " + ("on" if row.get_active() else "off"))

    def _spin(self, title, lo, hi, digits, val):
        r = Adw.SpinRow.new_with_range(lo, hi, 10 ** -digits if digits else 1)
        r.set_digits(digits)
        r.set_title(title)
        r.set_value(float(val or 0))
        return r

    def _vis(self):
        src = SOURCES[self.src.get_selected()][0]
        self.board_grp.set_visible(src == "board")
        self.settings_grp.set_visible(src == "board")
        for w in (self.gps_port, self.gps_baud):
            w.set_visible(src == "nmea")
        for w in (self.m_lat, self.m_lon):
            w.set_visible(src == "manual")
        self.m_alt.set_visible(src in ("manual", "geoclue", "nmea"))

    def _use_home(self):
        st = self.v.state
        if st.home_lat is None:
            toast("The vehicle has no home position yet")
            return
        self.src.set_selected([x[0] for x in SOURCES].index("manual"))
        self.m_lat.set_value(st.home_lat)
        self.m_lon.set_value(st.home_lon)
        self.m_alt.set_value(st.home_alt or 0)

    def _apply_pos(self, quiet=False):
        cfg = dict(self.s.get("ground_gps") or {})
        cfg["source"] = SOURCES[self.src.get_selected()][0]
        if self.ports:
            cfg["device"] = self.ports[min(self.gps_port.get_selected(), len(self.ports) - 1)]
        cfg["baud"] = BAUD_RATES[self.gps_baud.get_selected()]
        cfg["lat"] = self.m_lat.get_value()
        cfg["lon"] = self.m_lon.get_value()
        cfg["alt"] = self.m_alt.get_value()
        tr = self.v.transport
        if cfg["source"] == "nmea" and getattr(tr, "kind", "") == "serial" and getattr(tr, "device", None) == cfg.get("device"):
            alert(self.get_root(), "That port is the vehicle connection",
                  f"{cfg['device']} is already used for the vehicle link, so it can't be opened twice. "
                  "If the GPS is on the same radio board, choose “GPS on the telemetry radio board” instead.")
            return
        self.s.set("ground_gps", cfg)
        g = self.shared.ground
        g.start()
        if cfg["source"] in ("nmea", "geoclue"):
            g.alt = cfg["alt"] if cfg["alt"] else g.alt
        if not quiet:
            toast("Ground position settings applied")

    # ------------------------------------------------------------ follow me
    def _follow_group(self):
        cfg = dict(self.s.get("follow_me") or {})
        grp = Adw.PreferencesGroup(title="Follow Me",
                                   description="The vehicle keeps flying to your position (in GUIDED mode). "
                                               "Needs a live ground position from a GPS or the location service.")
        self.f_alt = self._spin("Altitude above home (m)", 2, 500, 0, cfg.get("alt", 20))
        self.f_off = self._spin("Stay this far away (m, 0 = overhead)", 0, 500, 0, cfg.get("offset_m", 0))
        self.f_rate = self._spin("Updates per second", 0.2, 5, 1, cfg.get("rate_hz", 2))
        for r in (self.f_alt, self.f_off, self.f_rate):
            grp.add(r)
        self.f_status = Adw.ActionRow(title="Status", subtitle="Off")
        grp.add(self.f_status)
        self.f_btn = button("Start Follow Me", self._follow_toggle, ["suggested-action"], valign=Gtk.Align.CENTER)
        grp.set_header_suffix(self.f_btn)
        return grp

    def _save_follow(self):
        self.s.set("follow_me", {"alt": self.f_alt.get_value(), "offset_m": self.f_off.get_value(),
                                 "rate_hz": self.f_rate.get_value(), "min_move_m": 2})

    def _follow_toggle(self):
        f = self.shared.follow
        if f.active:
            f.stop()
            return
        self._save_follow()
        if not self.v.state.connected:
            toast("Connect to a vehicle first")
            return
        if not self.shared.ground.valid() or self.shared.ground.source == "manual":
            alert(self.get_root(), "No live ground position",
                  "Follow Me needs a moving position: choose a GPS receiver or the location service above.")
            return

        def go():
            from .util import background

            def work():
                if self.v.state.mode != "GUIDED":
                    return self.v.set_mode("GUIDED")
                return True, "ok"

            def done(r):
                if r[0]:
                    f.start()
                    toast("Follow Me started")
                else:
                    toast(f"Could not switch to GUIDED: {r[1]}")
            background(work, done)
        confirm(self.get_root(), "Start Follow Me?",
                f"The vehicle will switch to GUIDED and fly towards you at {self.f_alt.get_value():.0f} m. "
                "Switch to another mode on your transmitter at any time to take back control.",
                "Start", go)

    # -------------------------------------------------------------- tracker
    def _tracker_group(self):
        cfg = dict(self.s.get("tracker") or {})
        grp = Adw.PreferencesGroup(title="Antenna tracker",
                                   description="Points a pan/tilt antenna at the vehicle. The tracker's position "
                                               "is the ground position above.")
        ids = [x[0] for x in TRACKER_TYPES]
        self.t_type = Adw.ComboRow(title="Tracker hardware", model=Gtk.StringList.new([x[1] for x in TRACKER_TYPES]))
        self.t_type.set_selected(ids.index(cfg.get("type", "maestro")) if cfg.get("type") in ids else 0)
        readable(self.t_type)
        self.t_type.set_tooltip_text("The pan/tilt controller your antenna is mounted on")
        grp.add(self.t_type)
        self.t_ports, model = port_model()
        self.t_port = Adw.ComboRow(title="Serial port", model=model)
        if cfg.get("device") in self.t_ports:
            self.t_port.set_selected(self.t_ports.index(cfg["device"]))
        readable(self.t_port)
        grp.add(self.t_port)
        self.t_baud = Adw.ComboRow(title="Baud rate", model=Gtk.StringList.new([str(b) for b in BAUD_RATES]))
        b = int(cfg.get("baud", 9600))
        self.t_baud.set_selected(BAUD_RATES.index(b) if b in BAUD_RATES else 0)
        grp.add(self.t_baud)
        self.t = {}
        for key, title, lo, hi, d in (("pan_ch", "Pan servo channel", 0, 23, 0), ("tilt_ch", "Tilt servo channel", 0, 23, 0),
                                      ("pan_min", "Pan PWM minimum", 500, 2500, 0), ("pan_max", "Pan PWM maximum", 500, 2500, 0),
                                      ("pan_range", "Pan range (degrees)", 10, 720, 0), ("pan_trim", "Pan trim (degrees)", -180, 180, 0),
                                      ("tilt_min", "Tilt PWM minimum", 500, 2500, 0), ("tilt_max", "Tilt PWM maximum", 500, 2500, 0),
                                      ("tilt_range", "Tilt range (degrees)", 10, 360, 0), ("tilt_trim", "Tilt trim (degrees)", -90, 90, 0)):
            r = self._spin(title, lo, hi, d, cfg.get(key, 0))
            self.t[key] = r
            grp.add(r)
        self.t_pan_rev = Adw.SwitchRow(title="Reverse pan", active=bool(cfg.get("pan_rev")))
        self.t_tilt_rev = Adw.SwitchRow(title="Reverse tilt", active=bool(cfg.get("tilt_rev")))
        grp.add(self.t_pan_rev)
        grp.add(self.t_tilt_rev)
        self.t_status = Adw.ActionRow(title="Status", subtitle="Off")
        grp.add(self.t_status)
        btns = Gtk.Box(spacing=8)
        btns.append(button("Point north, level", self._tracker_test, tooltip="Move the servos to 0° / 0° to check alignment"))
        self.t_btn = button("Start tracking", self._tracker_toggle, ["suggested-action"])
        btns.append(self.t_btn)
        btns.set_valign(Gtk.Align.CENTER)
        grp.set_header_suffix(btns)
        return grp

    def _save_tracker(self):
        cfg = dict(self.s.get("tracker") or {})
        cfg["type"] = TRACKER_TYPES[self.t_type.get_selected()][0]
        if self.t_ports:
            cfg["device"] = self.t_ports[min(self.t_port.get_selected(), len(self.t_ports) - 1)]
        cfg["baud"] = BAUD_RATES[self.t_baud.get_selected()]
        for k, r in self.t.items():
            cfg[k] = r.get_value()
        cfg["pan_rev"] = self.t_pan_rev.get_active()
        cfg["tilt_rev"] = self.t_tilt_rev.get_active()
        self.s.set("tracker", cfg)

    def _tracker_toggle(self):
        tr = self.shared.tracker
        if tr.active:
            tr.stop()
            return
        self._save_tracker()
        try:
            tr.start()
            toast("Antenna tracker started")
        except TransportError as e:
            alert(self.get_root(), "Can't start the tracker", str(e))

    def _tracker_test(self):
        self._save_tracker()
        tr = self.shared.tracker
        started_here = False
        try:
            if not tr.active:
                tr.start()
                tr.active = False       # one-shot: keep the port, not the loop
                started_here = True
            tr.point(0, 0)
            toast(f"Pan {tr.pan_pwm} µs, tilt {tr.tilt_pwm} µs")
        except TransportError as e:
            alert(self.get_root(), "Tracker test failed", str(e))
        finally:
            if started_here:
                tr.stop()

    # ----------------------------------------------------------------- tick
    def _tick(self):
        if not self.get_mapped():
            return True
        g = self.shared.ground
        if g.valid():
            self.pos_status.set_subtitle(f"{g.lat:.6f}, {g.lon:.6f} · {g.alt:.0f} m · {g.status}")
        else:
            self.pos_status.set_subtitle(g.status)
        if g.last_raw:
            age = time.time() - g.last_raw_time
            self.raw_row.set_subtitle(f"{g.last_raw}\n({age:.0f} s ago)")
        elif g.source == "board":
            self.raw_row.set_subtitle("Nothing received yet")
        b = getattr(g, "board", None) or {}
        if b and "fw" in b:
            gp = b.get("gps") or {}
            cnt = b.get("cnt") or {}
            bits = [f"{b.get('name') or 'Ground station'} · firmware {b.get('fw', '?')}"]
            bits.append(f"GPS {'fix' if gp.get('fix') else 'no fix'}, {gp.get('sats', 0)} satellites"
                        + (f", HDOP {gp.get('hdop'):.1f}" if gp.get('fix') and gp.get('hdop') is not None else ""))
            if b.get("follow"):
                bits.append(f"sending position to aircraft ({cnt.get('follow', 0)} sent)")
            self.board_row.set_subtitle(" · ".join(bits))
            if bool(b.get("follow")) != self.board_follow.get_active():
                self._board_follow_sync = True
                self.board_follow.set_active(bool(b.get("follow")))
                self._board_follow_sync = False
        f = self.shared.follow
        self.f_status.set_subtitle(f.status if f.active else "Off")
        self.f_btn.set_label("Stop Follow Me" if f.active else "Start Follow Me")
        self.f_btn.set_css_classes(["destructive-action"] if f.active else ["suggested-action"])
        tr = self.shared.tracker
        st = self.v.state
        txt = tr.status if tr.active else "Off"
        if g.valid() and st.has_position:
            az, el, d = tracker_angles(g.lat, g.lon, g.alt or 0, st.lat, st.lon, st.alt_msl)
            txt += f" · vehicle at {az:.0f}° bearing, {el:.0f}° up, {fmt_dist(d)}"
        self.t_status.set_subtitle(txt)
        self.t_btn.set_label("Stop tracking" if tr.active else "Start tracking")
        self.t_btn.set_css_classes(["destructive-action"] if tr.active else ["suggested-action"])
        return True
