"""Setup page: firmware, frame, calibrations, flight modes, failsafes, battery, motor test."""
import os
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..core import firmware as fwmod  # noqa: E402
from ..core import modes  # noqa: E402
from ..core.params import format_value  # noqa: E402
from .motors import MotorDiagram, find_layout, letter  # noqa: E402
from .paramform import ParamForm  # noqa: E402
from .util import (alert, background, button, clear_box, confirm, file_dialog, idle,  # noqa: E402
                   label, toast)


def page_box():
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=24, margin_end=24,
                  margin_top=18, margin_bottom=24)
    clamp = Adw.Clamp(maximum_size=860, tightening_threshold=600)
    clamp.set_child(box)
    sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    sc.set_child(clamp)
    return sc, box


def header(box, title, text):
    box.append(label(title, ["title-1"]))
    if text:
        box.append(label(text, ["dim-label"], wrap=True))


class Section:
    title = ""
    icon = "emblem-system-symbolic"

    def __init__(self, shared):
        self.shared = shared
        self.v = shared.vehicle
        self.widget, self.box = page_box()

    def shown(self):
        pass

    def hidden(self):
        pass

    def params_loaded(self):
        pass

    def tick(self):
        pass


# ------------------------------------------------------------------ firmware
class FirmwareSection(Section):
    title = "Install firmware"
    icon = "system-software-install-symbolic"

    def __init__(self, shared):
        super().__init__(shared)
        header(self.box, "Install firmware",
               "Put ArduPilot on a flight controller over USB. You need an internet connection to "
               "download firmware, or a .apj file you already have.")
        self.box.append(Adw.Banner(title="Use a USB cable straight to the flight controller (not a telemetry radio). "
                                         "Don't unplug it while it is being written.", revealed=True))
        grp = Adw.PreferencesGroup(title="Firmware")
        self.vehicle_row = Adw.ComboRow(title="Vehicle type", model=Gtk.StringList.new([v[0] for v in fwmod.VEHICLE_TYPES]))
        self.release_row = Adw.ComboRow(title="Release", model=Gtk.StringList.new([r[0] for r in fwmod.RELEASES]))
        self.board_row = Adw.EntryRow(title="Board ID (detected automatically when possible)")
        self.board_row.set_input_purpose(Gtk.InputPurpose.DIGITS)
        grp.add(self.vehicle_row)
        grp.add(self.release_row)
        grp.add(self.board_row)
        self.box.append(grp)
        btns = Gtk.Box(spacing=8)
        btns.append(button("Detect board", self.detect, icon="find-location-symbolic",
                           tooltip="Find a board in bootloader mode on USB"))
        btns.append(button("Find firmware", self.lookup, icon="system-search-symbolic"))
        btns.append(button("Load a .apj file…", self.load_local, icon="document-open-symbolic"))
        self.box.append(btns)
        self.list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.set_placeholder(label("Press “Find firmware” to see what is available for your board.",
                                        ["dim-label"], margin_top=12, margin_bottom=12, xalign=0.5))
        self.box.append(self.list)
        self.install_btn = button("Install selected firmware", self.install, ["suggested-action", "pill"],
                                  halign=Gtk.Align.CENTER)
        self.install_btn.set_sensitive(False)
        self.box.append(self.install_btn)
        self.progress = Gtk.ProgressBar(show_text=True, visible=False)
        self.box.append(self.progress)
        self.logview = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                    top_margin=6, bottom_margin=6, left_margin=8)
        self.logview.set_size_request(-1, 140)
        frame = Gtk.Frame()
        frame.set_child(self.logview)
        self.box.append(frame)
        self.entries = []
        self.local_fw = None
        self.list.connect("row-selected", lambda *_: self.install_btn.set_sensitive(
            self.list.get_selected_row() is not None or self.local_fw is not None))
        self.cancel = threading.Event()

    def log(self, text):
        def add():
            buf = self.logview.get_buffer()
            buf.insert(buf.get_end_iter(), text + "\n")
        idle(add)

    def shown(self):
        st = self.v.state
        if st.connected and st.board_version and not self.board_row.get_text():
            self.board_row.set_text(str(st.board_version >> 16))
        names = [v[1] for v in fwmod.VEHICLE_TYPES]
        want = {"copter": "Copter", "plane": "Plane", "rover": "Rover", "sub": "Sub",
                "tracker": "AntennaTracker"}.get(st.vclass)
        if st.connected and want in names:
            self.vehicle_row.set_selected(names.index(want))

    def detect(self):
        if self.v.is_open:
            confirm(self.widget.get_root(), "Disconnect first?",
                    "To find the board MPXAir has to disconnect and restart it in bootloader mode.",
                    "Restart in bootloader", self._reboot_and_detect)
            return
        self._detect()

    def _reboot_and_detect(self):
        def work():
            self.v.send("COMMAND_LONG", **self.v.target, command=246, confirmation=0, param1=3,
                        param2=0, param3=0, param4=0, param5=0, param6=0, param7=0)
            time.sleep(0.5)
            idle(self.v.disconnect)
            time.sleep(1.5)
        background(work, lambda _r: self._detect())

    def _detect(self):
        self.log("Looking for a board in bootloader mode… (plug it in now if it isn't)")
        self.cancel.clear()

        def work():
            bl = fwmod.find_bootloader(30, self.log, self.cancel)
            bid = bl.board_id
            bl.close()
            return bid
        background(work, lambda bid: (self.board_row.set_text(str(bid)), self.lookup()),
                   lambda e: self.log(f"Error: {e}"))

    def lookup(self):
        txt = self.board_row.get_text().strip()
        if not txt.isdigit():
            toast("Enter or detect the board ID first")
            return
        bid = int(txt)
        vt = fwmod.VEHICLE_TYPES[self.vehicle_row.get_selected()][1]
        rel = fwmod.RELEASES[self.release_row.get_selected()][1]
        self.log(f"Looking up {vt} ({rel}) for board {bid}…")

        def work():
            m = fwmod.load_manifest()
            return fwmod.find_firmware(m, bid, vt, rel)

        def done(entries):
            self.entries = entries
            self.local_fw = None
            clear_box(self.list)
            for fw in entries[:20]:
                row = Adw.ActionRow(title=f"{fw.get('platform')} — {fw.get('vehicletype')} "
                                          f"{fw.get('mav-firmware-version')}",
                                    subtitle=f"{fw.get('mav-firmware-version-type', '').title()} · git {fw.get('git-sha', '')[:8]}")
                self.list.append(row)
            if not entries:
                self.log("No firmware found for that board and vehicle type.")
            else:
                self.log(f"Found {len(entries)} matching firmware builds.")
        background(work, done, lambda e: self.log(f"Error: {e}"))

    def load_local(self):
        def got(path):
            try:
                self.local_fw = fwmod.Firmware.from_file(path)
            except (OSError, fwmod.FirmwareError) as e:
                alert(self.widget.get_root(), "Can't use this file", str(e))
                return
            clear_box(self.list)
            fw = self.local_fw
            self.list.append(Adw.ActionRow(title=os.path.basename(path),
                                           subtitle=f"Board {fw.board_id} · {fw.summary} {fw.version}"))
            self.list.select_row(self.list.get_row_at_index(0))
            self.install_btn.set_sensitive(True)
            self.log(f"Loaded {path}: board {fw.board_id}, {len(fw.image) // 1024} kB")
        file_dialog(self.widget.get_root(), "Choose firmware", filters=[("ArduPilot firmware", ["*.apj"])],
                    on_file=got)

    def install(self):
        row = self.list.get_selected_row()
        if row is None:
            return
        entry = None if self.local_fw else self.entries[row.get_index()]
        if self.v.is_open:
            confirm(self.widget.get_root(), "Install firmware?",
                    "MPXAir will disconnect, restart the board in bootloader mode and write the new firmware. "
                    "Parameters are normally kept.", "Install", lambda: self._install(entry, True))
        else:
            confirm(self.widget.get_root(), "Install firmware?",
                    "After you press Install, plug the board in over USB (or unplug and replug it) within 30 seconds.",
                    "Install", lambda: self._install(entry, False))

    def _install(self, entry, reboot):
        self.progress.set_visible(True)
        self.progress.set_fraction(0)
        self.install_btn.set_sensitive(False)
        self.cancel.clear()

        def prog(a, b, what):
            idle(lambda: (self.progress.set_fraction(a / b if b else 0),
                          self.progress.set_text(f"{what} {a * 100 // max(1, b)}%")))

        def work():
            if self.local_fw:
                fw = self.local_fw
            else:
                self.log(f"Downloading {entry['url']}…")
                fw = fwmod.Firmware(fwmod.download(entry["url"], lambda a, b: prog(a, b, "Downloading")))
            if reboot:
                self.log("Restarting the board in bootloader mode…")
                self.v.send("COMMAND_LONG", **self.v.target, command=246, confirmation=0, param1=3,
                            param2=0, param3=0, param4=0, param5=0, param6=0, param7=0)
                time.sleep(0.5)
                self.v.disconnect()
                time.sleep(1.0)
            bl = fwmod.find_bootloader(30, self.log, self.cancel)
            try:
                fwmod.flash(fw, bl, lambda a, b: prog(a, b, "Writing"), self.log)
            finally:
                bl.close()
            return True

        def done(_r):
            self.progress.set_text("Firmware installed")
            self.install_btn.set_sensitive(True)
            alert(self.widget.get_root(), "Firmware installed",
                  "The board is restarting. Wait about 10 seconds, then connect again.")

        def err(e):
            self.log(f"Failed: {e}")
            self.progress.set_text("Failed")
            self.install_btn.set_sensitive(True)
            alert(self.widget.get_root(), "Firmware was not installed", str(e))
        background(work, done, err)


# --------------------------------------------------------------------- frame
class FrameSection(Section):
    title = "Frame type"
    icon = "applications-engineering-symbolic"

    def __init__(self, shared):
        super().__init__(shared)
        header(self.box, "Frame type", "Tell the autopilot what kind of airframe it is in. "
               "A reboot is needed after changing the frame class.")
        self.form = ParamForm(self.v, [
            ("Frame", ["FRAME_CLASS", "FRAME_TYPE"]),
            ("QuadPlane (VTOL)", ["Q_ENABLE", "Q_FRAME_CLASS", "Q_FRAME_TYPE"]),
            ("Board orientation", ["AHRS_ORIENTATION"]),
        ], on_saved=self._update_diagram)
        self.box.append(self.form)
        self.diagram = MotorDiagram()
        self.diagram.set_min_size(300, 300)
        frame = Gtk.Frame()
        frame.set_child(self.diagram)
        self.box.append(frame)

    def _update_diagram(self):
        p = self.v.params
        fc, ft = p.get("FRAME_CLASS"), p.get("FRAME_TYPE")
        if p.get("Q_ENABLE"):
            fc, ft = p.get("Q_FRAME_CLASS"), p.get("Q_FRAME_TYPE")
        self.diagram.dark = Adw.StyleManager.get_default().get_dark()
        lay = find_layout(fc, ft) if fc is not None else None
        self.diagram.set_layout(lay, "No motor diagram for this frame." if fc is not None else None)

    def params_loaded(self):
        self.form.build()
        self._update_diagram()

    def shown(self):
        self.form.build()
        self._update_diagram()


# --------------------------------------------------------------------- accel
ACCEL_STEPS = {1: "LEVEL", 2: "on its LEFT side", 3: "on its RIGHT side", 4: "NOSE DOWN",
               5: "NOSE UP", 6: "on its BACK"}


class AccelSection(Section):
    title = "Accelerometer"
    icon = "object-rotate-right-symbolic"

    def __init__(self, shared):
        super().__init__(shared)
        header(self.box, "Accelerometer calibration",
               "Teaches the autopilot which way is down. You will be asked to hold the vehicle in six "
               "positions. Keep it still in each one, then press the button.")
        self.step_lbl = label("", ["calib-step"], xalign=0.5, wrap=True, margin_top=12)
        self.box.append(self.step_lbl)
        self.step_img = label("", ["dim-label"], xalign=0.5, wrap=True)
        self.box.append(self.step_img)
        btns = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=8)
        self.start_btn = button("Calibrate accelerometer", self.start, ["suggested-action", "pill"])
        self.next_btn = button("Done — next position", self.next, ["suggested-action", "pill"], visible=False)
        btns.append(self.start_btn)
        btns.append(self.next_btn)
        self.box.append(btns)
        more = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        more.append(button("Calibrate level only", self.level, tooltip="Vehicle sitting level: trims roll and pitch"))
        more.append(button("Simple calibration", self.simple, tooltip="One position, for large vehicles"))
        self.box.append(more)
        self.msgs = label("", ["dim-label", "caption"], wrap=True, xalign=0.5)
        self.box.append(self.msgs)
        self.pos = None
        self.active = False
        self.v.on("COMMAND_LONG", lambda m: idle(self._vehicle_cmd, m))
        self.v.on("statustext", lambda e: idle(self._text, e))
        self._reset()

    def _reset(self):
        self.active = False
        self.pos = None
        self.next_btn.set_visible(False)
        self.start_btn.set_visible(True)
        self.step_lbl.set_text("")
        self.step_img.set_text("")

    def start(self):
        if not self.v.state.connected:
            toast("Connect to a vehicle first")
            return
        if self.v.state.armed:
            toast("Disarm first")
            return
        self.active = True
        self.start_btn.set_visible(False)
        self.step_lbl.set_text("Starting…")
        self.msgs.set_text("")
        background(lambda: self.v.command(241, 0, 0, 0, 0, 1, 0, 0, timeout=3, retries=1),
                   lambda r: None if r[0] or r[1] == "NO RESPONSE" else
                   (toast(f"The vehicle refused: {r[1]}"), self._reset()))

    def _vehicle_cmd(self, m):
        if m.command != 42429 or not self.active:
            return
        p = int(m.param1)
        if p == 16777215:
            self.step_lbl.set_text("Calibration successful ✔")
            self.step_img.set_text("Reboot the autopilot before flying.")
            self.active = False
            self.next_btn.set_visible(False)
            self.start_btn.set_visible(True)
            return
        if p == 16777216:
            self.step_lbl.set_text("Calibration failed")
            self.step_img.set_text("Try again, keeping the vehicle completely still in each position.")
            self._reset_buttons()
            return
        self.pos = p
        self.step_lbl.set_text(f"Place the vehicle {ACCEL_STEPS.get(p, p)}")
        self.step_img.set_text(f"Position {p} of 6 · hold it still, then press the button")
        self.next_btn.set_visible(True)

    def _reset_buttons(self):
        self.active = False
        self.next_btn.set_visible(False)
        self.start_btn.set_visible(True)

    def next(self):
        if self.pos is None:
            return
        self.v.send("COMMAND_LONG", **self.v.target, command=42429, confirmation=0, param1=self.pos,
                    param2=0, param3=0, param4=0, param5=0, param6=0, param7=0)
        self.next_btn.set_visible(False)
        self.step_img.set_text("Reading the sensors…")

    def _text(self, entry):
        _ts, _sev, text = entry
        t = text.lower()
        if self.active or "calibrat" in t:
            self.msgs.set_text(text)
        if self.active and "calibration successful" in t:
            self._vehicle_cmd(type("M", (), {"command": 42429, "param1": 16777215})())
        elif self.active and "calibration failed" in t:
            self._vehicle_cmd(type("M", (), {"command": 42429, "param1": 16777216})())

    def level(self):
        if self.v.state.connected:
            background(lambda: self.v.command(241, 0, 0, 0, 0, 2, 0, 0, timeout=10, retries=1),
                       lambda r: toast("Level calibration done" if r[0] else f"Level calibration: {r[1]}"))

    def simple(self):
        if self.v.state.connected:
            background(lambda: self.v.command(241, 0, 0, 0, 0, 4, 0, 0, timeout=10, retries=1),
                       lambda r: toast("Simple calibration done" if r[0] else f"Simple calibration: {r[1]}"))


# ------------------------------------------------------------------- compass
MAG_STATUS = {0: "Not started", 1: "Waiting to start", 2: "Step 1", 3: "Step 2", 4: "Success",
              5: "Failed", 6: "Bad orientation", 7: "Bad radius"}


class CompassSection(Section):
    title = "Compass"
    icon = "find-location-symbolic"

    def __init__(self, shared):
        super().__init__(shared)
        header(self.box, "Compass calibration",
               "Press Start, then slowly rotate the vehicle so each side points at the ground in turn "
               "(a full turn around every axis). Keep away from metal and magnets.")
        btns = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=6)
        self.start_btn = button("Start", self.start, ["suggested-action", "pill"])
        self.accept_btn = button("Accept", self.accept, ["pill"], sensitive=False)
        self.cancel_btn = button("Cancel", self.cancel, ["pill"], sensitive=False)
        for b in (self.start_btn, self.accept_btn, self.cancel_btn):
            btns.append(b)
        self.box.append(btns)
        self.bars_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.box.append(self.bars_box)
        self.bars = {}
        self.result = label("", wrap=True, xalign=0.5)
        self.box.append(self.result)
        self.form = ParamForm(self.v, [("Compass settings", ["COMPASS_USE", "COMPASS_USE2", "COMPASS_USE3",
                                                             "COMPASS_ORIENT", "COMPASS_ORIENT2",
                                                             "COMPASS_CAL_FIT", "COMPASS_AUTODEC"])])
        self.box.append(self.form)
        self.v.on("MAG_CAL_PROGRESS", lambda m: idle(self._progress, m))
        self.v.on("MAG_CAL_REPORT", lambda m: idle(self._report, m))
        self.reports = {}

    def params_loaded(self):
        self.form.build()

    def _bar(self, cid):
        if cid not in self.bars:
            row = Gtk.Box(spacing=10)
            row.append(label(f"Compass {cid + 1}", width_chars=10))
            pb = Gtk.ProgressBar(hexpand=True, show_text=True, valign=Gtk.Align.CENTER)
            row.append(pb)
            self.bars_box.append(row)
            self.bars[cid] = pb
        return self.bars[cid]

    def start(self):
        if not self.v.state.connected:
            toast("Connect to a vehicle first")
            return
        for pb in self.bars.values():
            pb.set_fraction(0)
        self.reports = {}
        self.result.set_text("Rotate the vehicle now…")

        def done(r):
            ok, msg = r
            if ok:
                self.start_btn.set_sensitive(False)
                self.cancel_btn.set_sensitive(True)
            else:
                self.result.set_text(f"Could not start: {msg}")
        background(lambda: self.v.command(42424, 0, 1, 1, 0, 0), done)

    def _progress(self, m):
        pb = self._bar(m.compass_id)
        pb.set_fraction(m.completion_pct / 100.0)
        pb.set_text(f"{m.completion_pct}% · {MAG_STATUS.get(m.cal_status, m.cal_status)}")

    def _report(self, m):
        self.reports[m.compass_id] = m
        pb = self._bar(m.compass_id)
        ok = m.cal_status == 4
        pb.set_fraction(1.0 if ok else pb.get_fraction())
        pb.set_text(f"{MAG_STATUS.get(m.cal_status, m.cal_status)} · fitness {m.fitness:.1f}")
        lines = []
        for cid, r in sorted(self.reports.items()):
            lines.append(f"Compass {cid + 1}: {MAG_STATUS.get(r.cal_status)} — offsets "
                         f"{r.ofs_x:.0f}, {r.ofs_y:.0f}, {r.ofs_z:.0f} · fitness {r.fitness:.1f}"
                         + (" (saved)" if r.autosaved else ""))
        self.result.set_text("\n".join(lines))
        if all(r.cal_status in (4, 5, 6, 7) for r in self.reports.values()):
            self.cancel_btn.set_sensitive(False)
            self.start_btn.set_sensitive(True)
            good = all(r.cal_status == 4 for r in self.reports.values())
            need_accept = good and not all(r.autosaved for r in self.reports.values())
            self.accept_btn.set_sensitive(need_accept)
            if good and not need_accept:
                self.result.set_text(self.result.get_text() + "\n\nSaved. Reboot the autopilot before flying.")

    def accept(self):
        background(lambda: self.v.command(42425, 0, 0, 1),
                   lambda r: (toast("Compass calibration saved — reboot the autopilot" if r[0] else r[1]),
                              self.accept_btn.set_sensitive(False)))

    def cancel(self):
        background(lambda: self.v.command(42426, 0, 0, 1),
                   lambda r: (self.start_btn.set_sensitive(True), self.cancel_btn.set_sensitive(False),
                              self.result.set_text("Cancelled")))


# --------------------------------------------------------------------- radio
class RcBar(Gtk.Box):
    def __init__(self, name):
        super().__init__(spacing=10)
        self.append(label(name, width_chars=10))
        self.bar = Gtk.LevelBar(min_value=800, max_value=2200, hexpand=True, valign=Gtk.Align.CENTER)
        for o in ("low", "high", "full"):
            self.bar.remove_offset_value(o)
        self.append(self.bar)
        self.val = label("", ["numeric"], width_chars=16, xalign=1.0)
        self.append(self.val)

    def set(self, v, lo=None, hi=None):
        self.bar.set_value(v if 800 <= v <= 2200 else 800)
        txt = f"{v}" if v else "—"
        if lo is not None and lo < 3000:
            txt = f"{lo} · {v} · {hi}"
        self.val.set_text(txt)


class RadioSection(Section):
    title = "Radio"
    icon = "input-gaming-symbolic"

    def __init__(self, shared):
        super().__init__(shared)
        header(self.box, "Radio calibration",
               "Switch your transmitter on. Press Calibrate, then move every stick and switch to both "
               "ends of its travel. Centre the sticks (throttle down) and press Done.")
        btns = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self.cal_btn = button("Calibrate", self.toggle, ["suggested-action", "pill"])
        btns.append(self.cal_btn)
        self.box.append(btns)
        self.rows = []
        names = ["1 Roll", "2 Pitch", "3 Throttle", "4 Yaw"] + [str(i) for i in range(5, 17)]
        for n in names:
            r = RcBar(n)
            self.rows.append(r)
            self.box.append(r)
        self.calibrating = False
        self.mins = [3000] * 16
        self.maxs = [0] * 16

    def shown(self):
        if self.v.state.connected:
            self.v.send("REQUEST_DATA_STREAM", **self.v.target, req_stream_id=3, req_message_rate=10,
                        start_stop=1)

    def hidden(self):
        if self.v.state.connected:
            self.v.request_streams()
        if self.calibrating:
            self.calibrating = False
            self.cal_btn.set_label("Calibrate")

    def tick(self):
        rc = self.v.state.rc
        for i, r in enumerate(self.rows):
            v = rc[i] if i < len(rc) else 0
            if self.calibrating and 800 <= v <= 2200:
                self.mins[i] = min(self.mins[i], v)
                self.maxs[i] = max(self.maxs[i], v)
            if self.calibrating:
                r.set(v, self.mins[i], self.maxs[i])
            else:
                r.set(v)

    def toggle(self):
        if not self.v.state.connected:
            toast("Connect to a vehicle first")
            return
        if not self.calibrating:
            if not any(800 <= v <= 2200 for v in self.v.state.rc[:4]):
                toast("No radio signal. Is the transmitter on and bound?")
                return
            self.mins = [3000] * 16
            self.maxs = [0] * 16
            self.calibrating = True
            self.cal_btn.set_label("Done")
            return
        self.calibrating = False
        self.cal_btn.set_label("Calibrate")
        rc = self.v.state.rc
        changes = {}
        for i in range(16):
            lo, hi = self.mins[i], self.maxs[i]
            if lo < hi and lo and hi and hi - lo > 100:
                trim = min(max(rc[i], lo), hi)
                if i == 2:
                    trim = lo          # throttle trim at the bottom
                changes[f"RC{i + 1}_MIN"] = lo
                changes[f"RC{i + 1}_MAX"] = hi
                changes[f"RC{i + 1}_TRIM"] = trim
        if not changes:
            toast("No stick movement was seen; nothing saved")
            return
        lines = [f"Ch {i + 1}: {self.mins[i]} – {self.maxs[i]}" for i in range(16)
                 if f"RC{i + 1}_MIN" in changes]

        def save():
            def work():
                errs = []
                for k, val in changes.items():
                    if k in self.v.params.values:
                        ok, msg = self.v.params.set(k, val)
                        if not ok:
                            errs.append(msg)
                return errs
            background(work, lambda errs: toast("Radio calibration saved" if not errs else "; ".join(errs[:3]), 5))
        confirm(self.widget.get_root(), "Save radio calibration?", "\n".join(lines), "Save", save)


# --------------------------------------------------------------- flight modes
class ModesSection(Section):
    title = "Flight modes"
    icon = "view-list-bullet-symbolic"

    def __init__(self, shared):
        super().__init__(shared)
        header(self.box, "Flight modes", "Choose which mode each position of your transmitter's mode "
               "switch selects. The highlighted row is the switch position right now.")
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.box.append(self.content)
        self.rows = []

    def params_loaded(self):
        self.build()

    def shown(self):
        self.build()

    def _prefix(self):
        p = self.v.params.values
        return "MODE" if "MODE1" in p and "FLTMODE1" not in p else "FLTMODE"

    def build(self):
        clear_box(self.content)
        self.rows = []
        p = self.v.params
        if not p.values:
            self.content.append(label("Connect to a vehicle to set its flight modes.", ["dim-label"]))
            return
        pre = self._prefix()
        if f"{pre}1" not in p.values:
            self.content.append(label("This vehicle has no flight mode switch settings.", ["dim-label"]))
            return
        table = modes.mode_table(self.v.state.vclass)
        nums = sorted(table)
        names = [table[n] for n in nums]
        grp = Adw.PreferencesGroup()
        simple = p.get("SIMPLE")
        ssimple = p.get("SUPER_SIMPLE")
        ranges = ["PWM 0–1230", "1231–1360", "1361–1490", "1491–1620", "1621–1749", "1750+"]
        for i in range(6):
            cur = int(p.get(f"{pre}{i + 1}", 0))
            row = Adw.ComboRow(title=f"Flight mode {i + 1}", subtitle=ranges[i],
                               model=Gtk.StringList.new(names))
            if cur in nums:
                row.set_selected(nums.index(cur))
            chk = []
            if simple is not None and self.v.state.vclass == "copter":
                s = Gtk.CheckButton(label="Simple", active=bool(int(simple) & (1 << i)), valign=Gtk.Align.CENTER)
                ss = Gtk.CheckButton(label="Super simple", active=bool(int(ssimple or 0) & (1 << i)),
                                     valign=Gtk.Align.CENTER)
                row.add_suffix(s)
                row.add_suffix(ss)
                chk = [s, ss]
            grp.add(row)
            self.rows.append((row, chk))
        self.content.append(grp)
        ch_name = "FLTMODE_CH" if "FLTMODE_CH" in p.values else "MODE_CH"
        self.ch_name = ch_name
        self.status = label("", ["dim-label"])
        self.content.append(self.status)
        bar = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        bar.append(button("Revert", self.build))
        bar.append(button("Save modes", lambda: self.save(nums), ["suggested-action"]))
        self.content.append(bar)

    def tick(self):
        if not self.rows:
            return
        ch = int(self.v.params.get(getattr(self, "ch_name", "FLTMODE_CH"), 5) or 5)
        pwm = self.v.state.rc[ch - 1] if 0 < ch <= 16 else 0
        slot = -1
        if pwm:
            for i, edge in enumerate((1230, 1360, 1490, 1620, 1749)):
                if pwm <= edge:
                    slot = i
                    break
            else:
                slot = 5
        for i, (row, _c) in enumerate(self.rows):
            (row.add_css_class if i == slot else row.remove_css_class)("accent")
        self.status.set_text(f"Mode switch is channel {ch}: PWM {pwm or '—'} · current mode {self.v.state.mode}")

    def save(self, nums):
        p = self.v.params
        pre = self._prefix()
        changes = {}
        simple = 0
        ssimple = 0
        for i, (row, chk) in enumerate(self.rows):
            val = nums[row.get_selected()]
            if int(p.get(f"{pre}{i + 1}", -1)) != val:
                changes[f"{pre}{i + 1}"] = val
            if chk:
                simple |= (1 << i) if chk[0].get_active() else 0
                ssimple |= (1 << i) if chk[1].get_active() else 0
        if self.rows and self.rows[0][1]:
            if int(p.get("SIMPLE", 0)) != simple:
                changes["SIMPLE"] = simple
            if int(p.get("SUPER_SIMPLE", 0)) != ssimple:
                changes["SUPER_SIMPLE"] = ssimple
        if not changes:
            toast("Nothing changed")
            return

        def work():
            return [m for ok, m in (p.set(k, val) for k, val in changes.items()) if not ok]
        background(work, lambda errs: toast("Flight modes saved" if not errs else "; ".join(errs)))


# ------------------------------------------------------------------ failsafe
class FailsafeSection(Section):
    title = "Failsafe"
    icon = "dialog-warning-symbolic"

    def __init__(self, shared):
        super().__init__(shared)
        header(self.box, "Failsafes", "What the vehicle does on its own when something goes wrong: "
               "lost radio, low battery, lost ground station or a bad position estimate.")
        self.live = label("", ["dim-label", "numeric"])
        self.box.append(self.live)
        self.form = ParamForm(self.v, [
            ("Radio (throttle) failsafe", ["FS_THR_ENABLE", ("THR_FAILSAFE",), ("FS_THR_VALUE", "THR_FS_VALUE"),
                                           "FS_SHORT_ACTN", "FS_LONG_ACTN", "FS_ACTION", "FS_TIMEOUT"]),
            ("Battery failsafe", [("BATT_FS_LOW_ACT", "FS_BATT_ENABLE"), ("BATT_LOW_VOLT", "FS_BATT_VOLTAGE"),
                                  ("BATT_LOW_MAH", "FS_BATT_MAH"), "BATT_FS_CRT_ACT", "BATT_CRT_VOLT",
                                  "BATT_CRT_MAH", "BATT_LOW_TIMER"]),
            ("Ground station failsafe", [("FS_GCS_ENABLE", "FS_GCS_ENABL"), "FS_GCS_TIMEOUT"]),
            ("Position estimate (EKF) failsafe", ["FS_EKF_ACTION", "FS_EKF_THRESH"]),
            ("Return to launch", [("RTL_ALT_M", "RTL_ALT", "ALT_HOLD_RTL", "RTL_ALTITUDE"),
                                  ("RTL_ALT_FINAL_M", "RTL_ALT_FINAL"), "RTL_LOIT_TIME",
                                  ("RTL_SPEED_MS", "RTL_SPEED")]),
        ])
        self.box.append(self.form)

    def params_loaded(self):
        self.form.build()

    def shown(self):
        self.form.build()

    def tick(self):
        st = self.v.state
        if not st.connected:
            self.live.set_text("")
            return
        thr = st.rc[2] if len(st.rc) > 2 else 0
        fsv = self.v.params.get("FS_THR_VALUE") or self.v.params.get("THR_FS_VALUE")
        warn = " · BELOW FAILSAFE VALUE" if fsv and thr and thr < fsv else ""
        self.live.set_text(f"Now: throttle input {thr}{warn} · battery {st.voltage:.2f} V · mode {st.mode} · "
                           f"{'armed' if st.armed else 'disarmed'}")


# ------------------------------------------------------------------- battery
class BatterySection(Section):
    title = "Battery monitor"
    icon = "battery-good-symbolic"

    def __init__(self, shared):
        super().__init__(shared)
        header(self.box, "Battery monitor", "Set up voltage and current sensing so MPXAir and the "
               "failsafes know how much battery is left.")
        self.live = label("", ["title-3", "numeric"])
        self.box.append(self.live)
        self.form = ParamForm(self.v, [
            ("Sensor", ["BATT_MONITOR", "BATT_CAPACITY", "BATT_VOLT_PIN", "BATT_CURR_PIN",
                        "BATT_VOLT_MULT", "BATT_AMP_PERVLT", "BATT_AMP_OFFSET", "BATT_ARM_VOLT"]),
        ], on_saved=None)
        self.box.append(self.form)
        grp = Adw.PreferencesGroup(title="Calibrate voltage",
                                   description="Measure the battery with a multimeter and enter the reading. "
                                               "MPXAir corrects BATT_VOLT_MULT to match.")
        self.meas = Adw.SpinRow.new_with_range(0, 100, 0.01)
        self.meas.set_digits(2)
        self.meas.set_title("Measured voltage (V)")
        grp.add(self.meas)
        self.box.append(grp)
        self.box.append(button("Apply measured voltage", self.calibrate, halign=Gtk.Align.END))

    def params_loaded(self):
        self.form.build()

    def shown(self):
        self.form.build()

    def tick(self):
        st = self.v.state
        if st.connected:
            pct = f" · {st.battery_remaining}% left" if st.battery_remaining >= 0 else ""
            self.live.set_text(f"{st.voltage:.2f} V · {st.current:.1f} A · {st.battery_used_mah:.0f} mAh used{pct}")
        else:
            self.live.set_text("")

    def calibrate(self):
        st = self.v.state
        mult = self.v.params.get("BATT_VOLT_MULT")
        meas = self.meas.get_value()
        if not st.connected or mult is None:
            toast("Connect to a vehicle with an analog battery monitor first")
            return
        if st.voltage < 1 or meas < 1:
            toast("Need a reported voltage and your measurement")
            return
        new = mult * meas / st.voltage
        confirm(self.widget.get_root(), "Change BATT_VOLT_MULT?",
                f"Reported {st.voltage:.2f} V, measured {meas:.2f} V.\n"
                f"BATT_VOLT_MULT {format_value(mult)} → {new:.4f}", "Apply",
                lambda: background(lambda: self.v.params.set("BATT_VOLT_MULT", new),
                                   lambda r: (toast("Voltage calibrated" if r[0] else r[1]), self.form.build())))


# ---------------------------------------------------------------- motor test
class MotorSection(Section):
    title = "Motor test"
    icon = "media-playlist-repeat-symbolic"

    def __init__(self, shared):
        super().__init__(shared)
        header(self.box, "Motor test", "Spin one motor at a time to check that each motor is plugged into "
               "the right output and spins the right way. Compare with the diagram below.")
        self.box.append(Adw.Banner(title="REMOVE THE PROPELLERS before testing motors.", revealed=True))
        self.diagram = MotorDiagram()
        frame = Gtk.Frame()
        frame.set_child(self.diagram)
        self.box.append(frame)
        self.frame_lbl = label("", ["dim-label", "caption"], wrap=True)
        self.box.append(self.frame_lbl)
        grp = Adw.PreferencesGroup()
        self.thr = Adw.SpinRow.new_with_range(1, 100, 1)
        self.thr.set_title("Throttle (%)")
        self.thr.set_value(8)
        self.dur = Adw.SpinRow.new_with_range(0.5, 10, 0.5)
        self.dur.set_digits(1)
        self.dur.set_title("Duration (seconds)")
        self.dur.set_value(2)
        grp.add(self.thr)
        grp.add(self.dur)
        self.box.append(grp)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6,
                                column_spacing=8, row_spacing=8, homogeneous=True)
        self.box.append(self.flow)
        row = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        row.append(button("Test all in sequence", self.test_all))
        row.append(button("Stop all", self.stop_all, ["destructive-action"]))
        self.box.append(row)
        self.box.append(label("How to check: press a letter. The motor shown with that letter in the diagram "
                              "should spin, in the direction of its arrow (look from above). If a different "
                              "motor spins, its wire is on the wrong output. If it spins the wrong way, swap "
                              "any two of its three motor wires (or reverse it in the ESC settings).",
                              ["dim-label"], wrap=True))
        self.n_motors = 8
        self.refresh()

    def shown(self):
        self.refresh()

    def params_loaded(self):
        self.refresh()

    def refresh(self):
        p = self.v.params
        self.diagram.dark = Adw.StyleManager.get_default().get_dark()
        fc, ft = p.get("FRAME_CLASS"), p.get("FRAME_TYPE")
        if p.get("Q_ENABLE"):
            fc, ft = p.get("Q_FRAME_CLASS"), p.get("Q_FRAME_TYPE")
        lay = find_layout(fc, ft) if fc is not None else None
        if fc is None:
            msg = "Connect to a multicopter (or QuadPlane) to see its motor layout."
            self.frame_lbl.set_text("")
        elif lay is None:
            msg = "No motor diagram for this frame (helicopters, single/coax and some special frames)."
            self.frame_lbl.set_text(f"FRAME_CLASS {fc}, FRAME_TYPE {ft}")
        else:
            msg = None
            self.frame_lbl.set_text(f"Frame from the vehicle: FRAME_CLASS {fc} ({lay.get('ClassName', '').title()}), "
                                    f"FRAME_TYPE {ft} ({lay.get('TypeName', '').replace('_', ' ')}). "
                                    "Change it under Frame type.")
        self.diagram.set_layout(lay, msg)
        motors = sorted(lay["motors"], key=lambda m: m["TestOrder"]) if lay else None
        self.n_motors = len(motors) if motors else 8
        child = self.flow.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.flow.remove(child)
            child = nxt
        for k in range(1, self.n_motors + 1):
            m = next((m for m in motors if m["TestOrder"] == k), None) if motors else None
            txt = f"{letter(k)}  (motor {m['Number']}, {m['Rotation']})" if m else f"Motor {letter(k)}"
            self.flow.append(button(txt, lambda k=k: self.test(k, 1), ["pill"]))

    def test(self, motor, count, order=0, thr=None):
        if not self.v.state.connected:
            toast("Connect to a vehicle first")
            return
        if self.v.state.armed:
            toast("Disarm first")
            return
        t = self.thr.get_value() if thr is None else thr
        dur = self.dur.get_value()
        if thr != 0 and count == 1:
            self.diagram.flash(motor, dur)
            GLib.timeout_add(int(dur * 1000) + 100, lambda: self.diagram.queue_draw() and False)
        background(lambda: self.v.command(209, motor, 0, t, dur, count, order),
                   lambda r: toast(f"Motor test: {'running' if r[0] else r[1]}"))

    def test_all(self):
        self.test(1, self.n_motors, order=1)

    def stop_all(self):
        self.diagram.flash(None, 0)
        self.test(1, self.n_motors, thr=0)


SECTIONS = [FirmwareSection, FrameSection, AccelSection, CompassSection, RadioSection,
            ModesSection, FailsafeSection, BatterySection, MotorSection]


class SetupPage(Gtk.Box):
    def __init__(self, shared):
        super().__init__()
        self.shared = shared
        self.sections = [cls(shared) for cls in SECTIONS]
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, hexpand=True)
        lb = Gtk.ListBox(css_classes=["navigation-sidebar"])
        for i, s in enumerate(self.sections):
            self.stack.add_named(s.widget, str(i))
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6, margin_start=6)
            box.append(Gtk.Image(icon_name=s.icon))
            box.append(label(s.title))
            row.set_child(box)
            lb.append(row)
        lb.connect("row-selected", self._selected)
        side = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, css_classes=["side-panel"])
        side.set_size_request(230, -1)
        side.set_child(lb)
        self.append(side)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.append(self.stack)
        self.current = None
        lb.select_row(lb.get_row_at_index(0))
        shared.hub.on("params-loaded", lambda ok: [s.params_loaded() for s in self.sections])
        GLib.timeout_add(200, self._tick)

    def _selected(self, lb, row):
        if row is None:
            return
        if self.current:
            self.current.hidden()
        self.current = self.sections[row.get_index()]
        self.stack.set_visible_child_name(str(row.get_index()))
        self.current.shown()

    def on_shown(self):
        if self.current:
            self.current.shown()

    def _tick(self):
        if self.get_mapped() and self.current:
            self.current.tick()
        elif self.current and isinstance(self.current, RadioSection) and self.current.calibrating:
            self.current.hidden()
        return True
