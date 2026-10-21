"""Plan page (Mission Planner's 'Flight Plan' view): missions, geofence, rally points."""
import math
import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from ..core import config  # noqa: E402
from ..core.mission import (FENCE, FRAME_NAMES, MISSION, RALLY, CommandLabels, MissionItem,  # noqa: E402
                            command_short_name, load_poly, load_waypoints, save_poly,
                            save_waypoints)
from ..core.vehicle import bearing, haversine  # noqa: E402
from .fly import map_type_button  # noqa: E402
from .layers import (FENCE_EXCL_CIRC, FENCE_EXCL_POLY, FENCE_INCL_CIRC, FENCE_INCL_POLY,  # noqa: E402
                     FENCE_RETURN, FenceLayer, GroundLayer, MissionLayer, RallyLayer, VehicleLayer)
from .mapview import MapView, draw_home, draw_marker  # noqa: E402
from .progress import ProgressDialog  # noqa: E402
from .util import (alert, ask_number, background, button, clear_box, confirm, file_dialog,  # noqa: E402
                   fmt_dist, idle, label, readable, short_description, toast)

FRAMES = [(3, "Relative to home"), (0, "Above sea level"), (10, "Above terrain")]
FENCE_NAMES = {FENCE_INCL_POLY: "Inclusion polygon", FENCE_EXCL_POLY: "Exclusion polygon",
               FENCE_INCL_CIRC: "Inclusion circle", FENCE_EXCL_CIRC: "Exclusion circle",
               FENCE_RETURN: "Return point"}


class PolygonLayer:
    """Survey polygon being drawn."""

    def __init__(self, page):
        self.page = page
        self.points = []

    def draw(self, cr, view):
        if not self.points:
            return
        pts = [view.to_screen(*p) for p in self.points]
        cr.move_to(*pts[0])
        for p in pts[1:]:
            cr.line_to(*p)
        if len(pts) > 2:
            cr.close_path()
            cr.set_source_rgba(0.6, 0.3, 0.95, 0.15)
            cr.fill_preserve()
        cr.set_source_rgba(0.7, 0.4, 1.0, 0.95)
        cr.set_line_width(2)
        cr.stroke()
        for i, (x, y) in enumerate(pts):
            draw_marker(cr, x, y, str(i + 1), (0.6, 0.3, 0.95), r=8)

    def hit(self, view, x, y):
        if self.page.mode != "polygon":
            return None
        for i, p in enumerate(self.points):
            sx, sy = view.to_screen(*p)
            if math.hypot(sx - x, sy - y) < 10:
                return i
        return None

    def drag(self, view, i, lat, lon):
        self.points[i] = (lat, lon)

    def drag_end(self, view, i):
        self.page.refresh()


class HomeDragLayer:
    """Draws the planned home and lets it be dragged in mission mode."""

    def __init__(self, page):
        self.page = page

    def draw(self, cr, view):
        h = self.page.items[0]
        if h.x or h.y:
            x, y = view.to_screen(h.x, h.y)
            draw_home(cr, x, y)

    def hit(self, view, x, y):
        if self.page.mode != "mission":
            return None
        h = self.page.items[0]
        if not (h.x or h.y):
            return None
        sx, sy = view.to_screen(h.x, h.y)
        return 0 if math.hypot(sx - x, sy - y) < 12 else None

    def drag(self, view, i, lat, lon):
        self.page.move_item(0, lat, lon, live=True)

    def drag_end(self, view, i):
        self.page.move_item(0, None, None, live=False)

    def clicked(self, view, i, n):
        self.page.select_item(0)


class PlanPage(Gtk.Box):
    def __init__(self, shared):
        super().__init__()
        self.shared = shared
        self.v = shared.vehicle
        self.settings = shared.settings
        self.mode = "mission"          # mission | fence | rally | polygon
        self.items = [MissionItem(16, 0, 0, 0, frame=0)]   # [0] is home
        self.fence = []
        self.rally = []
        self.selected = set()
        self.undo_stack = []
        self.redo_stack = []
        self.dirty = False
        self._suspend = False
        self._drag_undo = False
        self._fence_new_kind = None
        self.labels = CommandLabels.for_class("copter")

        paned = Gtk.Paned(hexpand=True, shrink_end_child=False, resize_end_child=False)
        self.append(paned)

        # ---------------------------------------------------------------- map
        self.map = MapView(shared.tex_cache, shared.settings)
        self.poly = PolygonLayer(self)
        self.map.layers = [
            FenceLayer(lambda: self.fence, editor=self),
            RallyLayer(lambda: self.rally, editor=self),
            MissionLayer(lambda: self.items, self._home, lambda: self.v.state.current_wp, editor=self),
            HomeDragLayer(self),
            self.poly,
            GroundLayer(shared.ground, self.v, lambda: bool(shared.settings.get("show_ground", True))),
            VehicleLayer(self.v),
        ]
        self.map.on_click = self._map_click
        self.map.on_context = self._context
        overlay = Gtk.Overlay()
        overlay.set_child(self.map)
        overlay.add_overlay(self._map_toolbar())
        overlay.add_overlay(self._map_buttons())
        self.info = label("", ["map-card", "numeric"], halign=Gtk.Align.START, valign=Gtk.Align.END,
                          margin_start=10, margin_bottom=44)
        overlay.add_overlay(self.info)
        self.hint = label("", ["map-card", "caption"], halign=Gtk.Align.CENTER, valign=Gtk.Align.END,
                          margin_bottom=12)
        overlay.add_overlay(self.hint)
        paned.set_start_child(overlay)

        # --------------------------------------------------------------- side
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["side-panel"], spacing=6)
        side.set_size_request(380, -1)
        side.append(self._side_header())
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.MULTIPLE,
                                css_classes=["navigation-sidebar"])
        self.list.connect("selected-rows-changed", self._list_selection)
        # Our own click handling: a plain click or tap selects just that row,
        # Ctrl adds/removes a row, Shift selects a range. (GTK's default turns
        # touch taps into "add to selection", which selected every row tapped.)
        click = Gtk.GestureClick(button=1)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._list_pressed)
        self.list.add_controller(click)
        self._anchor = None
        self.list.set_placeholder(label("Click on the map to add items.", ["dim-label"], xalign=0.5,
                                        margin_top=30, margin_bottom=30, wrap=True))
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(self.list)
        sc.set_size_request(-1, 160)
        side_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        side_paned.set_start_child(sc)
        self.editor_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=10,
                                  margin_end=10, margin_bottom=10)
        esc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        esc.set_child(self.editor_box)
        esc.set_size_request(-1, 160)
        side_paned.set_end_child(esc)
        side_paned.set_position(300)
        side.append(side_paned)
        paned.set_end_child(side)
        self._paned = paned
        self._paned_set = False

        sc_ctl = Gtk.ShortcutController()
        for trig, cb in (("<Control>z", self.undo), ("<Control><Shift>z", self.redo),
                         ("<Control>y", self.redo), ("Delete", self._delete_selected)):
            sc_ctl.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string(trig),
                                             action=Gtk.CallbackAction.new(lambda *_a, c=cb: c() or True)))
        self.add_controller(sc_ctl)

        shared.hub.on("mission-changed", self._shared_changed)
        shared.hub.on("map-provider", lambda pid: self.map.set_provider(pid))
        shared.hub.on("vehicle-connected", lambda *_: self._vehicle_class_changed())
        shared.hub.on("vehicle-home", lambda *_: self._home_from_vehicle(auto=True))
        GLib.timeout_add(250, self._tick)
        self._set_mode("mission")

    # ------------------------------------------------------------ toolbars
    def _map_toolbar(self):
        bar = Gtk.Box(spacing=6, halign=Gtk.Align.START, valign=Gtk.Align.START, margin_start=10,
                      margin_top=10, css_classes=["map-card"])
        self.mode_btns = {}
        group = Gtk.Box(css_classes=["linked"])
        first = None
        for mid, text in (("mission", "Mission"), ("fence", "Geofence"), ("rally", "Rally"),
                          ("polygon", "Survey area")):
            b = Gtk.ToggleButton(label=text)
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.connect("toggled", lambda btn, m=mid: btn.get_active() and self._set_mode(m))
            self.mode_btns[mid] = b
            group.append(b)
        bar.append(group)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        bar.append(button("Read", self.read_from_vehicle, icon="go-down-symbolic",
                          tooltip="Read the plan from the vehicle"))
        bar.append(button("Write", self.write_to_vehicle, ["suggested-action"], icon="go-up-symbolic",
                          tooltip="Write the plan to the vehicle"))
        mb = Gtk.MenuButton(icon_name="document-open-symbolic", tooltip_text="Files")
        pop = Gtk.Popover()
        pb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for text, cb in (("Load from file…", self.load_file), ("Save to file…", self.save_file)):
            b = Gtk.Button(label=text, css_classes=["flat"])
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda *_a, c=cb: (pop.popdown(), c()))
            pb.append(b)
        pop.set_child(pb)
        mb.set_popover(pop)
        bar.append(mb)
        cmb = Gtk.MenuButton(icon_name="user-trash-symbolic", tooltip_text="Clear…")
        cpop = Gtk.Popover()
        cb_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for text, what in (("Clear mission", "mission"), ("Clear geofence", "fence"),
                           ("Clear rally points", "rally"), ("Clear survey area", "polygon"),
                           ("Clear everything", "all")):
            b = Gtk.Button(label=text, css_classes=["flat"] + (["destructive-action"] if what == "all" else []))
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda *_a, w=what: (cpop.popdown(), self.clear(w)))
            if what == "all":
                cb_box.append(Gtk.Separator())
            cb_box.append(b)
        cpop.set_child(cb_box)
        cmb.set_popover(cpop)
        bar.append(cmb)
        bar.append(button("", self.undo, icon="edit-undo-symbolic", tooltip="Undo (Ctrl+Z)"))
        bar.append(button("", self.redo, icon="edit-redo-symbolic", tooltip="Redo (Ctrl+Shift+Z)"))
        return bar

    def _map_buttons(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.END,
                      valign=Gtk.Align.START, margin_end=10, margin_top=10)
        box.append(button("", lambda: self.map.zoom_at(1), ["osd", "circular"], icon="zoom-in-symbolic",
                          tooltip="Zoom in"))
        box.append(button("", lambda: self.map.zoom_at(-1), ["osd", "circular"], icon="zoom-out-symbolic",
                          tooltip="Zoom out"))
        box.append(map_type_button(self.shared))
        box.append(button("", self.fit, ["osd", "circular"], icon="zoom-fit-best-symbolic",
                          tooltip="Show the whole plan"))
        box.append(button("", self._goto_vehicle, ["osd", "circular"], icon="find-location-symbolic",
                          tooltip="Go to the vehicle"))
        return box

    def _side_header(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=10, margin_end=10,
                      margin_top=10)
        self.side_title = label("Mission", ["title-3"])
        box.append(self.side_title)
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.def_alt = Gtk.SpinButton.new_with_range(1, 10000, 1)
        self.def_alt.set_value(float(self.settings.get("default_alt", 50)))
        self.def_alt.connect("value-changed", lambda s: self.settings.set("default_alt", s.get_value()))
        self.frame_dd = Gtk.DropDown(model=Gtk.StringList.new([f[1] for f in FRAMES]), hexpand=True)
        grid.attach(label("New item altitude"), 0, 0, 1, 1)
        grid.attach(self.def_alt, 1, 0, 1, 1)
        grid.attach(label("m", ["dim-label"]), 2, 0, 1, 1)
        grid.attach(label("Altitude reference"), 0, 1, 1, 1)
        grid.attach(self.frame_dd, 1, 1, 2, 1)
        self.side_settings = grid
        box.append(grid)
        self.tool_btns = Gtk.Box(spacing=6, homogeneous=True)
        box.append(self.tool_btns)
        return box

    # ----------------------------------------------------------- mode
    def _set_mode(self, mode):
        self.mode = mode
        if not self.mode_btns[mode].get_active():
            self.mode_btns[mode].set_active(True)
        titles = {"mission": "Mission", "fence": "Geofence", "rally": "Rally points",
                  "polygon": "Survey area"}
        self.side_title.set_text(titles[mode])
        hints = {"mission": "Click to add a waypoint · drag to move · right-click for more",
                 "fence": "Click to add polygon corners · right-click for circles and the return point",
                 "rally": "Click to add a rally point (a safe place for RTL to go)",
                 "polygon": "Click to outline the survey area, then press 'Generate grid'"}
        self.hint.set_text(hints[mode])
        self.side_settings.set_visible(mode in ("mission", "rally", "polygon"))
        clear_box(self.tool_btns)
        if mode == "mission":
            self.tool_btns.append(button("Takeoff", self._add_takeoff, icon="go-up-symbolic"))
            self.tool_btns.append(button("RTL", lambda: self._append_cmd(20), icon="go-home-symbolic"))
            self.tool_btns.append(button("Land", self._add_land, icon="go-bottom-symbolic"))
            self.tool_btns.append(button("Clear", lambda: self.clear("mission"), icon="user-trash-symbolic",
                                         tooltip="Clear the mission"))
        elif mode == "fence":
            self.tool_btns.append(button("New polygon", lambda: self._new_fence_poly(FENCE_INCL_POLY)))
            self.tool_btns.append(button("Settings…", self._fence_enable_dialog))
            self.tool_btns.append(button("Clear", lambda: self.clear("fence"), icon="user-trash-symbolic",
                                         tooltip="Clear the geofence"))
        elif mode == "rally":
            self.tool_btns.append(button("Clear rally points", lambda: self.clear("rally"),
                                         icon="user-trash-symbolic"))
        elif mode == "polygon":
            self.tool_btns.append(button("Generate grid…", self._grid_dialog, ["suggested-action"]))
            self.tool_btns.append(button("Clear area", lambda: self.clear("polygon"), icon="user-trash-symbolic"))
        self.selected = set()
        self.refresh()

    def map_editable(self):
        return self.mode == "mission"

    def fence_editable(self):
        return self.mode == "fence"

    def rally_editable(self):
        return self.mode == "rally"

    def _frame(self):
        return FRAMES[self.frame_dd.get_selected()][0]

    # ---------------------------------------------------------- undo
    def _snapshot(self):
        return ([i.copy() for i in self.items], [i.copy() for i in self.fence],
                [i.copy() for i in self.rally], list(self.poly.points))

    def push_undo(self):
        self.undo_stack.append(self._snapshot())
        if len(self.undo_stack) > 100:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        self.dirty = True

    def _restore(self, snap):
        self.items, self.fence, self.rally, self.poly.points = snap
        self.selected = set()
        self.refresh()

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self._snapshot())
            self._restore(self.undo_stack.pop())

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self._snapshot())
            self._restore(self.redo_stack.pop())

    # ---------------------------------------------------------- editing
    def _home(self):
        h = self.items[0]
        if h.x == 0 and h.y == 0:
            st = self.v.state
            if st.home_lat is not None:
                return st.home_lat, st.home_lon
            return None
        return h.x, h.y

    def _home_from_vehicle(self, auto=False):
        st = self.v.state
        if st.home_lat is None:
            return
        h = self.items[0]
        if auto and (h.x or h.y):
            return
        h.x, h.y, h.z = st.home_lat, st.home_lon, st.home_alt or 0
        self.refresh()

    def _insert_index(self):
        if self.selected and self.mode == "mission":
            return max(self.selected) + 1
        return len(self.items)

    def add_waypoint(self, lat, lon, cmd=16, at=None):
        self.push_undo()
        if not (self.items[0].x or self.items[0].y):
            if self.v.state.home_lat is not None:
                self._home_from_vehicle()
            elif len(self.items) == 1:
                # no vehicle: the first click sets the planned home
                h = self.items[0]
                h.x, h.y = lat, lon
                self.refresh()
                toast("Planned home set here. Click again to add waypoints.")
                return
        it = MissionItem(cmd, lat, lon, self.def_alt.get_value(), frame=self._frame())
        idx = len(self.items) if at is None else at
        self.items.insert(idx, it)
        self.selected = {idx}
        self.refresh()

    def move_item(self, idx, lat, lon, live):
        if live:
            if not self._drag_undo:
                self.push_undo()
                self._drag_undo = True
            it = self.items[idx]
            it.x, it.y = lat, lon
            self._update_info()
        else:
            self._drag_undo = False
            self.refresh()

    def select_item(self, idx):
        self.selected = {idx}
        self.refresh()

    def _append_cmd(self, cmd):
        self.push_undo()
        idx = self._insert_index()
        self.items.insert(idx, MissionItem(cmd, 0, 0, 0, frame=self._frame()))
        self.selected = {idx}
        self.refresh()

    def _add_takeoff(self):
        for i, it in enumerate(self.items[1:], 1):
            if it.command in (22, 84):
                self.selected = {i}
                self.refresh()
                toast("The mission already has a takeoff (selected)")
                return
        self.push_undo()
        it = MissionItem(22, 0, 0, self.def_alt.get_value(), frame=self._frame())
        if self.v.state.vclass == "plane":
            it.p1 = 15      # minimum pitch
        self.items.insert(1, it)
        self.selected = {1}
        self.refresh()

    def _add_land(self):
        if len(self.items) > 1 and self.items[-1].command in (21, 85):
            self.selected = {len(self.items) - 1}
            self.refresh()
            toast("The mission already ends with a landing (selected)")
            return
        h = self._home()
        self.push_undo()
        self.items.append(MissionItem(21, h[0] if h else 0, h[1] if h else 0, 0, frame=self._frame()))
        self.selected = {len(self.items) - 1}
        self.refresh()

    def _delete_selected(self):
        if not self.selected:
            return
        if self.mode == "mission":
            sel = sorted(i for i in self.selected if i > 0)
            if not sel:
                return
            self.push_undo()
            for i in reversed(sel):
                del self.items[i]
        elif self.mode == "fence":
            self.push_undo()
            self._delete_fence(set(self.selected))
        elif self.mode == "rally":
            self.push_undo()
            for i in sorted(self.selected, reverse=True):
                del self.rally[i]
        elif self.mode == "polygon":
            self.push_undo()
            for i in sorted(self.selected, reverse=True):
                del self.poly.points[i]
        self.selected = set()
        self.refresh()

    def _move_selected(self, delta):
        if len(self.selected) != 1:
            return
        i = next(iter(self.selected))
        lst = self._current_list()
        j = i + delta
        lo = 1 if self.mode == "mission" else 0
        if i < lo or j < lo or j >= len(lst):
            return
        self.push_undo()
        lst[i], lst[j] = lst[j], lst[i]
        self.selected = {j}
        self.refresh()

    def _current_list(self):
        return {"mission": self.items, "fence": self.fence, "rally": self.rally,
                "polygon": self.poly.points}[self.mode]

    # ---------------------------------------------------------- fence
    def _fence_polys(self):
        """[(start_index, count, command)] for polygon groups."""
        out = []
        i = 0
        while i < len(self.fence):
            it = self.fence[i]
            if it.command in (FENCE_INCL_POLY, FENCE_EXCL_POLY):
                n = max(1, min(int(it.p1), len(self.fence) - i))
                out.append((i, n, it.command))
                i += n
            else:
                i += 1
        return out

    def _new_fence_poly(self, cmd):
        self._fence_new_kind = cmd
        toast("Click on the map to add the corners of the new "
              + ("inclusion" if cmd == FENCE_INCL_POLY else "exclusion") + " polygon")

    def _add_fence_vertex(self, lat, lon):
        self.push_undo()
        kind = self._fence_new_kind
        polys = self._fence_polys()
        if kind is None and polys:
            start, n, cmd = polys[-1]
        else:
            cmd = kind or FENCE_INCL_POLY
            start, n = len(self.fence), 0
            self._fence_new_kind = None
        self.fence.insert(start + n, MissionItem(cmd, lat, lon, 0, frame=0, mission_type=FENCE))
        for k in range(start, start + n + 1):
            self.fence[k].p1 = n + 1
            self.fence[k].command = cmd
        self.refresh()

    def _delete_fence(self, idxs):
        groups = {start: n for start, n, _c in self._fence_polys()}
        out = []
        i = 0
        while i < len(self.fence):
            if i in groups:
                keep = [self.fence[k] for k in range(i, i + groups[i]) if k not in idxs]
                for it in keep:
                    it.p1 = len(keep)
                out.extend(keep)
                i += groups[i]
                continue
            if i not in idxs:
                out.append(self.fence[i])
            i += 1
        self.fence = out

    def fence_changed(self):
        self.dirty = True
        self.refresh()

    def rally_changed(self):
        self.dirty = True
        self.refresh()

    def _fence_enable_dialog(self):
        if not self.v.state.connected:
            toast("Connect to a vehicle to change fence settings")
            return
        p = self.v.params
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sw = Gtk.Switch(active=bool(p.get("FENCE_ENABLE", 0)), halign=Gtk.Align.START)
        row = Gtk.Box(spacing=8)
        row.append(label("Fence enabled", hexpand=True))
        row.append(sw)
        box.append(row)
        alt_name = "FENCE_ALT_MAX" if "FENCE_ALT_MAX" in p.values or "FENCE_ALT_MAX_M" not in p.values else "FENCE_ALT_MAX_M"
        alt = Gtk.SpinButton.new_with_range(0, 10000, 1)
        alt.set_value(float(p.get(alt_name, 100) or 100))
        r2 = Gtk.Box(spacing=8)
        r2.append(label("Maximum altitude (m)", hexpand=True))
        r2.append(alt)
        box.append(r2)
        act = Gtk.DropDown(model=Gtk.StringList.new(["Report only", "RTL or Land", "Always Land",
                                                     "SmartRTL or RTL or Land", "Brake or Land",
                                                     "SmartRTL or Land"]))
        act.set_selected(min(5, int(p.get("FENCE_ACTION", 1) or 0)))
        r3 = Gtk.Box(spacing=8)
        r3.append(label("On breach", hexpand=True))
        r3.append(act)
        box.append(r3)

        def apply(r):
            if r != "ok":
                return
            vals = {"FENCE_ENABLE": 1 if sw.get_active() else 0, alt_name: alt.get_value(),
                    "FENCE_ACTION": act.get_selected()}

            def work():
                errs = []
                for k, val in vals.items():
                    if k in p.values:
                        ok, msg = p.set(k, val)
                        if not ok:
                            errs.append(msg)
                return errs
            background(work, lambda errs: toast("Fence settings saved" if not errs else "; ".join(errs)))
        alert(self.get_root(), "Geofence settings",
              "The fence shape is sent with 'Write'. These settings switch the fence on and choose what "
              "happens when it is breached.",
              (("cancel", "Cancel"), ("ok", "Save")), default="ok", on_response=apply, extra=box)

    # ---------------------------------------------------------- map input
    def _map_click(self, lat, lon, n, x, y):
        if self.mode == "mission":
            self.add_waypoint(lat, lon, at=self._insert_index())
        elif self.mode == "fence":
            self._add_fence_vertex(lat, lon)
        elif self.mode == "rally":
            self.push_undo()
            self.rally.append(MissionItem(5100, lat, lon, self.def_alt.get_value(), frame=3,
                                          mission_type=RALLY))
            self.selected = {len(self.rally) - 1}
            self.refresh()
        elif self.mode == "polygon":
            self.push_undo()
            self.poly.points.append((lat, lon))
            self.refresh()

    def _context(self, lat, lon, x, y):
        pop = Gtk.Popover(has_arrow=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        def add(text, cb):
            b = Gtk.Button(label=text, css_classes=["flat"])
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda *_: (pop.popdown(), cb()))
            box.append(b)

        if self.mode == "mission":
            add("Waypoint here", lambda: self.add_waypoint(lat, lon, at=self._insert_index()))
            add("Spline waypoint here", lambda: self.add_waypoint(lat, lon, 82, at=self._insert_index()))
            add("Loiter here (turns)…", lambda: self._loiter(lat, lon, 18))
            add("Loiter here (time)…", lambda: self._loiter(lat, lon, 19))
            add("Loiter here (until told otherwise)", lambda: self.add_waypoint(lat, lon, 17, at=self._insert_index()))
            add("Land here", lambda: self.add_waypoint(lat, lon, 21, at=self._insert_index()))
            add("Point camera here (ROI)", lambda: self.add_waypoint(lat, lon, 201, at=self._insert_index()))
            add("Set planned home here", lambda: self._set_home_here(lat, lon))
        elif self.mode == "fence":
            add("New inclusion polygon from here",
                lambda: (self._new_fence_poly(FENCE_INCL_POLY), self._add_fence_vertex(lat, lon)))
            add("New exclusion polygon from here",
                lambda: (self._new_fence_poly(FENCE_EXCL_POLY), self._add_fence_vertex(lat, lon)))
            add("Inclusion circle here…", lambda: self._fence_circle(lat, lon, FENCE_INCL_CIRC))
            add("Exclusion circle here…", lambda: self._fence_circle(lat, lon, FENCE_EXCL_CIRC))
            add("Return point here", lambda: self._fence_return(lat, lon))
        elif self.mode == "rally":
            add("Rally point here", lambda: self._map_click(lat, lon, 1, x, y))
        else:
            add("Add corner here", lambda: self._map_click(lat, lon, 1, x, y))
            add("Generate grid…", self._grid_dialog)
        box.append(Gtk.Separator())
        add("Delete selected", self._delete_selected)
        pop.set_child(box)
        pop.set_parent(self.map)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def _loiter(self, lat, lon, cmd):
        if cmd == 18:
            ask_number(self.get_root(), "Loiter turns", "How many circles?", 3,
                       lambda n: self._add_with(lat, lon, cmd, p1=n), lo=1, hi=255)
        else:
            ask_number(self.get_root(), "Loiter time", "How long to wait here?", 30,
                       lambda n: self._add_with(lat, lon, cmd, p1=n), lo=1, hi=3600, unit="s")

    def _add_with(self, lat, lon, cmd, **kw):
        self.add_waypoint(lat, lon, cmd, at=self._insert_index())
        if self.selected:
            it = self.items[max(self.selected)]
            for k, val in kw.items():
                setattr(it, k, val)
        self.refresh()

    def _set_home_here(self, lat, lon):
        self.push_undo()
        h = self.items[0]
        h.x, h.y = lat, lon
        self.refresh()

    def _fence_circle(self, lat, lon, cmd):
        def go(r):
            self.push_undo()
            self.fence.append(MissionItem(cmd, lat, lon, 0, p1=r, frame=0, mission_type=FENCE))
            self.refresh()
        ask_number(self.get_root(), "Circle radius", "Radius of the fence circle:", 100, go, lo=5,
                   hi=100000, unit="m")

    def _fence_return(self, lat, lon):
        self.push_undo()
        self.fence = [f for f in self.fence if f.command != FENCE_RETURN]
        self.fence.append(MissionItem(FENCE_RETURN, lat, lon, 0, frame=0, mission_type=FENCE))
        self.refresh()

    # ---------------------------------------------------------- survey grid
    def _grid_dialog(self):
        if len(self.poly.points) < 3:
            toast("Outline the survey area first: click at least 3 corners on the map")
            self._set_mode("polygon")
            return
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        spin = {}
        for r, (key, text, val, lo, hi) in enumerate((
                ("spacing", "Line spacing (m)", 20, 1, 1000),
                ("angle", "Line direction (°)", 0, 0, 359),
                ("alt", "Altitude (m)", self.def_alt.get_value(), 1, 5000),
                ("overshoot", "Turn margin (m)", 0, 0, 200))):
            s = Gtk.SpinButton.new_with_range(lo, hi, 1)
            s.set_value(val)
            spin[key] = s
            grid.attach(label(text), 0, r, 1, 1)
            grid.attach(s, 1, r, 1, 1)

        def go(resp):
            if resp != "ok":
                return
            pts = survey_grid(self.poly.points, spin["spacing"].get_value(), spin["angle"].get_value(),
                              spin["overshoot"].get_value())
            if not pts:
                toast("The area is too small for that line spacing")
                return
            self.push_undo()
            alt = spin["alt"].get_value()
            if not (self.items[0].x or self.items[0].y):
                if self.v.state.home_lat is not None:
                    self._home_from_vehicle()
                else:
                    self.items[0].x, self.items[0].y = pts[0]
            for la, lo in pts:
                self.items.append(MissionItem(16, la, lo, alt, frame=self._frame()))
            self._set_mode("mission")
            toast(f"Added {len(pts)} survey waypoints")
            self.fit()
        alert(self.get_root(), "Survey grid", "Fly back-and-forth lines across the outlined area.",
              (("cancel", "Cancel"), ("ok", "Generate")), default="ok", on_response=go, extra=grid)

    # ---------------------------------------------------------- list + editor
    def refresh(self):
        self._rebuild_list()
        self._build_editor()
        self._update_info()
        self.map.queue_draw()

    def _row(self, badge, title, subtitle):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(spacing=10, margin_top=4, margin_bottom=4, margin_start=4, margin_end=4)
        box.append(label(badge, ["wp-badge", "numeric"], xalign=0.5, valign=Gtk.Align.CENTER))
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        v.append(label(title, ["heading"]))
        v.append(label(subtitle, ["dim-label", "caption", "numeric"], ellipsize=3))
        box.append(v)
        row.set_child(box)
        return row

    def _rebuild_list(self):
        self._suspend = True
        clear_box(self.list)
        if self.mode == "polygon":
            for i, (la, lo) in enumerate(self.poly.points):
                self.list.append(self._row(str(i + 1), "Corner", f"{la:.6f}, {lo:.6f}"))
        elif self.mode == "mission":
            h = self.items[0]
            self.list.append(self._row("H", "Home", f"{h.x:.6f}, {h.y:.6f}" if (h.x or h.y)
                                       else "Not set — will come from the vehicle"))
            prev = self._home()
            for i, it in enumerate(self.items[1:], 1):
                sub = []
                if it.has_location:
                    sub.append(f"{it.z:.0f} m {FRAME_NAMES.get(it.frame, '').lower()}")
                    if prev:
                        sub.append(fmt_dist(haversine(prev[0], prev[1], it.x, it.y)))
                    prev = (it.x, it.y)
                elif it.command == 22:
                    sub.append(f"climb to {it.z:.0f} m")
                else:
                    lbls = self.labels.get(it.command)
                    if lbls and lbls[0]:
                        sub.append(f"{lbls[0]}: {it.p1:g}")
                self.list.append(self._row(str(i), command_short_name(it.command).replace("_", " ").title(),
                                           " · ".join(sub) or " "))
        elif self.mode == "fence":
            for i, it in enumerate(self.fence):
                name = FENCE_NAMES.get(it.command, command_short_name(it.command))
                sub = f"{it.x:.6f}, {it.y:.6f}"
                if it.command in (FENCE_INCL_CIRC, FENCE_EXCL_CIRC):
                    sub = f"radius {it.p1:.0f} m · " + sub
                self.list.append(self._row(str(i + 1), name, sub))
        elif self.mode == "rally":
            for i, it in enumerate(self.rally):
                self.list.append(self._row(f"R{i + 1}", "Rally point", f"{it.z:.0f} m · {it.x:.6f}, {it.y:.6f}"))
        for i in sorted(self.selected):
            row = self.list.get_row_at_index(i)
            if row:
                self.list.select_row(row)
        self._suspend = False

    def _list_pressed(self, gesture, n, x, y):
        row = self.list.get_row_at_y(int(y))
        if row is None:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        state = gesture.get_current_event_state()
        idx = row.get_index()
        if state & Gdk.ModifierType.SHIFT_MASK and self._anchor is not None:
            a, b = sorted((self._anchor, idx))
            sel = set(range(a, b + 1))
        elif state & Gdk.ModifierType.CONTROL_MASK:
            sel = set(self.selected) ^ {idx}
            self._anchor = idx
        else:
            sel = {idx}
            self._anchor = idx
        self._suspend = True
        self.list.unselect_all()
        for i in sorted(sel):
            r = self.list.get_row_at_index(i)
            if r:
                self.list.select_row(r)
        self._suspend = False
        self.list.grab_focus()
        self._list_selection(self.list)

    def _list_selection(self, lb):
        if self._suspend:
            return
        self.selected = {r.get_index() for r in lb.get_selected_rows()}
        self._build_editor()
        self.map.queue_draw()
        if len(self.selected) == 1:
            i = next(iter(self.selected))
            lst = self._current_list()
            if i < len(lst):
                it = lst[i]
                la, lo = (it if isinstance(it, tuple) else (it.x, it.y))
                if la or lo:
                    x, y = self.map.to_screen(la, lo)
                    if not (0 < x < self.map.get_width() and 0 < y < self.map.get_height()):
                        self.map.set_center(la, lo)

    def _build_editor(self):
        clear_box(self.editor_box)
        lst = self._current_list()
        sel = [i for i in sorted(self.selected) if i < len(lst)]
        if not sel:
            self.editor_box.append(label("Select an item to edit it.", ["dim-label"], margin_top=10))
            return
        actions = Gtk.Box(spacing=6, margin_top=6)
        actions.append(button("", lambda: self._move_selected(-1), icon="go-up-symbolic", tooltip="Move up"))
        actions.append(button("", lambda: self._move_selected(1), icon="go-down-symbolic", tooltip="Move down"))
        actions.append(button("Delete", self._delete_selected, ["destructive-action"],
                              icon="user-trash-symbolic", hexpand=True))
        if not (self.mode == "mission" and sel == [0]):
            self.editor_box.append(actions)
        if self.mode == "polygon":
            return
        if len(sel) > 1:
            self.editor_box.append(label(f"{len(sel)} items selected", ["dim-label"]))
            if self.mode == "mission":
                grp = Adw.PreferencesGroup()
                alt = Adw.SpinRow.new_with_range(0, 10000, 1)
                alt.set_title("Altitude for all selected")
                alt.set_value(self.items[sel[-1]].z)
                grp.add(alt)
                self.editor_box.append(grp)
                self.editor_box.append(button("Apply altitude", lambda: self._set_alt_many(sel, alt.get_value())))
            return
        i = sel[0]
        it = lst[i]
        grp = Adw.PreferencesGroup(margin_top=6)
        if self.mode == "mission" and i == 0:
            grp.set_title("Home")
            grp.set_description("Where the vehicle took off. Normally set by the vehicle itself.")
            self._num_row(grp, "Latitude", it, "x", -90, 90, 7)
            self._num_row(grp, "Longitude", it, "y", -180, 180, 7)
            self._num_row(grp, "Altitude (above sea level)", it, "z", -1000, 10000, 1)
            self.editor_box.append(grp)
            b = button("Use the vehicle's home", lambda: self._home_from_vehicle())
            b.set_sensitive(self.v.state.home_lat is not None)
            self.editor_box.append(b)
            return
        if self.mode == "mission":
            cmds = self._command_list()
            if it.command not in cmds:
                cmds.append(it.command)
            names = [command_short_name(c).replace("_", " ").title() for c in cmds]
            combo = Adw.ComboRow(title="Command", model=Gtk.StringList.new(names), enable_search=True)
            combo.set_selected(cmds.index(it.command))
            combo.connect("notify::selected", lambda c, *_: self._set_command(i, cmds[c.get_selected()]))
            grp.add(combo)
            from ..mavlink import defs
            cmd_doc = defs.ENUMS["MAV_CMD"][1].get(it.command, ("", ""))[1]
            combo.set_tooltip_text(short_description(cmd_doc) or None)
            pdocs = defs.CMD_PARAMS.get(it.command, {})
            labels = self.labels.get(it.command, ["", "", "", "", "Lat", "Long", "Alt"])
            for k, (attr, lbl) in enumerate(zip(("p1", "p2", "p3", "p4"), labels[:4]), 1):
                if lbl:
                    r = self._num_row(grp, lbl, it, attr, -1e7, 1e7, 2)
                    doc = pdocs.get(k)
                    if doc and (doc[2] or doc[0]):
                        r.set_tooltip_text(short_description(doc[2] or doc[0])
                                           + (f" ({doc[1]})" if doc[1] else ""))
            if labels[4] or it.has_location:
                self._num_row(grp, "Latitude", it, "x", -90, 90, 7)
                self._num_row(grp, "Longitude", it, "y", -180, 180, 7)
            if labels[6]:
                self._num_row(grp, "Altitude (m)", it, "z", -1000, 100000, 1)
                fidx = [f[0] for f in FRAMES]
                fr = Adw.ComboRow(title="Altitude reference", model=Gtk.StringList.new([f[1] for f in FRAMES]))
                fr.set_selected(fidx.index(it.frame) if it.frame in fidx else 0)
                fr.connect("notify::selected", lambda c, *_: self._set_attr(i, "frame", fidx[c.get_selected()]))
                grp.add(fr)
        elif self.mode == "fence":
            grp.set_title(FENCE_NAMES.get(it.command, "Fence item"))
            self._num_row(grp, "Latitude", it, "x", -90, 90, 7)
            self._num_row(grp, "Longitude", it, "y", -180, 180, 7)
            if it.command in (FENCE_INCL_CIRC, FENCE_EXCL_CIRC):
                self._num_row(grp, "Radius (m)", it, "p1", 1, 1e6, 0)
            if it.command in (FENCE_INCL_POLY, FENCE_EXCL_POLY):
                kind = Adw.ComboRow(title="Type", model=Gtk.StringList.new(
                    ["Inclusion (stay inside)", "Exclusion (keep out)"]))
                kind.set_selected(0 if it.command == FENCE_INCL_POLY else 1)
                readable(kind)
                kind.connect("notify::selected", lambda c, *_: self._set_poly_kind(
                    i, FENCE_INCL_POLY if c.get_selected() == 0 else FENCE_EXCL_POLY))
                grp.add(kind)
        elif self.mode == "rally":
            grp.set_title("Rally point")
            self._num_row(grp, "Latitude", it, "x", -90, 90, 7)
            self._num_row(grp, "Longitude", it, "y", -180, 180, 7)
            self._num_row(grp, "Altitude (m)", it, "z", 0, 10000, 0)
        self.editor_box.append(grp)

    def _num_row(self, grp, title, it, attr, lo, hi, digits):
        row = Adw.SpinRow.new_with_range(lo, hi, 10 ** -digits if digits else 1)
        row.set_digits(digits)
        row.set_title(title)
        row.set_value(float(getattr(it, attr)))
        row.connect("notify::value", lambda r, *_: self._spin_changed(it, attr, r.get_value()))
        grp.add(row)
        return row

    def _spin_changed(self, it, attr, value):
        if abs(float(getattr(it, attr)) - value) < 1e-9:
            return
        self.push_undo()
        setattr(it, attr, value)
        self._rebuild_list()
        self._update_info()
        self.map.queue_draw()

    def _set_attr(self, i, attr, val):
        it = self._current_list()[i]
        if getattr(it, attr) == val:
            return
        self.push_undo()
        setattr(it, attr, val)
        GLib.idle_add(lambda: self.refresh() and False)

    def _set_command(self, i, cmd):
        it = self.items[i]
        if it.command == cmd:
            return
        self.push_undo()
        it.command = cmd
        if cmd in (22, 20):
            # takeoff climbs straight up from where the vehicle is; RTL has no position
            it.x = it.y = 0.0
        elif cmd in (16, 82, 17, 18, 19, 21, 31, 201) and not (it.x or it.y):
            it.x, it.y = self._nearby_position(i)
        if cmd == 20:
            it.z = 0.0
        elif cmd in (16, 82, 17, 18, 19, 31, 22) and it.z <= 0:
            it.z = self.def_alt.get_value()
        GLib.idle_add(lambda: self.refresh() and False)

    def _nearby_position(self, i):
        """A sensible spot for an item that just gained a position: the previous
        waypoint, else the next one, else home, else the middle of the map."""
        for k in list(range(i - 1, 0, -1)) + list(range(i + 1, len(self.items))):
            o = self.items[k]
            if o.has_location:
                return o.x, o.y
        h = self._home()
        return h if h else (self.map.lat, self.map.lon)

    def _set_poly_kind(self, i, cmd):
        for start, n, _c in self._fence_polys():
            if start <= i < start + n:
                self.push_undo()
                for k in range(start, start + n):
                    self.fence[k].command = cmd
        GLib.idle_add(lambda: self.refresh() and False)

    def _set_alt_many(self, sel, alt):
        self.push_undo()
        for i in sel:
            if i > 0:
                self.items[i].z = alt
        self.refresh()

    def _command_list(self):
        common = [16, 22, 21, 20, 17, 18, 19, 82, 93, 178, 177, 201, 197, 183, 181, 206, 203,
                  205, 112, 114, 115, 211, 189, 179, 31, 84, 85, 3000, 92]
        allowed = set(self.labels)
        out = [c for c in common if c in allowed or c in (16, 22, 21, 20)]
        out += sorted(c for c in allowed if c not in out)
        return out

    def _update_info(self):
        total = 0.0
        prev = self._home()
        for it in self.items[1:]:
            if it.has_location:
                if prev:
                    total += haversine(prev[0], prev[1], it.x, it.y)
                prev = (it.x, it.y)
        n = len(self.items) - 1
        if self.v.state.vclass == "plane":
            speed = (self.v.params.get("AIRSPEED_CRUISE") or self.v.params.get("TRIM_ARSPD_CM", 1500) / 100.0)
        else:
            speed = (self.v.params.get("WPNAV_SPEED") or 500) / 100.0
        eta = total / max(0.5, float(speed))
        txt = f"{n} items · {fmt_dist(total)} · about {int(eta // 60)} min {int(eta % 60)} s"
        if self.dirty:
            txt += " · not written to vehicle"
        h = self._home()
        if self.map.mouse_latlon and h:
            la, lo = self.map.mouse_latlon
            txt += (f"\nCursor {la:.6f}, {lo:.6f} · {fmt_dist(haversine(h[0], h[1], la, lo))} "
                    f"from home at {bearing(h[0], h[1], la, lo):.0f}°")
        if self.info.get_text() != txt:
            self.info.set_text(txt)

    def _tick(self):
        if not self._paned_set and self._paned.get_width() > 0:
            self._paned.set_position(max(400, self._paned.get_width() - 390))
            self._paned_set = True
        if self.get_mapped():
            self._update_info()
        return True

    # ---------------------------------------------------------- view
    def fit(self):
        pts = [(it.x, it.y) for it in self.items if it.x or it.y]
        pts += [(it.x, it.y) for it in self.fence + self.rally]
        pts += list(self.poly.points)
        if pts:
            self.map.fit(pts)

    def _goto_vehicle(self):
        st = self.v.state
        if st.has_position:
            self.map.set_center(st.lat, st.lon)

    def on_shown(self):
        if not any(it.x or it.y for it in self.items) and self.v.state.has_position:
            self.map.set_center(self.v.state.lat, self.v.state.lon)

    def _vehicle_class_changed(self):
        self.labels = CommandLabels.for_class(self.v.state.vclass)
        self.refresh()

    def _shared_changed(self, source=None):
        if source != "vehicle":
            return
        if self.dirty and len(self.items) > 1:
            toast("Your unsaved plan was kept. Press Read to load the vehicle's plan instead.")
            return
        self._load_from_shared()

    def _load_from_shared(self):
        sh = self.shared
        self.items = [i.copy() for i in sh.mission] or [MissionItem(16, 0, 0, 0, frame=0)]
        self.fence = [i.copy() for i in sh.fence]
        self.rally = [i.copy() for i in sh.rally]
        self.dirty = False
        self.selected = set()
        self._home_from_vehicle(auto=True)
        self.refresh()

    # ---------------------------------------------------------- vehicle IO
    def read_from_vehicle(self):
        if not self.v.state.connected:
            toast("Connect to a vehicle first")
            return
        win = self.get_root()
        cancel = threading.Event()
        dlg = ProgressDialog("Reading the plan", on_cancel=cancel.set)
        dlg.status("Reading the plan from the aircraft", "Mission, geofence and rally points")
        dlg.present(win)

        def work():
            out = {"errors": []}
            for name, title, mt in (("mission", "Mission", MISSION), ("fence", "Geofence", FENCE),
                                    ("rally", "Rally points", RALLY)):
                h = [None]
                idle(lambda h=h, t=title: h.__setitem__(0, dlg.step(f"{t}: reading…")))

                def prog(a, b, t=title, h=h):
                    idle(lambda h=h: (dlg.progress(a / max(1, b), f"{t}: item {a} of {b}"),
                                  h[0] and dlg.update_step(h[0], f"{t}: item {a} of {b}")))
                try:
                    items = self.v.mission.download(mt, progress=prog, cancel=cancel)
                    out[name] = items
                    n = len(items) - (1 if mt == MISSION and items else 0)
                    idle(lambda h=h, t=title, n=n: h[0] and dlg.update_step(
                        h[0], f"{t}: {n} item{'s' if n != 1 else ''}", "ok"))
                except RuntimeError as e:
                    out[name] = None
                    out["errors"].append(f"{title}: {e}")
                    idle(lambda h=h, t=title, e=e: h[0] and dlg.update_step(h[0], f"{t}: {e}", "fail"))
                    if cancel.is_set():
                        break
            return out

        def done(out):
            if cancel.is_set():
                return
            self.push_undo()
            for name in ("mission", "fence", "rally"):
                if out.get(name) is not None:
                    setattr(self.shared, name, out[name])
            self._load_from_shared()
            self.shared.hub.emit("mission-changed", "planner")
            summary = (f"{max(0, len(self.items) - 1)} mission items, {len(self.fence)} fence items, "
                       f"{len(self.rally)} rally points")
            if out["errors"]:
                dlg.finish("Read finished with problems", summary, ok=False)
            else:
                dlg.finish("Plan read from the aircraft", summary, ok=True)
            self.fit()
        background(work, done, lambda e: dlg.finish("Read failed", str(e), ok=False))

    def write_to_vehicle(self):
        if not self.v.state.connected:
            toast("Connect to a vehicle first")
            return
        if self.v.state.armed and self.v.state.mode == "AUTO":
            confirm(self.get_root(), "The vehicle is flying a mission",
                    "Writing a new mission now changes what it does immediately. Continue?",
                    "Write", self._do_write, destructive=True)
            return
        self._do_write()

    def _do_write(self):
        win = self.get_root()
        items = [i.copy() for i in self.items]
        h0 = items[0]
        if not (h0.x or h0.y) and self.v.state.home_lat is not None:
            h0.x, h0.y, h0.z = self.v.state.home_lat, self.v.state.home_lon, self.v.state.home_alt or 0
        h0.command, h0.frame = 16, 0
        fence = [i.copy() for i in self.fence]
        rally = [i.copy() for i in self.rally]
        jobs = [("Mission", items, MISSION, len(items) - 1)]
        if fence or self.shared.fence:
            jobs.append(("Geofence", fence, FENCE, len(fence)))
        if rally or self.shared.rally:
            jobs.append(("Rally points", rally, RALLY, len(rally)))
        cancel = threading.Event()
        dlg = ProgressDialog("Writing the plan", on_cancel=cancel.set)
        dlg.status("Writing the plan to the aircraft", ", ".join(j[0].lower() for j in jobs))
        dlg.present(win)

        def work():
            res = []
            for title, its, mt, n in jobs:
                h = [None]
                idle(lambda h=h, t=title: h.__setitem__(0, dlg.step(f"{t}: writing…")))

                def prog(a, b, t=title, h=h):
                    idle(lambda h=h: (dlg.progress(a / max(1, b), f"{t}: item {a} of {b}"),
                                  h[0] and dlg.update_step(h[0], f"{t}: item {a} of {b}")))
                try:
                    self.v.mission.upload(its, mt, progress=prog, cancel=cancel)
                except RuntimeError as e:
                    idle(lambda h=h, t=title, e=e: h[0] and dlg.update_step(h[0], f"{t}: {e}", "fail"))
                    raise
                idle(lambda h=h, t=title, n=n: h[0] and dlg.update_step(
                    h[0], f"{t}: {n} item{'s' if n != 1 else ''} written and confirmed", "ok"))
                res.append(f"{title.lower()} ({n})")
            self.v.request_message(242)
            return res

        def done(res):
            self.shared.mission = items
            self.shared.fence = fence
            self.shared.rally = rally
            self.dirty = False
            self.shared.hub.emit("mission-changed", "planner")
            self._update_info()
            dlg.finish("Plan written to the aircraft", "The aircraft confirmed: " + ", ".join(res), ok=True)

        def err(e):
            if cancel.is_set():
                return
            dlg.finish("Write failed", f"{e}\n\nThe aircraft may still have its previous plan. "
                       "Check the link and press Write again.", ok=False)
        background(work, done, err)

    # ---------------------------------------------------------- files
    def load_file(self):
        if self.mode == "polygon":
            filters = [("Polygon files", ["*.poly", "*.txt"]), ("All files", ["*"])]
        else:
            filters = [("Waypoint files", ["*.waypoints", "*.txt"]), ("All files", ["*"])]

        def got(path):
            try:
                if self.mode == "polygon":
                    pts = load_poly(path)
                    self.push_undo()
                    self.poly.points = pts
                else:
                    items = load_waypoints(path)
                    self.push_undo()
                    if self.mode == "fence":
                        self.fence = [i for i in items if i.command in FENCE_NAMES]
                    elif self.mode == "rally":
                        self.rally = [i for i in items if i.command == 5100]
                    elif items and items[0].seq == 0:
                        self.items = items
                    else:
                        self.items = [MissionItem(16, 0, 0, 0, frame=0)] + items
            except (OSError, ValueError, IndexError) as e:
                alert(self.get_root(), "Could not open the file", str(e))
                return
            self.selected = set()
            self.refresh()
            self.fit()
            toast(f"Loaded {os.path.basename(path)}")
        file_dialog(self.get_root(), "Open", filters=filters, on_file=got, folder=config.data_dir())

    def save_file(self):
        def got(path):
            try:
                if self.mode == "polygon":
                    save_poly(path, self.poly.points)
                elif self.mode == "fence":
                    save_waypoints(path, [MissionItem(16, 0, 0, 0, frame=0)] + self.fence)
                elif self.mode == "rally":
                    save_waypoints(path, [MissionItem(16, 0, 0, 0, frame=0)] + self.rally)
                else:
                    save_waypoints(path, self.items)
                toast(f"Saved {os.path.basename(path)}")
            except OSError as e:
                alert(self.get_root(), "Could not save", str(e))
        name = {"polygon": "area.poly", "fence": "fence.waypoints",
                "rally": "rally.waypoints"}.get(self.mode, "mission.waypoints")
        file_dialog(self.get_root(), "Save", save=True, initial_name=name, on_file=got,
                    folder=config.data_dir())

    def clear(self, what):
        """what: mission | fence | rally | polygon | all"""
        names = {"mission": "the mission", "fence": "the geofence", "rally": "the rally points",
                 "polygon": "the survey area", "all": "everything (mission, geofence, rally points and survey area)"}
        empty = {"mission": len(self.items) <= 1, "fence": not self.fence, "rally": not self.rally,
                 "polygon": not self.poly.points}
        connected = self.v.state.connected
        if what != "all" and empty[what] and not (connected and what != "polygon"):
            toast(f"{names[what][0].upper() + names[what][1:]} is already empty")
            return

        def do_plan():
            self.push_undo()
            if what in ("mission", "all"):
                self.items = [self.items[0]]
            if what in ("fence", "all"):
                self.fence = []
            if what in ("rally", "all"):
                self.rally = []
            if what in ("polygon", "all"):
                self.poly.points = []
            self.selected = set()
            self.refresh()

        def do_vehicle():
            kinds = {"mission": [MISSION], "fence": [FENCE], "rally": [RALLY],
                     "all": [MISSION, FENCE, RALLY]}.get(what, [])

            def work():
                failed = [k for k in kinds if not self.v.mission.clear(k)]
                return failed

            def done(failed):
                if MISSION in kinds:
                    self.shared.mission = []
                if FENCE in kinds:
                    self.shared.fence = []
                if RALLY in kinds:
                    self.shared.rally = []
                self.shared.hub.emit("mission-changed", "planner")
                self.dirty = False
                self._update_info()
                toast("Cleared on the vehicle too" if not failed else
                      "The plan was cleared, but the vehicle did not confirm clearing its copy")
            background(work, done)

        def respond(r):
            if r == "plan":
                do_plan()
            elif r == "both":
                do_plan()
                do_vehicle()

        buttons = [("cancel", "Cancel"), ("plan", "Clear plan")]
        body = f"Remove {names[what]} from the plan? You can undo this."
        if connected and what != "polygon":
            buttons.append(("both", "Clear plan and vehicle"))
            body += ("\n\n“Clear plan and vehicle” also erases the vehicle's copy straight away. "
                     "“Clear plan” leaves the vehicle unchanged until you press Write.")
        alert(self.get_root(), "Clear " + ("everything" if what == "all" else names[what].replace("the ", "")) + "?",
              body, tuple(buttons), default="plan", destructive="both" if connected else "plan",
              on_response=respond)


# ------------------------------------------------------------- survey grid
def survey_grid(poly, spacing, angle_deg, overshoot=0.0):
    """Lawn-mower waypoints covering polygon [(lat, lon)], using local flat-earth maths."""
    if len(poly) < 3 or spacing <= 0:
        return []
    lat0 = sum(p[0] for p in poly) / len(poly)
    lon0 = sum(p[1] for p in poly) / len(poly)
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110540.0
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)

    def to_local(p):
        x, y = (p[1] - lon0) * kx, (p[0] - lat0) * ky
        return x * ca - y * sa, x * sa + y * ca

    def to_global(u, v):
        x = u * ca + v * sa
        y = -u * sa + v * ca
        return lat0 + y / ky, lon0 + x / kx

    pts = [to_local(p) for p in poly]
    us = [p[0] for p in pts]
    out = []
    u = min(us) + spacing / 2
    flip = False
    n = len(pts)
    while u < max(us):
        ys = []
        for i in range(n):
            (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n]
            if (x1 <= u < x2) or (x2 <= u < x1):
                t = (u - x1) / (x2 - x1)
                ys.append(y1 + t * (y2 - y1))
        ys.sort()
        for k in range(0, len(ys) - 1, 2):
            seg = [(u, ys[k] - overshoot), (u, ys[k + 1] + overshoot)]
            if flip:
                seg.reverse()
            out.extend(to_global(*s) for s in seg)
            flip = not flip
        u += spacing
    return out
