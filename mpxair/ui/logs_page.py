"""Logs page: download onboard logs, review .bin logs (graphs, map, messages), replay .tlog."""
import os
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk  # noqa: E402

from ..core import config, modes  # noqa: E402
from ..core.logs import LogDownloader, parse_bin  # noqa: E402
from .graph import Graph  # noqa: E402
from .mapview import MapView, draw_marker  # noqa: E402
from .util import (alert, background, button, clear_box, confirm, file_dialog, fmt_time, idle,  # noqa: E402
                   label, toast)

PRESETS = [
    ("Attitude", [("ATT", "Roll"), ("ATT", "DesRoll"), ("ATT", "Pitch"), ("ATT", "DesPitch")]),
    ("Altitude", [("CTUN", "Alt"), ("CTUN", "DAlt"), ("BARO", "Alt"), ("GPS", "Alt")]),
    ("Battery", [("BAT", "Volt"), ("BAT", "Curr")]),
    ("Vibration", [("VIBE", "VibeX"), ("VIBE", "VibeY"), ("VIBE", "VibeZ")]),
    ("GPS", [("GPS", "NSats"), ("GPS", "HDop"), ("GPS", "Spd")]),
    ("Throttle", [("CTUN", "ThI"), ("CTUN", "ThO"), ("RCOU", "C3")]),
]


class TrackLayer:
    def __init__(self):
        self.points = []

    def draw(self, cr, view):
        if len(self.points) < 2:
            return
        cr.set_line_width(3)
        cr.set_source_rgba(0.95, 0.25, 0.75, 0.95)
        x, y = view.to_screen(*self.points[0])
        cr.move_to(x, y)
        step = max(1, len(self.points) // 4000)
        for p in self.points[::step]:
            cr.line_to(*view.to_screen(*p))
        cr.stroke()
        draw_marker(cr, x, y, "S", (0.2, 0.7, 0.35), r=9)
        ex, ey = view.to_screen(*self.points[-1])
        draw_marker(cr, ex, ey, "E", (0.85, 0.3, 0.2), r=9)


class LogsPage(Gtk.Box):
    def __init__(self, shared):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.shared = shared
        self.v = shared.vehicle
        self.log = None
        self.cancel = threading.Event()
        self.stack = Adw.ViewStack(vexpand=True)
        sw = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE,
                              halign=Gtk.Align.CENTER, margin_top=8, margin_bottom=6)
        self.append(sw)
        self.append(self.stack)
        self.stack.add_titled_with_icon(self._vehicle_tab(), "vehicle", "Download from vehicle",
                                        "folder-download-symbolic")
        self.stack.add_titled_with_icon(self._review_tab(), "review", "Review a log", "utilities-system-monitor-symbolic")
        self.stack.add_titled_with_icon(self._tlog_tab(), "tlog", "Replay telemetry", "media-playback-start-symbolic")

    # ============================================================ download tab
    def _vehicle_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=24, margin_end=24,
                      margin_top=12, margin_bottom=12)
        clamp = Adw.Clamp(maximum_size=900)
        clamp.set_child(box)
        box.append(label("Logs stored on the vehicle", ["title-2"]))
        box.append(label("The autopilot records a detailed log (.bin) on its SD card every time it is armed. "
                         f"Downloaded logs are saved in {config.log_dir()}.", ["dim-label"], wrap=True))
        bar = Gtk.Box(spacing=8)
        bar.append(button("List logs", self.list_logs, ["suggested-action"], icon="view-refresh-symbolic"))
        self.dl_btn = button("Download selected", self.download_selected, icon="folder-download-symbolic")
        bar.append(self.dl_btn)
        bar.append(button("Download all", self.download_all))
        bar.append(button("Erase all…", self.erase_all, ["destructive-action"]))
        bar.append(button("Open folder", lambda: Gtk.FileLauncher.new(
            Gio.File.new_for_path(config.log_dir())).launch(self.get_root(), None, None)))
        box.append(bar)
        self.loglist = Gtk.ListBox(selection_mode=Gtk.SelectionMode.MULTIPLE, css_classes=["boxed-list"])
        self.loglist.set_placeholder(label("Press “List logs” while connected.", ["dim-label"], xalign=0.5,
                                           margin_top=16, margin_bottom=16))
        box.append(self.loglist)
        self.dl_progress = Gtk.ProgressBar(show_text=True, visible=False)
        box.append(self.dl_progress)
        self.entries = []
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(clamp)
        return sc

    def list_logs(self):
        if not self.v.state.connected:
            toast("Connect to a vehicle first")
            return

        def done(entries):
            self.entries = entries
            clear_box(self.loglist)
            for lid, size, ts in entries:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts > 1e8 else "time unknown"
                row = Adw.ActionRow(title=f"Log {lid}", subtitle=f"{when} · {size / 1e6:.1f} MB")
                self.loglist.append(row)
            if not entries:
                toast("No logs on the vehicle")
        background(lambda: LogDownloader(self.v).list(), done, lambda e: toast(str(e)))

    def download_selected(self):
        rows = self.loglist.get_selected_rows()
        if not rows:
            toast("Select one or more logs first")
            return
        self._download([self.entries[r.get_index()] for r in rows])

    def download_all(self):
        if self.entries:
            self._download(list(self.entries))

    def _download(self, entries):
        self.dl_progress.set_visible(True)
        self.cancel.clear()

        def work():
            out = []
            dl = LogDownloader(self.v)
            for k, (lid, size, ts) in enumerate(entries):
                stamp = time.strftime("%Y-%m-%d %H-%M-%S", time.localtime(ts)) if ts > 1e8 else "unknown"
                path = os.path.join(config.log_dir(), f"{stamp} log{lid}.bin")

                def prog(a, b, k=k):
                    idle(lambda: (self.dl_progress.set_fraction(a / b if b else 0),
                                  self.dl_progress.set_text(f"Log {lid} ({k + 1}/{len(entries)}): "
                                                            f"{a / 1e6:.1f} of {b / 1e6:.1f} MB")))
                dl.download(lid, size, path, prog, self.cancel)
                out.append(path)
            return out

        def done(paths):
            self.dl_progress.set_text(f"Downloaded {len(paths)} log{'s' if len(paths) != 1 else ''}")
            if len(paths) == 1:
                confirm(self.get_root(), "Log downloaded", f"Saved as {os.path.basename(paths[0])}. Open it now?",
                        "Review", lambda: self.open_bin(paths[0]))
        background(work, done, lambda e: (self.dl_progress.set_text(f"Failed: {e}"), toast(str(e))))

    def erase_all(self):
        if not self.v.state.connected:
            toast("Connect to a vehicle first")
            return
        confirm(self.get_root(), "Erase every log on the vehicle?",
                "This cannot be undone. Download anything you want to keep first.", "Erase",
                lambda: background(lambda: LogDownloader(self.v).erase(),
                                   lambda _r: (toast("Logs erased"), clear_box(self.loglist))),
                destructive=True)

    # ============================================================== review tab
    def _review_tab(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_bottom=6)
        bar.append(button("Open .bin log…", self._pick_bin, ["suggested-action"], icon="document-open-symbolic"))
        self.log_title = label("No log open", ["dim-label"], hexpand=True, ellipsize=3)
        bar.append(self.log_title)
        bar.append(label("Quick graphs:", ["dim-label"]))
        for name, fields in PRESETS:
            bar.append(button(name, lambda f=fields: self._preset(f), ["flat"]))
        bar.append(button("Clear", self._clear_graph, ["flat"]))
        outer.append(bar)
        self.parse_progress = Gtk.ProgressBar(visible=False)
        outer.append(self.parse_progress)

        paned = Gtk.Paned(vexpand=True, shrink_start_child=False, resize_start_child=False)
        outer.append(paned)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["side-panel"])
        left.set_size_request(280, -1)
        self.field_search = Gtk.SearchEntry(placeholder_text="Filter messages…", margin_start=8, margin_end=8,
                                            margin_top=8)
        self.field_search.connect("search-changed", lambda *_: self._fill_fields())
        left.append(self.field_search)
        self.fields_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=6, margin_end=6)
        fsc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        fsc.set_child(self.fields_box)
        left.append(fsc)
        paned.set_start_child(left)

        right = Adw.ViewStack()
        rsw = Adw.ViewSwitcher(stack=right, policy=Adw.ViewSwitcherPolicy.WIDE, halign=Gtk.Align.CENTER)
        rbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        rbox.append(rsw)
        rbox.append(right)
        self.graph = Graph()
        right.add_titled_with_icon(self.graph, "graph", "Graph", "utilities-system-monitor-symbolic")
        self.track = TrackLayer()
        self.log_map = MapView(self.shared.tex_cache, self.shared.settings)
        self.log_map.layers = [self.track]
        right.add_titled_with_icon(self.log_map, "map", "Flight path", "mark-location-symbolic")
        self.msg_view = Gtk.TextView(editable=False, monospace=True, left_margin=10, top_margin=8)
        msc = Gtk.ScrolledWindow()
        msc.set_child(self.msg_view)
        right.add_titled_with_icon(msc, "messages", "Messages", "chat-bubbles-text-symbolic")
        self.parm_view = Gtk.TextView(editable=False, monospace=True, left_margin=10, top_margin=8)
        psc = Gtk.ScrolledWindow()
        psc.set_child(self.parm_view)
        right.add_titled_with_icon(psc, "params", "Parameters", "view-list-symbolic")
        self.review_stack = right
        paned.set_end_child(rbox)
        self.selected_fields = []
        self.checks = {}
        return outer

    def _pick_bin(self):
        file_dialog(self.get_root(), "Open a log", filters=[("DataFlash logs", ["*.bin", "*.BIN"]),
                                                            ("All files", ["*"])],
                    on_file=self.open_bin, folder=config.log_dir())

    def open_bin(self, path):
        self.stack.set_visible_child_name("review")
        self.parse_progress.set_visible(True)
        self.log_title.set_text(f"Reading {os.path.basename(path)}…")

        def work():
            return parse_bin(path, lambda a, b: idle(self.parse_progress.set_fraction, a / max(1, b)))

        def done(log):
            self.parse_progress.set_visible(False)
            self.log = log
            vclass = "copter"
            for _t, m in log.messages[:30]:
                ml = m.lower()
                for k, c in (("arduplane", "plane"), ("rover", "rover"), ("ardusub", "sub"), ("tracker", "tracker")):
                    if k in ml:
                        vclass = c
            self.log_vclass = vclass
            size = os.path.getsize(path) / 1e6
            self.log_title.set_text(f"{os.path.basename(path)} · {size:.1f} MB · {fmt_time(log.duration)} · "
                                    f"{len(log.data)} message types")
            self.selected_fields = []
            self._fill_fields()
            self.track.points = log.gps_track()
            if self.track.points:
                self.log_map.fit(self.track.points)
            buf = self.msg_view.get_buffer()
            buf.set_text("\n".join(f"{t:9.2f} s  {m}" for t, m in log.messages))
            mt = modes.mode_table(vclass)
            mode_lines = [f"{t:9.2f} s  MODE → {mt.get(int(m), m)}" for t, m in log.modes]
            if mode_lines:
                buf.insert(buf.get_end_iter(), "\n\n" + "\n".join(mode_lines))
            self.parm_view.get_buffer().set_text(
                "\n".join(f"{k:<20} {v:g}" for k, v in sorted(log.params.items())))
            self._preset(PRESETS[0][1] if "ATT" in log.data else [])

        def err(e):
            self.parse_progress.set_visible(False)
            self.log_title.set_text("Could not read the log")
            alert(self.get_root(), "Could not read the log", str(e))
        background(work, done, err)

    def _fill_fields(self):
        clear_box(self.fields_box)
        self.checks = {}
        if not self.log:
            return
        q = self.field_search.get_text().strip().upper()
        for name in sorted(self.log.data):
            d = self.log.data[name]
            fields = [c for c in d["cols"] if c != "TimeUS"]
            if q and q not in name and not any(q in f.upper() for f in fields):
                continue
            exp = Gtk.Expander(label=f"{name}  ({len(d['rows'])})")
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=14)
            for f in fields:
                if d["rows"] and not isinstance(d["rows"][0][d["cols"].index(f)], (int, float)):
                    continue
                cb = Gtk.CheckButton(label=f, active=(name, f) in self.selected_fields)
                cb.connect("toggled", lambda b, n=name, f=f: self._toggle_field(n, f, b.get_active()))
                inner.append(cb)
                self.checks[(name, f)] = cb
            exp.set_child(inner)
            if any(sel[0] == name for sel in self.selected_fields) or (q and len(q) > 1):
                exp.set_expanded(True)
            self.fields_box.append(exp)

    def _toggle_field(self, name, field, on):
        key = (name, field)
        if on and key not in self.selected_fields:
            self.selected_fields.append(key)
        elif not on and key in self.selected_fields:
            self.selected_fields.remove(key)
        self._replot()

    def _preset(self, fields):
        if not self.log:
            toast("Open a log first")
            return
        self.selected_fields = [f for f in fields if f[0] in self.log.data and f[1] in self.log.data[f[0]]["cols"]]
        for key, cb in self.checks.items():
            cb.set_active(key in self.selected_fields)
        self._replot()
        self.review_stack.set_visible_child_name("graph")

    def _clear_graph(self):
        self.selected_fields = []
        for cb in self.checks.values():
            cb.set_active(False)
        self._replot()

    def _replot(self):
        if not self.log:
            return
        series = []
        for name, f in self.selected_fields:
            xs, ys = self.log.series(name, f)
            series.append((f"{name}.{f}", xs, ys))
        mt = modes.mode_table(getattr(self, "log_vclass", "copter"))
        events = [(t, mt.get(int(m), m)) for t, m in self.log.modes]
        self.graph.dark = Adw.StyleManager.get_default().get_dark()
        self.graph.set_series(series, events)

    # ================================================================ tlog tab
    def _tlog_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=24, margin_end=24,
                      margin_top=12)
        clamp = Adw.Clamp(maximum_size=760)
        clamp.set_child(box)
        box.append(label("Replay a telemetry log", ["title-2"]))
        box.append(label("MPXAir records everything the vehicle sends while connected into a .tlog file "
                         f"(in {config.tlog_dir()}). Replaying one shows the flight again on the Fly screen, "
                         "with the HUD, map and status exactly as they were.", ["dim-label"], wrap=True))
        box.append(button("Open a .tlog file…", self.open_tlog, ["suggested-action", "pill"], halign=Gtk.Align.START))
        self.recent = Gtk.ListBox(css_classes=["boxed-list"])
        box.append(label("Recent recordings", ["heading"], margin_top=8))
        box.append(self.recent)
        self._fill_recent()
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sc.set_child(clamp)
        return sc

    def _fill_recent(self):
        clear_box(self.recent)
        try:
            files = sorted((f for f in os.listdir(config.tlog_dir()) if f.endswith(".tlog")), reverse=True)[:10]
        except OSError:
            files = []
        for f in files:
            p = os.path.join(config.tlog_dir(), f)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            row = Adw.ActionRow(title=f, subtitle=f"{size / 1e6:.2f} MB", activatable=True)
            row.add_suffix(Gtk.Image(icon_name="media-playback-start-symbolic"))
            row.connect("activated", lambda _r, p=p: self._replay(p))
            self.recent.append(row)
        if not files:
            self.recent.append(Adw.ActionRow(title="No recordings yet"))

    def on_shown(self):
        self._fill_recent()

    def open_tlog(self):
        file_dialog(self.get_root(), "Open a telemetry log", filters=[("Telemetry logs", ["*.tlog"])],
                    on_file=self._replay, folder=config.tlog_dir())

    def _replay(self, path):
        if self.v.is_open and getattr(self.v.transport, "kind", "") != "tlog":
            toast("Disconnect from the vehicle before replaying a log")
            return
        win = self.get_root()

        def work():
            self.v.connect({"type": "tlog", "path": path, "speed": 1.0})
            if not self.v.wait_heartbeat(10):
                self.v.disconnect()
                raise RuntimeError("No vehicle heartbeat found in this log.")
            return True

        def done(_r):
            self.shared.hub.emit("replay-started", path)
            win.stack.set_visible_child_name("fly")
            toast("Replaying — use the controls at the bottom of the map")
        background(work, done, lambda e: alert(win, "Can't replay this file", str(e)))
