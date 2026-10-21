"""Parameters page (Mission Planner's 'Full Parameter List')."""
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

from ..core import config  # noqa: E402
from ..core.params import (SKIP_ON_LOAD, ParamDocs, format_value, load_param_file,  # noqa: E402
                           save_param_file)
from .paramform import param_tooltip  # noqa: E402
from .util import (alert, background, button, clear_box, confirm, file_dialog, idle, label,  # noqa: E402
                   readable, toast)


# friendly names for parameter groups (the name prefix before the first "_")
GROUP_NAMES = {
    "ACRO": "Acro mode", "ADSB": "ADS-B", "AHRS": "AHRS (orientation)", "ANGLE": "Angle limits",
    "ARMING": "Arming checks", "ARSPD": "Airspeed sensor", "ATC": "Attitude control (tuning)",
    "AUTOTUNE": "Autotune", "AVOID": "Avoidance", "BARO": "Barometer", "BATT": "Battery",
    "BCN": "Beacons", "BRD": "Board", "BTN": "Buttons", "CAM": "Camera", "CAN": "CAN bus",
    "CHUTE": "Parachute", "CIRCLE": "Circle mode", "COMPASS": "Compass", "DISARM": "Disarming",
    "EAHRS": "External AHRS", "EK2": "EKF2 (position estimate)", "EK3": "EKF3 (position estimate)",
    "ESC": "ESC", "FENCE": "Geofence", "FLOW": "Optical flow", "FLTMODE": "Flight modes",
    "FOLL": "Follow mode", "FRAME": "Frame", "FS": "Failsafes", "GND": "Ground (barometer)",
    "GPS": "GPS", "GRIP": "Gripper", "INS": "Inertial sensors (IMU)", "LAND": "Landing",
    "LGR": "Landing gear", "LIM": "Limits", "LOG": "Logging", "LOIT": "Loiter mode",
    "MIS": "Mission", "MNT": "Camera mount (gimbal)", "MODE": "Flight modes", "MOT": "Motors",
    "NAVL1": "L1 navigation", "NTF": "LEDs and buzzer", "OA": "Object avoidance",
    "OSD": "On-screen display", "PHLD": "PosHold mode", "PILOT": "Pilot input",
    "PLND": "Precision landing", "PRX": "Proximity sensors", "PSC": "Position control (tuning)",
    "PTCH2SRV": "Pitch control", "Q": "QuadPlane (VTOL)", "RALLY": "Rally points",
    "RC": "RC input (radio)", "RCMAP": "RC channel mapping", "RELAY": "Relays",
    "RLL2SRV": "Roll control", "RNGFND": "Rangefinder", "RPM": "RPM sensors",
    "RTL": "Return to launch", "SCHED": "Scheduler", "SCR": "Scripting", "SERIAL": "Serial ports",
    "SERVO": "Servo / motor outputs", "SIM": "Simulator", "SR": "Telemetry stream rates",
    "SRTL": "Smart RTL", "STAT": "Statistics", "STEER2SRV": "Steering", "SURFTRAK": "Surface tracking",
    "SYSID": "System ID", "TCAL": "Temperature calibration", "TECS": "Speed and height control (TECS)",
    "TERRAIN": "Terrain", "THR": "Throttle", "TRIM": "Trims", "TUNE": "Transmitter tuning knob",
    "VISO": "Visual odometry", "WINCH": "Winch", "WP": "Waypoints", "WPNAV": "Waypoint navigation",
    "YAW2SRV": "Yaw control", "ZIGZ": "ZigZag mode",
}
KEEP_DIGITS = {"EK2", "EK3", "Q"}


def param_group(name):
    head = name.split("_", 1)[0] if "_" in name else "OTHER"
    if head not in KEEP_DIGITS:
        head = head.rstrip("0123456789") or head
    return head


def group_title(key):
    if key == "OTHER":
        return "Other"
    friendly = GROUP_NAMES.get(key)
    return f"{friendly} ({key})" if friendly else key


class ParamRow(GObject.Object):
    __gtype_name__ = "MPXParamRow"

    def __init__(self, name, value, doc):
        super().__init__()
        self.name = name
        self.value = value
        self.doc = doc or {}
        self.pending = None
        self.group = param_group(name)
        self.search = (name + " " + self.doc.get("DisplayName", "") + " " +
                       self.doc.get("Description", "")).lower()

    def display_value(self):
        v = self.pending if self.pending is not None else self.value
        txt = format_value(v)
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return txt
        for k, lbl in self.doc.get("values", []):
            if abs(k - fv) < 1e-6:
                return f"{txt}  ·  {lbl}"
        return txt


class ParamsPage(Gtk.Box):
    def __init__(self, shared):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.shared = shared
        self.v = shared.vehicle
        self.rows = {}
        self.docs = {}
        self.current = None

        bar = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=10, margin_bottom=8)
        self.search = Gtk.SearchEntry(placeholder_text="Search name or description…", hexpand=True)
        self.search.connect("search-changed", lambda *_: self._search_changed())
        bar.append(self.search)
        self.filter_dd = Gtk.DropDown(model=Gtk.StringList.new(
            ["All parameters", "Changed (not written)", "Standard", "Advanced"]))
        self.filter_dd.connect("notify::selected", lambda *_: self._refilter())
        bar.append(self.filter_dd)
        bar.append(button("Refresh", self.refresh_params, icon="view-refresh-symbolic",
                          tooltip="Download all parameters again"))
        bar.append(button("Load…", self.load_file, icon="document-open-symbolic",
                          tooltip="Load a .param file (changes are reviewed before writing)"))
        bar.append(button("Save…", self.save_file, icon="document-save-symbolic",
                          tooltip="Save all parameters to a .param file"))
        self.write_btn = button("Write changes", self.write_pending, ["suggested-action"],
                                icon="document-send-symbolic")
        self.write_btn.set_sensitive(False)
        bar.append(self.write_btn)
        self.append(bar)

        self.banner = Adw.Banner(title="Connect to a vehicle to see its parameters.")
        self.banner.set_revealed(True)
        self.append(self.banner)

        paned = Gtk.Paned(vexpand=True, shrink_end_child=False, resize_end_child=False)
        self.append(paned)

        self.store = Gio.ListStore.new(ParamRow)
        self.filter = Gtk.CustomFilter.new(self._match)
        self.filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        sorter = Gtk.CustomSorter.new(lambda a, b, _d: (a.name > b.name) - (a.name < b.name))
        self.sorted = Gtk.SortListModel(model=self.filtered, sorter=sorter)
        self.sel = Gtk.SingleSelection(model=self.sorted, autoselect=False, can_unselect=True)
        self.sel.connect("notify::selected-item", self._on_select)
        cv = Gtk.ColumnView(model=self.sel, show_row_separators=True, css_classes=["data-table"])
        for title, fn, expand, width in (("Name", lambda r: r.name, False, 210),
                                         ("Value", lambda r: r.display_value(), False, 210),
                                         ("Units", lambda r: r.doc.get("Units", ""), False, 90),
                                         ("Description", lambda r: r.doc.get("DisplayName", ""), True, 200)):
            f = Gtk.SignalListItemFactory()
            f.connect("setup", self._cell_setup)
            f.connect("bind", self._cell_bind, fn, title)
            col = Gtk.ColumnViewColumn(title=title, factory=f, expand=expand, resizable=True)
            col.set_fixed_width(width)
            cv.append_column(col)
        sc = Gtk.ScrolledWindow(hexpand=True)
        sc.set_child(cv)
        # groups sidebar + list
        self.group = None
        self.groups = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.groups.connect("row-selected", self._group_selected)
        gsc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, css_classes=["side-panel"])
        gsc.set_size_request(240, -1)
        gsc.set_child(self.groups)
        lbox = Gtk.Box()
        lbox.append(gsc)
        lbox.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        sc.set_hexpand(True)
        lbox.append(sc)
        paned.set_start_child(lbox)

        self.editor = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=14,
                              margin_end=14, margin_top=12, margin_bottom=12)
        esc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, css_classes=["side-panel"])
        esc.set_size_request(400, -1)
        esc.set_child(self.editor)
        paned.set_end_child(esc)
        self._paned = paned
        self.count_lbl = label("", ["dim-label", "caption"], margin_start=12, margin_bottom=6)
        self.append(self.count_lbl)
        self._show_editor(None)

        shared.hub.on("params-loaded", lambda ok: self.reload())
        shared.hub.on("vehicle-disconnected", lambda *_: self._disconnected())
        self.v.on("param", lambda name, val, new: idle(self._param_changed, name, val, new))
        GLib.timeout_add(500, self._fix_paned)

    def _fix_paned(self):
        w = self._paned.get_width()
        if w > 0:
            self._paned.set_position(max(400, w - 420))
            return False
        return True

    # ----------------------------------------------------------- cells
    def _cell_setup(self, f, item):
        item.set_child(Gtk.Label(xalign=0, ellipsize=3, margin_start=4, margin_end=4))

    def _cell_bind(self, f, item, fn, title):
        row = item.get_item()
        lb = item.get_child()
        lb.set_text(str(fn(row)))
        lb.set_tooltip_text(param_tooltip(row.name, row.doc) if row.doc else row.name)
        if title == "Name":
            lb.set_css_classes(["mono"])
        elif title == "Value":
            lb.set_css_classes(["numeric", "accent"] if row.pending is not None else ["numeric"])
        else:
            lb.set_css_classes(["dim-label"] if title == "Units" else [])

    # ----------------------------------------------------------- data
    def reload(self):
        p = self.v.params
        self.docs = ParamDocs.for_class(self.v.state.vclass)
        self.rows = {}
        items = []
        for name, val in list(p.values.items()):
            r = ParamRow(name, val, self.docs.get(name))
            self.rows[name] = r
            items.append(r)
        self.store.splice(0, self.store.get_n_items(), items)
        self._fill_groups()
        self.banner.set_revealed(not items)
        self._update_count()
        self._show_editor(None)

    def _disconnected(self):
        if self.rows:
            self.banner.set_title("Not connected — showing the last downloaded values.")
        else:
            self.banner.set_title("Connect to a vehicle to see its parameters.")
        self.banner.set_revealed(True)
        self._update_count()

    def _param_changed(self, name, val, new):
        r = self.rows.get(name)
        if r is None:
            if self.v.params.complete and self.rows:
                r = ParamRow(name, val, self.docs.get(name))
                self.rows[name] = r
                self.store.append(r)
            return
        if r.value != val:
            r.value = val
            if r.pending is not None and abs(float(r.pending) - float(val)) < 1e-6:
                r.pending = None
            self._refresh_row(r)

    def _refresh_row(self, r):
        ok, pos = self.store.find(r)
        if ok:
            self.store.items_changed(pos, 1, 1)
        if self.current is r:
            self._show_editor(r)
        self._update_count()

    def _update_count(self):
        n = sum(1 for r in self.rows.values() if r.pending is not None)
        self.write_btn.set_sensitive(n > 0 and self.v.state.connected)
        self.write_btn.get_child().set_label(f"Write {n} change{'s' if n != 1 else ''}" if n else "Write changes")
        self.count_lbl.set_text(f"{self.filtered.get_n_items()} of {len(self.rows)} parameters shown"
                                + (f" · {n} changed, not yet written" if n else ""))

    def _fill_groups(self):
        counts = {}
        for r in self.rows.values():
            counts[r.group] = counts.get(r.group, 0) + 1
        # tiny groups go under "Other" so the list stays short
        keys = sorted((k for k, n in counts.items() if n >= 2 or k in GROUP_NAMES),
                      key=lambda k: group_title(k).lower())
        other = sum(n for k, n in counts.items() if k not in keys)
        self._small = {k for k in counts if k not in keys}
        child = self.groups.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.groups.remove(child)
            child = nxt
        entries = [(None, "All parameters", len(self.rows))] + [(k, group_title(k), counts[k]) for k in keys]
        if other:
            entries.append(("OTHER", "Other", other))
        for key, title, n in entries:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=6, margin_start=6, margin_end=6, margin_top=3, margin_bottom=3)
            box.append(label(title, hexpand=True, ellipsize=3))
            box.append(label(str(n), ["dim-label", "caption", "numeric"]))
            row.set_child(box)
            row.key = key
            if key and GROUP_NAMES.get(key):
                row.set_tooltip_text(f"Parameters starting with {key}_")
            self.groups.append(row)
            if key == self.group:
                self.groups.select_row(row)
        if self.groups.get_selected_row() is None:
            self.groups.select_row(self.groups.get_row_at_index(0))

    def _search_changed(self):
        # a search looks through every parameter, not just the selected group
        if self.search.get_text().strip() and self.group is not None:
            self.groups.select_row(self.groups.get_row_at_index(0))
        self._refilter()

    def _group_selected(self, lb, row):
        self.group = getattr(row, "key", None) if row else None
        self._refilter()

    def _match(self, r, *_):
        if self.group == "OTHER":
            if r.group not in getattr(self, "_small", set()) and r.group != "OTHER":
                return False
        elif self.group and r.group != self.group:
            return False
        q = self.search.get_text().strip().lower()
        f = self.filter_dd.get_selected()
        if f == 1 and r.pending is None:
            return False
        if f == 2 and r.doc.get("User", "Standard") != "Standard":
            return False
        if f == 3 and r.doc.get("User") != "Advanced":
            return False
        if not q:
            return True
        return all(w in r.search for w in q.split())

    def _refilter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_count()

    # ----------------------------------------------------------- editor
    def _on_select(self, *_):
        self._show_editor(self.sel.get_selected_item())

    def _show_editor(self, r):
        self.current = r
        clear_box(self.editor)
        if r is None:
            self.editor.append(label("Select a parameter to see what it does and change it.",
                                     ["dim-label"], wrap=True))
            self.editor.append(label("Changes are collected and only sent to the vehicle when you "
                                     "press “Write changes”.", ["dim-label", "caption"], wrap=True))
            return
        d = r.doc
        self.editor.append(label(r.name, ["title-2", "mono"], selectable=True))
        if d.get("DisplayName"):
            self.editor.append(label(d["DisplayName"], ["heading"], wrap=True))
        if d.get("Description"):
            self.editor.append(label(d["Description"], wrap=True))
        if not d:
            self.editor.append(label("No description is available for this parameter in the bundled "
                                     "documentation.", ["dim-label"], wrap=True))
        facts = []
        if d.get("Units"):
            facts.append(f"Units: {d['Units']}")
        if d.get("range"):
            facts.append(f"Range: {d['range'][0]:g} to {d['range'][1]:g}")
        if d.get("Increment"):
            facts.append(f"Step: {d['Increment']}")
        if d.get("RebootRequired", "").lower() == "true":
            facts.append("Needs a reboot to take effect")
        if d.get("User"):
            facts.append(f"Level: {d['User']}")
        if facts:
            self.editor.append(label(" · ".join(facts), ["dim-label", "caption"], wrap=True))

        cur = r.pending if r.pending is not None else r.value
        readonly = d.get("ReadOnly", "").lower() == "true"
        grp = Adw.PreferencesGroup(margin_top=6)
        if d.get("bits"):
            ival = int(cur)
            checks = []
            for bit, lbl in d["bits"]:
                row = Adw.SwitchRow(title=lbl, subtitle=f"bit {bit}", active=bool(ival & (1 << bit)))
                checks.append((bit, row))
                grp.add(row)
            known = sum(1 << b for b, _r in checks)
            getter = lambda: (ival & ~known) | sum(1 << b for b, row in checks if row.get_active())
        elif d.get("values"):
            vals = list(d["values"])
            if not any(abs(k - float(cur)) < 1e-6 for k, _l in vals):
                vals.append((float(cur), "(custom value)"))
            combo = readable(Adw.ComboRow(title="Value", enable_search=len(vals) > 10,
                                          model=Gtk.StringList.new([f"{l}  ({format_value(k)})" for k, l in vals])))
            combo.set_selected(next(i for i, (k, _l) in enumerate(vals) if abs(k - float(cur)) < 1e-6))
            grp.add(combo)
            getter = lambda: vals[combo.get_selected()][0]
        else:
            lo, hi = d.get("range", (-1e9, 1e9))
            lo, hi = min(lo, float(cur)), max(hi, float(cur))
            is_int = isinstance(r.value, int)
            try:
                step = float(d.get("Increment") or (1 if is_int else 0.01))
            except ValueError:
                step = 1 if is_int else 0.01
            frac = len(format_value(cur).partition(".")[2])
            digits = 0 if is_int else max(2, min(6, frac))
            spin = Adw.SpinRow.new_with_range(lo, hi, step)
            spin.set_digits(digits)
            spin.set_title("Value")
            spin.set_value(float(cur))
            grp.add(spin)
            getter = lambda: spin.get_value()
        self.editor.append(grp)
        if readonly:
            self.editor.append(label("This parameter is read-only.", ["warning"]))
        btns = Gtk.Box(spacing=8, homogeneous=True)
        btns.append(button("Keep change", lambda: self._stage(r, getter()), ["suggested-action"],
                           tooltip="Collect this change; write all changes later"))
        btns.append(button("Write now", lambda: self._write_one(r, getter())))
        if r.pending is not None:
            btns.append(button("Undo change", lambda: self._stage(r, None)))
        self.editor.append(btns)
        self.editor.append(label(f"Value on the vehicle: {format_value(r.value)}",
                                 ["dim-label", "caption", "numeric"]))
        child = btns.get_first_child()
        while child:
            child.set_sensitive(not readonly)
            child = child.get_next_sibling()

    def _stage(self, r, val):
        if val is not None and abs(float(val) - float(r.value)) < 1e-7:
            val = None
        if val is not None and isinstance(r.value, int):
            val = int(round(val))
        r.pending = val
        self._refresh_row(r)

    def _write_one(self, r, val):
        if not self.v.state.connected:
            toast("Connect to a vehicle first")
            return
        if isinstance(r.value, int):
            val = int(round(val))

        def done(res):
            ok, msg = res
            if ok:
                r.pending = None
                r.value = self.v.params.get(r.name, val)
                toast(f"{r.name} = {format_value(r.value)}")
                self._reboot_hint([r])
            else:
                toast(msg, 5)
            self._refresh_row(r)
        background(lambda: self.v.params.set(r.name, val), done)

    def write_pending(self):
        rows = [r for r in self.rows.values() if r.pending is not None]
        if not rows or not self.v.state.connected:
            return
        win = self.get_root()
        todo = [(r, r.pending) for r in rows]

        def work():
            errors = []
            for i, (r, val) in enumerate(todo):
                ok, msg = self.v.params.set(r.name, val)
                if ok:
                    r.value = self.v.params.get(r.name, val)
                    r.pending = None
                else:
                    errors.append(msg)
                idle(win.set_progress, (i + 1) / len(todo))
            idle(win.set_progress, None)
            return errors

        def done(errors):
            for r in rows:
                self._refresh_row(r)
            if errors:
                alert(self.get_root(), "Some parameters were not written", "\n".join(errors[:15]))
            else:
                toast(f"Wrote {len(rows)} parameter{'s' if len(rows) != 1 else ''}")
                self._reboot_hint(rows)
            self._refilter()
        background(work, done)

    def _reboot_hint(self, rows):
        need = [r.name for r in rows if r.doc.get("RebootRequired", "").lower() == "true"]
        if need:
            confirm(self.get_root(), "Reboot needed",
                    f"{', '.join(need[:6])} only take effect after the autopilot restarts. Reboot now?",
                    "Reboot", lambda: background(self.v.reboot, lambda r: toast("Rebooting the autopilot…")))

    # ----------------------------------------------------------- toolbar
    def refresh_params(self):
        if not self.v.state.connected:
            toast("Connect to a vehicle first")
            return
        win = self.get_root()

        def work():
            ok = self.v.params.fetch(progress=lambda a, b: idle(win.set_progress, a / b if b else 0))
            idle(win.set_progress, None)
            return ok
        background(work, lambda ok: (self.reload(), toast("Parameters downloaded" if ok else
                                                           "Some parameters did not arrive")))

    def save_file(self):
        if not self.rows:
            toast("No parameters to save yet")
            return
        vals = {n: r.value for n, r in self.rows.items()}
        name = f"{self.v.state.vclass}-{GLib.DateTime.new_now_local().format('%Y%m%d-%H%M')}.param"

        def got(path):
            try:
                save_param_file(path, vals)
                toast(f"Saved {len(vals)} parameters to {os.path.basename(path)}")
            except OSError as e:
                alert(self.get_root(), "Could not save", str(e))
        file_dialog(self.get_root(), "Save parameters", save=True, initial_name=name, on_file=got,
                    filters=[("Parameter files", ["*.param", "*.parm"])], folder=config.data_dir())

    def load_file(self):
        if not self.rows:
            toast("Connect and download parameters first, then load a file to compare")
            return

        def got(path):
            try:
                vals = load_param_file(path)
            except OSError as e:
                alert(self.get_root(), "Could not open", str(e))
                return
            changed = unknown = 0
            for n, val in vals.items():
                if n in SKIP_ON_LOAD:
                    continue
                r = self.rows.get(n)
                if r is None:
                    unknown += 1
                    continue
                if abs(float(val) - float(r.value)) > 1e-6:
                    self._stage(r, val)
                    changed += 1
            self.filter_dd.set_selected(1)
            self._refilter()
            msg = f"{changed} parameters differ from the vehicle and are staged for writing."
            if unknown:
                msg += f" {unknown} names in the file don't exist on this vehicle and were skipped."
            alert(self.get_root(), "File loaded", msg + " Review them, then press “Write changes”.")
        file_dialog(self.get_root(), "Load parameters",
                    filters=[("Parameter files", ["*.param", "*.parm", "*.txt"]), ("All files", ["*"])],
                    on_file=got, folder=config.data_dir())
