"""Header-bar connection controls (port, baud, connect/disconnect)."""
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..core import config  # noqa: E402
from ..core.transport import BAUD_RATES, TransportError, list_serial_ports  # noqa: E402
from .progress import ProgressDialog  # noqa: E402
from .util import alert, background, idle, toast  # noqa: E402

FIXED = [("udp", "UDP (listen)"), ("tcp", "TCP"), ("udpout", "UDP (send to)"),
         ("demo", "Demo drone (built-in)")]


def usb_identity(device):
    """A stable name for a USB serial device (its /dev/serial/by-id entry), or None."""
    import glob
    import os
    real = os.path.realpath(device)
    for link in glob.glob("/dev/serial/by-id/*"):
        if os.path.realpath(link) == real:
            return link
    return None


def find_usb_device(identity):
    import os
    if identity and os.path.exists(identity):
        return os.path.realpath(identity)
    return None


class ConnectBar(Gtk.Box):
    def __init__(self, shared):
        super().__init__(spacing=6, css_classes=["connect-bar"])
        self.shared = shared
        self.v = shared.vehicle
        self.cancel = None
        self.connecting = False
        self.choices = []

        self.port_model = Gtk.StringList()
        self.port = Gtk.DropDown(model=self.port_model, tooltip_text="Connection")
        self.short = {}
        # closed: a short name that never widens the header; open list: the full name
        bf = Gtk.SignalListItemFactory()
        bf.connect("setup", lambda f, it: it.set_child(Gtk.Label(xalign=0, ellipsize=3, max_width_chars=16,
                                                                  width_chars=16)))
        bf.connect("bind", lambda f, it: it.get_child().set_text(
            self.short.get(it.get_item().get_string(), it.get_item().get_string())))
        self.port.set_factory(bf)
        lf = Gtk.SignalListItemFactory()
        lf.connect("setup", lambda f, it: it.set_child(Gtk.Label(xalign=0, ellipsize=3, max_width_chars=48)))
        lf.connect("bind", lambda f, it: it.get_child().set_text(it.get_item().get_string()))
        self.port.set_list_factory(lf)
        self.port.connect("notify::selected", lambda *_: self.port.set_tooltip_text(
            self._choice()[2] if self.choices else "Connection"))
        self.port.connect("notify::selected", lambda *_: self._update_fields())
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Look for USB ports again",
                             css_classes=["flat"])
        refresh.connect("clicked", lambda *_: self.refresh_ports())

        self.baud = Gtk.DropDown(model=Gtk.StringList.new([str(b) for b in BAUD_RATES]),
                                 tooltip_text="Baud rate (57600 for most telemetry radios, 115200 for USB)")
        self.addr = Gtk.Entry(placeholder_text="host:port", width_chars=13, max_width_chars=15)
        self.status = Gtk.Label(css_classes=["dim-label", "caption"])
        self.spinner = Adw.Spinner()
        self.spinner.set_visible(False)
        self.btn = Gtk.Button(label="Connect", css_classes=["suggested-action"])
        self.btn.connect("clicked", lambda *_: self.toggle())

        self.status.set_visible(False)
        for w in (self.status, self.spinner, self.port, refresh, self.baud, self.addr, self.btn):
            self.append(w)

        cfg = shared.settings.get("connection") or {}
        try:
            self.baud.set_selected(BAUD_RATES.index(int(cfg.get("baud", 115200))))
        except ValueError:
            self.baud.set_selected(BAUD_RATES.index(115200))
        self.refresh_ports(initial=True)
        shared.hub.on("vehicle-disconnected", self._on_disconnected)
        shared.hub.on("vehicle-connected", self._on_vehicle_connected)
        self.waiting_for_aircraft = False
        shared.hub.on("replay-started", lambda path: self._set_busy(False, "Replay"))
        GLib.timeout_add_seconds(3, self._poll_ports)
        self._known_ports = set(d for d, _ in list_serial_ports())

    # ----------------------------------------------------------------- ports
    def refresh_ports(self, initial=False):
        cfg = self.shared.settings.get("connection") or {}
        want = cfg.get("device") if cfg.get("type") == "serial" else cfg.get("type")
        self.choices = [("serial", d, f"{d}  {desc}".strip()) for d, desc in list_serial_ports()]
        self.choices += [(t, None, label) for t, label in FIXED]
        self.short = {}
        for t, dev, full in self.choices:
            if t == "serial":
                desc = full[len(dev):].strip()
                first = desc.split()[0] if desc else ""
                name = dev.replace("/dev/tty", "")
                self.short[full] = f"{name} · {first}" if first else name
            else:
                self.short[full] = {"udp": "UDP listen", "tcp": "TCP", "udpout": "UDP send",
                                    "demo": "Demo drone"}.get(t, full)
        n = self.port_model.get_n_items()
        self.port_model.splice(0, n, [c[2] for c in self.choices])
        sel = 0
        for i, (t, dev, _l) in enumerate(self.choices):
            if (t == "serial" and dev == want) or (t != "serial" and t == want):
                sel = i
                break
        else:
            if initial and not any(c[0] == "serial" for c in self.choices):
                sel = [c[0] for c in self.choices].index("demo")
        self.port.set_selected(sel)
        self._update_fields()

    def _poll_ports(self):
        now = set(d for d, _ in list_serial_ports())
        if now != self._known_ports:
            added = now - self._known_ports
            self._known_ports = now
            if not self.v.is_open:
                self.refresh_ports()
                if added:
                    toast(f"New USB device: {', '.join(sorted(added))}")
        return True

    def _choice(self):
        i = self.port.get_selected()
        if i == Gtk.INVALID_LIST_POSITION or i >= len(self.choices):
            return ("demo", None, "")
        return self.choices[i]

    def _update_fields(self):
        t, _dev, _l = self._choice()
        cfg = self.shared.settings.get("connection") or {}
        self.baud.set_visible(t == "serial")
        self.addr.set_visible(t in ("tcp", "udp", "udpout"))
        if t == "tcp":
            self.addr.set_text(f"{cfg.get('host', '127.0.0.1')}:{cfg.get('tcp_port', 5760)}")
            self.addr.set_placeholder_text("host:port")
        elif t == "udp":
            self.addr.set_text(str(cfg.get("port", 14550)))
            self.addr.set_placeholder_text("listen port")
        elif t == "udpout":
            self.addr.set_text(f"{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 14550)}")

    def _spec(self):
        t, dev, _l = self._choice()
        cfg = dict(self.shared.settings.get("connection") or {})
        if t == "serial":
            baud = BAUD_RATES[self.baud.get_selected()]
            cfg.update(type="serial", device=dev, baud=baud)
            spec = {"type": "serial", "device": dev, "baud": baud}
        elif t == "demo":
            port = self.shared.start_demo()
            cfg.update(type="demo")
            spec = {"type": "tcp", "host": "127.0.0.1", "port": port}
        else:
            text = self.addr.get_text().strip()
            if t == "udp":
                try:
                    port = int(text or 14550)
                except ValueError:
                    raise TransportError("The UDP port must be a number, for example 14550.")
                cfg.update(type="udp", port=port)
                spec = {"type": "udp", "port": port}
            else:
                host, _, port = text.rpartition(":")
                if not host:
                    host, port = text or "127.0.0.1", "5760" if t == "tcp" else "14550"
                try:
                    port = int(port)
                except ValueError:
                    raise TransportError("Enter the address as host:port, for example 127.0.0.1:5760.")
                cfg.update(type=t, host=host)
                if t == "tcp":
                    cfg["tcp_port"] = port
                else:
                    cfg["port"] = port
                spec = {"type": t, "host": host, "port": port}
        self.shared.settings.set("connection", cfg)
        return spec

    # ------------------------------------------------------------ connect
    def toggle(self):
        if getattr(self, "reconnecting", False):
            self.reconnecting = False
            self._set_busy(False)
            toast("Stopped reconnecting")
            return
        if self.connecting:
            if self.cancel:
                self.cancel.set()
            return
        if self.v.is_open:
            self.shared.follow.stop()
            self.v.disconnect()
            return
        try:
            spec = self._spec()
        except TransportError as e:
            alert(self.get_root(), "Can't connect", str(e))
            return
        self._connect(spec)

    def debug_connect(self, kind):
        for i, c in enumerate(self.choices):
            if c[0] == kind:
                self.port.set_selected(i)
        if kind == "tcp":
            self.addr.set_text("127.0.0.1:5760")
        self.toggle()

    def _set_busy(self, busy, text=""):
        self.connecting = busy
        replay = getattr(self.v.transport, "kind", "") == "tlog"
        self.port.set_visible(not replay)
        self.addr.set_visible(not replay and self._choice()[0] in ("tcp", "udp", "udpout"))
        self.baud.set_visible(not replay and self._choice()[0] == "serial")
        self.spinner.set_visible(busy)
        self.status.set_text(text)
        self.status.set_visible(bool(text))
        self.port.set_sensitive(not busy and not self.v.is_open)
        self.baud.set_sensitive(not busy and not self.v.is_open)
        self.addr.set_sensitive(not busy and not self.v.is_open)
        if busy:
            self.btn.set_label("Cancel")
            self.btn.set_css_classes([])
        elif self.v.is_open:
            self.btn.set_label("Disconnect")
            self.btn.set_css_classes(["destructive-action"])
        else:
            self.btn.set_label("Connect")
            self.btn.set_css_classes(["suggested-action"])

    def _connect(self, spec):
        if spec.get("type") == "serial":
            spec = dict(spec, usb_id=usb_identity(spec["device"]))
        self.last_spec = spec
        self.cancel = threading.Event()
        cancel = self.cancel
        self._set_busy(True, "Connecting…")
        tlog = config.tlog_dir() if self.shared.settings.get("record_tlog", True) else None
        where = {"serial": lambda: f"{spec.get('device')} at {spec.get('baud')} baud",
                 "tcp": lambda: f"TCP {spec.get('host')}:{spec.get('port')}",
                 "udp": lambda: f"UDP port {spec.get('port')}",
                 "udpout": lambda: f"UDP {spec.get('host')}:{spec.get('port')}"}.get(spec["type"], lambda: "")()
        dlg = ProgressDialog("Connecting", on_cancel=lambda: cancel.set())
        dlg.status("Connecting to the aircraft", where)
        self.dialog = dlg
        dlg.present(self.get_root())
        s_open = dlg.step(f"Opening {where}")
        s_hb = [None]

        def work():
            self.v.connect(spec, tlog_dir=tlog)
            idle(dlg.update_step, s_open, f"Opened {where}", "ok")
            idle(lambda: s_hb.__setitem__(0, dlg.step("Waiting for the aircraft's heartbeat…")))
            board = getattr(self.v.transport, "espressif", False)
            start = time.monotonic()
            end = start + 30
            while time.monotonic() < end:
                if self.v.wait_heartbeat(1.0, cancel):
                    return "ok"
                if cancel.is_set() or not self.v.is_open:
                    break
                left = int(end - time.monotonic())
                idle(lambda l=left: dlg.progress(None, f"Waiting… {l} s"))
                if board:
                    # RFD/ESP32 ground-station board: ask it to identify itself
                    self.v.send_text("#GCS status")
                    if self.v.gcs_board_seen and time.monotonic() > start + 6:
                        return "board"
            if self.v.gcs_board_seen and self.v.is_open:
                return "board"
            if not self.v.state.connected:
                self.v.disconnect()
                if cancel.is_set():
                    return "cancelled"
                raise TransportError(
                    "The link opened but no aircraft answered within 30 seconds.\n\n"
                    "Check that the aircraft is powered, the right port is selected and the "
                    "baud rate matches (57600 for most telemetry radios, 115200 for USB).")
            return "ok"

        def done(res):
            self._set_busy(False)
            if res == "board":
                self.waiting_for_aircraft = True
                self._board_found()
                self._set_busy(False, "Waiting for aircraft")
                self.btn.set_tooltip_text("Connected to the ground station board — click to disconnect")
                if s_hb[0]:
                    dlg.update_step(s_hb[0], "No heartbeat from the aircraft yet", "warn")
                dlg.step("Ground station board found — its GPS is being read", "ok")
                dlg.finish("Ground station connected",
                           "Waiting for the aircraft. When it powers up, MPXAir connects to it and "
                           "loads its parameters automatically.", ok=True, close_after=5)
                self.dialog = None
                return
            if res != "ok":
                self.status.set_text("")
                dlg.close()
                self.dialog = None
                return
            if s_hb[0]:
                st = self.v.state
                dlg.update_step(s_hb[0], f"Heartbeat received from {st.vclass} (system {st.sysid})", "ok")
            self._vehicle_ready()

        def error(e):
            self._set_busy(False)
            self.v.disconnect()
            dlg.close()
            self.dialog = None
            alert(self.get_root(), "Connection failed", str(e))

        background(work, done, error)

    def _board_found(self):
        g = self.shared.settings.get("ground_gps") or {}
        if g.get("source", "none") == "none":
            g = dict(g, source="board", board_profile="rfd_esp32")
            self.shared.settings.set("ground_gps", g)
            self.shared.ground.start()
            self.shared.hub.emit("ground-changed")
            toast("Using the ground station board's GPS as the ground position")

    def _on_vehicle_connected(self, *_):
        if getattr(self, "waiting_for_aircraft", False):
            self.waiting_for_aircraft = False
            self._vehicle_ready()

    def _vehicle_ready(self):
        self._set_busy(False)
        if getattr(self.v.transport, "espressif", False) and self.v.gcs_board_seen:
            self._board_found()
        st = self.v.state
        self.btn.set_tooltip_text(f"Connected to {st.vclass} #{st.sysid} — click to disconnect")
        self.shared.hub.emit("connected")
        self._after_connect()

    def _after_connect(self):
        """Download parameters, then the mission, fence and rally points (like MP),
        showing each step in the connection pop-up."""
        win = self.get_root()
        v = self.v
        st = v.state
        dlg = getattr(self, "dialog", None)
        if dlg is None:
            dlg = ProgressDialog("Connecting")
            dlg.present(win)
            dlg.step(f"Heartbeat received from {st.vclass} (system {st.sysid})", "ok")
        self.dialog = None
        dlg.set_button("Hide", cancel=False)
        dlg.status(f"Connected to {st.vclass} #{st.sysid}", "Downloading the aircraft's settings…")
        s_par = dlg.step("Parameters")
        dlg.progress(None, "Asking for the parameter list…")

        def prog(got, total):
            def ui():
                if total:
                    dlg.progress(got / total, f"Parameters {got} / {total}")
                    dlg.update_step(s_par, f"Parameters {got} / {total}")
                win.set_progress(got / total if total else 0.0)
            idle(ui)

        def work():
            ok = v.params.fetch(progress=prog)
            idle(win.set_progress, None)
            got, total = len(v.params.values), v.params.count
            idle(dlg.update_step, s_par, f"Parameters {got} / {total}" + ("" if ok else " (some missing)"),
                 "ok" if ok else "warn")
            # descriptions that match this exact firmware (downloaded once, then cached)
            from ..core.params import ParamDocs
            if not v.state.flight_sw_version:
                v.request_message(148)
                v.wait_for("AUTOPILOT_VERSION", timeout=2.0)
            ver = ParamDocs.version_of(v.state.flight_sw_version)
            if ver:
                h = [None]
                idle(lambda: h.__setitem__(0, dlg.step(f"Parameter descriptions for firmware {ver}…")))
                note = ParamDocs.use_firmware(st.vclass, v.state.flight_sw_version)
                idle(lambda: h[0] and dlg.update_step(
                    h[0], note or "Parameter descriptions: bundled",
                    "ok" if note and note.startswith("Descriptions matched") else "warn"))
            idle(self.shared.hub.emit, "params-loaded", ok)
            from ..core.mission import FENCE, MISSION, RALLY
            out = {}
            for name, label_, mt in (("mission", "Mission", MISSION), ("fence", "Geofence", FENCE),
                                     ("rally", "Rally points", RALLY)):
                if not v.is_open:
                    break
                h = [None]
                idle(lambda l=label_: h.__setitem__(0, dlg.step(f"{l}: reading…")))
                idle(dlg.progress, None, f"Reading {label_.lower()}…")
                try:
                    out[name] = v.mission.download(mt)
                    n = len(out[name]) - (1 if mt == MISSION and out[name] else 0)
                    idle(lambda l=label_, n=n: h[0] and dlg.update_step(
                        h[0], f"{l}: {n} item{'s' if n != 1 else ''}", "ok"))
                except RuntimeError as e:
                    out[name] = None
                    idle(lambda l=label_, e=e: h[0] and dlg.update_step(h[0], f"{l}: {e}", "warn"))
            return ok, out

        def done(res):
            ok, out = res
            for name in ("mission", "fence", "rally"):
                if out.get(name) is not None:
                    setattr(self.shared, name, out[name])
            self.shared.hub.emit("mission-changed", "vehicle")
            fw = st.firmware or st.vclass
            if not v.is_open:
                dlg.finish("Connection lost", "The link dropped while loading.", ok=False)
            else:
                dlg.finish(f"Connected to {fw}",
                           f"{len(v.params.values)} parameters loaded"
                           + ("" if ok else f" (of {v.params.count}; the rest will be fetched when needed)"),
                           ok=True, close_after=2.5)

        def err(e):
            idle(win.set_progress, None)
            dlg.finish("Couldn't load the aircraft's settings", str(e), ok=False)
        background(work, done, err)

    def _on_disconnected(self, err):
        self.waiting_for_aircraft = False
        if err == "Replay closed":
            err = None
        self._set_busy(False)
        self._update_fields()
        self.btn.set_tooltip_text(None)
        self.refresh_ports()
        spec = getattr(self, "last_spec", None)
        if err and spec and spec.get("type") == "serial" and spec.get("usb_id"):
            # the USB device dropped off (unplugged, or a power dip on the board):
            # wait for the same device to come back, under whatever name it gets
            self._start_reconnect(spec, err)
            return
        if err:
            alert(self.get_root(), "Connection lost", err)

    # ---------------------------------------------------------------- auto-reconnect
    def _start_reconnect(self, spec, err, seconds=120):
        self._reconnect_until = time.monotonic() + seconds
        self._reconnect_spec = spec
        self._set_busy(False, "Reconnecting…")
        self.btn.set_label("Stop")
        self.btn.set_css_classes([])
        self.reconnecting = True
        toast(f"Connection lost ({err}). Reconnecting automatically when the device is back…", 6)
        GLib.timeout_add(1500, self._reconnect_tick)

    def _reconnect_tick(self):
        if not getattr(self, "reconnecting", False):
            return False
        spec = self._reconnect_spec
        dev = find_usb_device(spec["usb_id"])
        if dev:
            self.reconnecting = False
            new = dict(spec, device=dev)
            cfg = dict(self.shared.settings.get("connection") or {})
            cfg.update(type="serial", device=dev, baud=spec.get("baud"))
            self.shared.settings.set("connection", cfg)
            self.refresh_ports()
            GLib.timeout_add(1200, lambda: self._connect(new) and False)   # let the device settle
            return False
        if time.monotonic() > self._reconnect_until:
            self.reconnecting = False
            self._set_busy(False)
            alert(self.get_root(), "Connection lost",
                  "The device didn't come back within two minutes. Check the cable and the board's power, "
                  "then press Connect.")
            return False
        return True
