"""Connection to one MAVLink vehicle: link thread, telemetry state, commands.

Threading model: one I/O thread reads the transport and updates `self.state`.
Listeners registered with `on()` are called from the I/O thread, so UI code
must hop back to the GTK main loop (see ui.util.idle). Blocking helpers such
as `command()`, `params.fetch()` and the mission transfers must be called from
a worker thread, never from the GTK main loop.
"""
import collections
import json
import math
import os
import queue
import struct
import threading
import time

from ..mavlink import defs
from ..mavlink.codec import Encoder, Parser
from . import modes
from .transport import TransportError, open_transport

MAV_RESULT = {v: n[0].replace("MAV_RESULT_", "") for v, n in defs.ENUMS["MAV_RESULT"][1].items()}
SEVERITY = ["EMERGENCY", "ALERT", "CRITICAL", "ERROR", "WARNING", "NOTICE", "INFO", "DEBUG"]

FOREIGN_POSITION = {"GPS_RAW_INT", "GLOBAL_POSITION_INT", "GPS_INPUT", "HIL_GPS", "GPS2_RAW"}
FORCE_ARM_MAGIC = 2989
FORCE_DISARM_MAGIC = 21196
GCS_SYSID = 255
GCS_COMPID = 190


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


class State:
    """Latest known vehicle telemetry. Plain attributes, read freely from the UI."""

    def __init__(self):
        self.connected = False
        self.link_ok = False
        self.last_heartbeat = 0.0
        self.sysid = 0
        self.compid = 0
        self.mav_type = 0
        self.autopilot = 0
        self.vclass = "copter"
        self.armed = False
        self.base_mode = 0
        self.custom_mode = 0
        self.mode = "—"
        self.system_status = 0
        # attitude (degrees)
        self.roll = self.pitch = self.yaw = 0.0
        self.rollspeed = self.pitchspeed = self.yawspeed = 0.0
        # position
        self.lat = self.lon = 0.0
        self.alt = 0.0            # relative to home, m
        self.alt_msl = 0.0
        self.has_position = False
        self.vx = self.vy = self.vz = 0.0
        self.heading = 0.0
        # VFR HUD
        self.airspeed = 0.0
        self.groundspeed = 0.0
        self.climb = 0.0
        self.throttle = 0
        # GPS
        self.gps_fix = 0
        self.gps_sats = 0
        self.gps_hdop = 99.99
        self.gps_lat = self.gps_lon = 0.0
        self.gps2_fix = None
        # battery / system
        self.voltage = 0.0
        self.current = 0.0
        self.battery_remaining = -1
        self.battery_used_mah = 0.0
        self.sensors_present = 0
        self.sensors_enabled = 0
        self.sensors_health = 0
        self.load = 0.0
        self.drop_rate = 0.0
        # navigation
        self.wp_dist = 0.0
        self.wp_bearing = 0.0
        self.nav_bearing = 0.0
        self.xtrack_error = 0.0
        self.alt_error = 0.0
        self.target_bearing = 0.0
        self.current_wp = 0
        # home
        self.home_lat = self.home_lon = self.home_alt = None
        self.dist_home = 0.0
        self.bearing_home = 0.0
        # RC / servos
        self.rc = [0] * 18
        self.rc_rssi = 0
        self.servo = [0] * 16
        # radio
        self.rssi = self.remrssi = 0
        self.noise = self.remnoise = 0
        self.txbuf = 100
        self.radio_seen = False
        # health
        self.ekf_flags = 0
        self.ekf_vel_var = self.ekf_pos_h_var = self.ekf_pos_v_var = 0.0
        self.ekf_compass_var = self.ekf_terrain_var = 0.0
        self.vibe = [0.0, 0.0, 0.0]
        self.clipping = [0, 0, 0]
        self.wind_dir = self.wind_speed = 0.0
        self.terrain_height = None
        self.rangefinder = None
        self.prearm_ok = None
        # firmware
        self.firmware = ""
        self.board_version = 0
        self.flight_sw_version = 0
        self.capabilities = 0
        self.uid = 0
        # timing
        self.time_boot_ms = 0
        self.armed_since = None
        self.flight_time = 0.0
        # link stats
        self.packets = 0
        self.packets_lost = 0
        self.link_quality = 0.0
        self.rx_bytes = 0
        self.tx_bytes = 0


class Vehicle:
    def __init__(self, settings=None):
        self.settings = settings or {}
        self.state = State()
        self.transport = None
        self.parser = Parser()
        self.encoder = Encoder(GCS_SYSID, GCS_COMPID)
        self._listeners = collections.defaultdict(list)
        self._lock = threading.RLock()
        self._wlock = threading.Lock()
        self._thread = None
        self._hb_thread = None
        self._running = False
        self._waiters = []
        self.messages = {}                 # latest message by name
        self.message_rates = collections.Counter()
        self.statustext = collections.deque(maxlen=500)
        self.tlog = None
        self.tlog_path = None
        self.seen_systems = {}
        self._streams_requested = False
        self._last_rate_calc = time.monotonic()
        self._rate_counts = collections.Counter()
        self.rates = {}
        self._link_window = collections.deque(maxlen=40)
        self._chunked_statustext = {}
        self._last_home_req = 0.0
        self._text_buf = b""
        self.gcs_board_seen = 0.0

        from .params import ParamManager
        from .mission import MissionManager
        self.params = ParamManager(self)
        self.mission = MissionManager(self)

    # ------------------------------------------------------------------ events
    def on(self, name, cb):
        """Register cb(msg) for message `name`, or '*' for every message.
        Special events: 'connected', 'disconnected', 'statustext', 'link_lost',
        'link_restored', 'armed', 'mode'."""
        with self._lock:
            self._listeners[name].append(cb)
        return (name, cb)

    def off(self, handle):
        name, cb = handle
        with self._lock:
            try:
                self._listeners[name].remove(cb)
            except ValueError:
                pass

    def emit(self, name, *args):
        with self._lock:
            cbs = list(self._listeners.get(name, ()))
        for cb in cbs:
            try:
                cb(*args)
            except Exception as e:  # a broken listener must never kill the link
                print(f"[mpxair] listener error for {name}: {e!r}")

    # -------------------------------------------------------------- connection
    def connect(self, spec, tlog_dir=None):
        """Open the link. Raises TransportError. Returns once the link is open;
        the vehicle is 'connected' once a heartbeat is seen (event 'connected')."""
        self.disconnect()
        self.transport = open_transport(spec)
        self.parser = Parser()
        self.state = State()
        self.messages.clear()
        self.params.reset()
        self._streams_requested = False
        self._text_buf = b""
        self.gcs_board_seen = 0.0
        # ESP32 ground-station boards answer "#GCS" text commands on the telemetry port
        self._scan_board = bool(getattr(self.transport, "espressif", False))
        self._running = True
        if tlog_dir:
            try:
                os.makedirs(tlog_dir, exist_ok=True)
                self.tlog_path = os.path.join(
                    tlog_dir, time.strftime("%Y-%m-%d %H-%M-%S") + ".tlog")
                self.tlog = open(self.tlog_path, "wb")
            except OSError:
                self.tlog = None
        self._thread = threading.Thread(target=self._io_loop, name="mav-io", daemon=True)
        self._thread.start()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, name="mav-hb", daemon=True)
        self._hb_thread.start()

    def wait_heartbeat(self, timeout=10.0, cancel=None):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.state.connected:
                return True
            if cancel is not None and cancel.is_set():
                return False
            if not self._running:
                return False
            time.sleep(0.05)
        return False

    def disconnect(self):
        was = self._running
        self._running = False
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.5)
        if self.transport:
            try:
                self.transport.close()
            except Exception:
                pass
        self.transport = None
        if self.tlog:
            try:
                self.tlog.close()
            except OSError:
                pass
            self.tlog = None
        if was:
            self.state.connected = False
            self.state.link_ok = False
            self.emit("disconnected", None)

    @property
    def is_open(self):
        return self._running and self.transport is not None

    def _io_loop(self):
        err = None
        while self._running:
            try:
                data = self.transport.read(0.05)
            except TransportError as e:
                err = str(e)
                break
            except Exception as e:
                err = f"Link error: {e}"
                break
            if data:
                self.state.rx_bytes += len(data)
                if (self._scan_board or "nmea" in self._listeners
                        or "gcs-board" in self._listeners):
                    self._scan_text(data)
                for msg in self.parser.feed(data):
                    self._handle(msg)
            self._periodic()
        if self._running:
            # unexpected stop
            self._running = False
            self.state.connected = False
            self.state.link_ok = False
            if self.tlog:
                try:
                    self.tlog.close()
                except OSError:
                    pass
                self.tlog = None
            self.emit("disconnected", err)

    def _scan_text(self, data):
        """Pick text lines out of the binary MAVLink stream:
        - NMEA sentences ($GPGGA…) from radio boards that mix their GPS in;
        - "#GCS {json}" replies from the ESP32 ground-station board (RFD/ESP router).
        The MAVLink parser skips these bytes on its own, so telemetry is unaffected."""
        buf = self._text_buf + data
        nmea, gcs = [], []
        while True:
            i_n = buf.find(b"$")
            i_g = buf.find(b"#GCS {")
            starts = [i for i in (i_n, i_g) if i >= 0]
            if not starts:
                # keep a tail that could still grow into "#GCS {"
                keep = 0
                for n in range(5, 0, -1):
                    if buf.endswith(b"#GCS {"[:n]):
                        keep = n
                        break
                buf = buf[len(buf) - keep:] if keep else b""
                break
            start = min(starts)
            end = buf.find(b"\n", start)
            limit = 65536 if start == i_g else 120
            if end < 0:
                buf = buf[start:] if len(buf) - start < limit else b""
                break
            line = buf[start:end].strip()
            buf = buf[end + 1:]
            if start == i_g:
                gcs.append(line[5:])
            elif 10 < len(line) < 100 and b"*" in line:
                nmea.append(line)
        self._text_buf = buf
        for line in nmea:
            try:
                self.emit("nmea", line.decode("ascii"))
            except UnicodeDecodeError:
                pass
        for raw in gcs:
            obj = None
            for cand in (raw, bytes(b for b in raw if 0x20 <= b <= 0x7E)):
                try:
                    obj = json.loads(cand.decode("ascii", "ignore"))
                    break
                except ValueError:
                    continue
            if isinstance(obj, dict):
                self.gcs_board_seen = time.monotonic()
                self.emit("gcs-board", obj)

    def send_text(self, text):
        """Send a plain text command (used for the ESP32 ground-station board)."""
        t = self.transport
        if t is None:
            return False
        try:
            with self._wlock:
                t.write((text + "\r").encode())
            return True
        except Exception:
            return False

    def _heartbeat_loop(self):
        while self._running:
            self.send("HEARTBEAT", type=6, autopilot=8, base_mode=0, custom_mode=0,
                      system_status=4, mavlink_version=3)
            for _ in range(10):
                if not self._running:
                    return
                time.sleep(0.1)

    def _periodic(self):
        now = time.monotonic()
        st = self.state
        if st.connected:
            ok = (now - st.last_heartbeat) < 4.0
            if ok != st.link_ok:
                st.link_ok = ok
                self.emit("link_restored" if ok else "link_lost", None)
        if st.connected and st.home_lat is None and now - self._last_home_req > 5.0:
            self._last_home_req = now
            self.request_message(242)
        if now - self._last_rate_calc >= 1.0:
            dt = now - self._last_rate_calc
            self.rates = {k: v / dt for k, v in self._rate_counts.items()}
            self._rate_counts.clear()
            self._last_rate_calc = now
            self._link_window.append((self.parser.received, self.parser.lost))
            if len(self._link_window) >= 2:
                r0, l0 = self._link_window[0]
                r1, l1 = self._link_window[-1]
                got, lost = r1 - r0, l1 - l0
                st.link_quality = 100.0 * got / (got + lost) if (got + lost) else 0.0
            st.packets = self.parser.received
            st.packets_lost = self.parser.lost
            if st.armed and st.armed_since:
                st.flight_time = time.monotonic() - st.armed_since

    # ------------------------------------------------------------------- send
    def send(self, name, **kw):
        t = self.transport
        if t is None:
            return False
        try:
            data = self.encoder.encode(name, **kw)
            with self._wlock:
                t.write(data)
            self.state.tx_bytes += len(data)
            if self.tlog:
                try:
                    self.tlog.write(struct.pack(">Q", int(time.time() * 1e6)) + data)
                except (OSError, ValueError):
                    pass
            return True
        except TransportError:
            return False
        except Exception as e:
            print(f"[mpxair] send {name} failed: {e!r}")
            return False

    @property
    def target(self):
        return dict(target_system=self.state.sysid or 1, target_component=self.state.compid or 1)

    # -------------------------------------------------------------- waiting
    def wait_for(self, name, cond=None, timeout=2.0):
        """Block (worker thread only) until a message `name` matching cond arrives."""
        q = queue.Queue()
        w = (name, cond, q)
        with self._lock:
            self._waiters.append(w)
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            with self._lock:
                try:
                    self._waiters.remove(w)
                except ValueError:
                    pass

    def request_and_wait(self, send_fn, name, cond=None, timeout=1.5, retries=3):
        """Register a waiter first, then send, so a fast reply is never missed."""
        for _ in range(retries):
            q = queue.Queue()
            w = (name, cond, q)
            with self._lock:
                self._waiters.append(w)
            try:
                send_fn()
                try:
                    return q.get(timeout=timeout)
                except queue.Empty:
                    pass
            finally:
                with self._lock:
                    try:
                        self._waiters.remove(w)
                    except ValueError:
                        pass
            if not self._running:
                return None
        return None

    # ------------------------------------------------------------ dispatch
    def _handle(self, msg):
        name = msg._def.name
        st = self.state
        now = time.monotonic()
        msg._ts = now
        if self.tlog and getattr(msg, "_raw", None):
            try:
                self.tlog.write(struct.pack(">Q", int(time.time() * 1e6)) + msg._raw)
            except (OSError, ValueError):
                pass

        if name == "HEARTBEAT":
            self.seen_systems[(msg._sysid, msg._compid)] = (msg.type, msg.autopilot, now)
            # only lock on to autopilots (not GCSs, gimbals, radios)
            if msg.autopilot == 0 or msg.type in (6, 18, 26, 27, 30, 31, 32, 33):
                if name in self._listeners:
                    self.emit(name, msg)
                return
            if st.sysid == 0:
                st.sysid = msg._sysid
                st.compid = msg._compid
            if msg._sysid != st.sysid or msg._compid != st.compid:
                return
            self._on_heartbeat(msg, now)
        elif st.sysid and msg._sysid != st.sysid:
            # other systems: radios (SiK = sysid 51) and ground devices with their own GPS
            if name == "RADIO_STATUS":
                self._on_radio(msg)
            elif name in FOREIGN_POSITION and "foreign-position" in self._listeners:
                self.emit("foreign-position", msg)
            return
        elif not st.sysid and name in FOREIGN_POSITION:
            # not locked on to a vehicle yet: never mistake a ground GPS for the vehicle
            if "foreign-position" in self._listeners:
                self.emit("foreign-position", msg)
            return

        self.messages[name] = msg
        self._rate_counts[name] += 1
        handler = _HANDLERS.get(name)
        if handler:
            handler(self, msg)

        with self._lock:
            waiters = [w for w in self._waiters
                       if w[0] == name or (isinstance(w[0], tuple) and name in w[0])]
        for wname, cond, q in waiters:
            try:
                if cond is None or cond(msg):
                    q.put_nowait(msg)
            except Exception:
                pass

        if name in self._listeners:
            self.emit(name, msg)
        if "*" in self._listeners:
            self.emit("*", msg)

    def _on_heartbeat(self, msg, now):
        st = self.state
        first = not st.connected
        st.last_heartbeat = now
        st.mav_type = msg.type
        st.autopilot = msg.autopilot
        st.vclass = modes.vehicle_class(msg.type)
        st.base_mode = msg.base_mode
        st.system_status = msg.system_status
        armed = bool(msg.base_mode & 128)
        mode = modes.mode_name(st.vclass, msg.custom_mode)
        if armed != st.armed:
            st.armed = armed
            st.armed_since = time.monotonic() if armed else None
            if not first:
                self.emit("armed", armed)
        if mode != st.mode or msg.custom_mode != st.custom_mode:
            st.custom_mode = msg.custom_mode
            st.mode = mode
            if not first:
                self.emit("mode", mode)
        if first:
            st.connected = True
            st.link_ok = True
            self.encoder.version = 2 if self.parser.v2_seen else 1
            threading.Thread(target=self._on_first_heartbeat, daemon=True).start()
            self.emit("connected", None)

    def _on_first_heartbeat(self):
        self.request_streams()
        self.request_message(242)    # HOME_POSITION
        self.request_message(148)    # AUTOPILOT_VERSION

    def request_streams(self):
        """Ask the autopilot for telemetry at Mission Planner's default rates."""
        rates = self.settings.get("stream_rates") or {}
        streams = [
            (1, rates.get("sensors", 2)),      # RAW_SENSORS
            (2, rates.get("status", 2)),       # EXTENDED_STATUS
            (3, rates.get("rc", 2)),           # RC_CHANNELS
            (4, rates.get("rc", 2)),           # RAW_CONTROLLER
            (6, rates.get("position", 4)),     # POSITION
            (10, rates.get("attitude", 10)),   # EXTRA1 (attitude)
            (11, rates.get("attitude", 10)),   # EXTRA2 (VFR_HUD)
            (12, rates.get("status", 2)),      # EXTRA3
        ]
        for sid, rate in streams:
            self.send("REQUEST_DATA_STREAM", **self.target, req_stream_id=sid,
                      req_message_rate=int(rate), start_stop=1 if rate else 0)
            time.sleep(0.02)
        self._streams_requested = True

    def request_message(self, msgid):
        self.send("COMMAND_LONG", **self.target, command=512, confirmation=0,
                  param1=msgid, param2=0, param3=0, param4=0, param5=0, param6=0, param7=0)

    def _on_radio(self, msg):
        st = self.state
        st.rssi, st.remrssi = msg.rssi, msg.remrssi
        st.noise, st.remnoise = msg.noise, msg.remnoise
        st.txbuf = msg.txbuf
        st.radio_seen = True

    def _update_home_distance(self):
        st = self.state
        if st.home_lat is not None and st.has_position:
            st.dist_home = haversine(st.lat, st.lon, st.home_lat, st.home_lon)
            st.bearing_home = bearing(st.lat, st.lon, st.home_lat, st.home_lon)

    # ------------------------------------------------------------ commands
    def command(self, cmd, p1=0, p2=0, p3=0, p4=0, p5=0, p6=0, p7=0, timeout=2.0, retries=3,
                target=None):
        """Send COMMAND_LONG and wait for the matching COMMAND_ACK.
        Returns (ok, result_name). Call from a worker thread."""
        tgt = target or self.target
        conf = [0]

        def send():
            self.send("COMMAND_LONG", **tgt, command=cmd, confirmation=conf[0],
                      param1=p1, param2=p2, param3=p3, param4=p4, param5=p5, param6=p6, param7=p7)
            conf[0] = min(conf[0] + 1, 255)

        ack = self.request_and_wait(send, "COMMAND_ACK", lambda m: m.command == cmd,
                                    timeout=timeout, retries=retries)
        if ack is None:
            return False, "NO RESPONSE"
        # IN_PROGRESS: keep waiting for the final result
        while ack is not None and ack.result == 5:
            ack = self.wait_for("COMMAND_ACK", lambda m: m.command == cmd, timeout=30)
        if ack is None:
            return False, "NO RESPONSE"
        return ack.result == 0, MAV_RESULT.get(ack.result, str(ack.result))

    def command_int(self, cmd, frame, x, y, z, p1=0, p2=0, p3=0, p4=0, timeout=2.0):
        def send():
            self.send("COMMAND_INT", **self.target, frame=frame, command=cmd, current=0,
                      autocontinue=0, param1=p1, param2=p2, param3=p3, param4=p4,
                      x=x, y=y, z=z)
        ack = self.request_and_wait(send, "COMMAND_ACK", lambda m: m.command == cmd,
                                    timeout=timeout, retries=3)
        if ack is None:
            return False, "NO RESPONSE"
        return ack.result == 0, MAV_RESULT.get(ack.result, str(ack.result))

    def arm(self, arm=True, force=False):
        magic = (FORCE_ARM_MAGIC if arm else FORCE_DISARM_MAGIC) if force else 0
        return self.command(400, 1 if arm else 0, magic, timeout=10)

    def set_mode(self, name):
        num = modes.mode_number(self.state.vclass, name)
        if num is None:
            return False, f"Unknown mode {name}"
        ok, res = self.command(176, 1, num)     # MAV_CMD_DO_SET_MODE, custom mode flag
        if not ok and res == "NO RESPONSE":
            # very old firmware: SET_MODE message
            self.send("SET_MODE", target_system=self.state.sysid, base_mode=1, custom_mode=num)
            end = time.monotonic() + 2
            while time.monotonic() < end:
                if self.state.custom_mode == num:
                    return True, "ACCEPTED"
                time.sleep(0.1)
        return ok, res

    def takeoff(self, alt):
        return self.command(22, 0, 0, 0, float("nan"), 0, 0, alt)

    def land(self):
        vc = self.state.vclass
        if vc == "plane":
            return self.set_mode("QLAND" if self.params.get("Q_ENABLE") else "RTL")
        if vc == "rover":
            return self.set_mode("HOLD")
        return self.set_mode("SURFACE" if vc == "sub" else "LAND")

    def rtl(self):
        return self.set_mode("RTL")

    def goto(self, lat, lon, alt):
        """Guided 'fly to here' (same as MP: a MISSION_ITEM with current=2).
        Switches to GUIDED first when needed."""
        if self.state.mode != "GUIDED":
            ok, res = self.set_mode("GUIDED")
            if not ok:
                return ok, f"Could not switch to GUIDED: {res}"
        self.send("MISSION_ITEM_INT", **self.target, seq=0, frame=3, command=16, current=2,
                  autocontinue=0, param1=0, param2=0, param3=0, param4=0,
                  x=int(lat * 1e7), y=int(lon * 1e7), z=float(alt), mission_type=0)
        ack = self.wait_for("MISSION_ACK", timeout=2.0)
        if ack is None:
            return False, "NO RESPONSE"
        return ack.type == 0, "ACCEPTED" if ack.type == 0 else f"REJECTED ({ack.type})"

    def set_current_wp(self, seq):
        ok, res = self.command(224, seq)   # MAV_CMD_DO_SET_MISSION_CURRENT
        if not ok:
            self.send("MISSION_SET_CURRENT", **self.target, seq=seq)
            m = self.wait_for("MISSION_CURRENT", lambda m: m.seq == seq, timeout=2)
            return m is not None, "ACCEPTED" if m else res
        return ok, res

    def set_home(self, lat=None, lon=None, alt=None):
        if lat is None:
            return self.command(179, 1)                     # use current location
        return self.command(179, 0, 0, 0, 0, lat, lon, alt or 0)

    def reboot(self):
        return self.command(246, 1, 0, retries=1)

    def set_servo(self, ch, pwm):
        return self.command(183, ch, pwm)

    def mission_start(self):
        return self.command(300, 0, 0)

    def change_speed(self, speed, kind=1):
        return self.command(178, kind, speed, -1)

    def change_alt(self, alt):
        st = self.state
        return self.goto(st.lat, st.lon, alt)

    def preflight_calibration(self, gyro=0, mag=0, baro=0, rc=0, accel=0, airspeed=0):
        return self.command(241, gyro, mag, baro, rc, accel, 0, 0, timeout=5, retries=1)


# -------------------------------------------------------------------- handlers
def _h_attitude(v, m):
    st = v.state
    st.roll = math.degrees(m.roll)
    st.pitch = math.degrees(m.pitch)
    st.yaw = (math.degrees(m.yaw) + 360) % 360
    st.rollspeed, st.pitchspeed, st.yawspeed = m.rollspeed, m.pitchspeed, m.yawspeed
    st.time_boot_ms = m.time_boot_ms


def _h_gpi(v, m):
    st = v.state
    if m.lat == 0 and m.lon == 0:
        return
    st.lat = m.lat / 1e7
    st.lon = m.lon / 1e7
    st.alt = m.relative_alt / 1000.0
    st.alt_msl = m.alt / 1000.0
    st.vx, st.vy, st.vz = m.vx / 100.0, m.vy / 100.0, m.vz / 100.0
    if m.hdg != 65535:
        st.heading = m.hdg / 100.0
    st.has_position = True
    v._update_home_distance()


def _h_gps_raw(v, m):
    st = v.state
    st.gps_fix = m.fix_type
    st.gps_sats = m.satellites_visible if m.satellites_visible != 255 else 0
    st.gps_hdop = m.eph / 100.0 if m.eph != 65535 else 99.99
    st.gps_lat = m.lat / 1e7
    st.gps_lon = m.lon / 1e7


def _h_gps2(v, m):
    v.state.gps2_fix = m.fix_type


def _h_vfr(v, m):
    st = v.state
    st.airspeed = m.airspeed
    st.groundspeed = m.groundspeed
    st.climb = m.climb
    st.throttle = m.throttle
    if not v.messages.get("GLOBAL_POSITION_INT"):
        st.heading = m.heading
        st.alt_msl = m.alt


def _h_sys_status(v, m):
    st = v.state
    st.voltage = m.voltage_battery / 1000.0 if m.voltage_battery != 65535 else 0.0
    st.current = m.current_battery / 100.0 if m.current_battery >= 0 else 0.0
    st.battery_remaining = m.battery_remaining
    st.sensors_present = m.onboard_control_sensors_present
    st.sensors_enabled = m.onboard_control_sensors_enabled
    st.sensors_health = m.onboard_control_sensors_health
    st.load = m.load / 10.0
    st.drop_rate = m.drop_rate_comm / 100.0
    # prearm check bit (MAV_SYS_STATUS_PREARM_CHECK = 1<<28)
    if m.onboard_control_sensors_present & (1 << 28):
        st.prearm_ok = bool(m.onboard_control_sensors_health & (1 << 28))


def _h_battery(v, m):
    if m.id == 0 and m.current_consumed >= 0:
        v.state.battery_used_mah = m.current_consumed


def _h_nav(v, m):
    st = v.state
    st.wp_dist = m.wp_dist
    st.nav_bearing = m.nav_bearing
    st.target_bearing = m.target_bearing
    st.xtrack_error = m.xtrack_error
    st.alt_error = m.alt_error


def _h_mission_current(v, m):
    v.state.current_wp = m.seq


def _h_home(v, m):
    st = v.state
    st.home_lat = m.latitude / 1e7
    st.home_lon = m.longitude / 1e7
    st.home_alt = m.altitude / 1000.0
    v._update_home_distance()
    v.emit("home", None)


def _h_rc(v, m):
    st = v.state
    st.rc = [getattr(m, f"chan{i}_raw") for i in range(1, 19)]
    st.rc_rssi = m.rssi


def _h_servo(v, m):
    st = v.state
    out = [getattr(m, f"servo{i}_raw") for i in range(1, 17)]
    if m.port == 0:
        st.servo = out


def _h_radio(v, m):
    v._on_radio(m)


def _h_ekf(v, m):
    st = v.state
    st.ekf_flags = m.flags
    st.ekf_vel_var = m.velocity_variance
    st.ekf_pos_h_var = m.pos_horiz_variance
    st.ekf_pos_v_var = m.pos_vert_variance
    st.ekf_compass_var = m.compass_variance
    st.ekf_terrain_var = m.terrain_alt_variance


def _h_vibe(v, m):
    st = v.state
    st.vibe = [m.vibration_x, m.vibration_y, m.vibration_z]
    st.clipping = [m.clipping_0, m.clipping_1, m.clipping_2]


def _h_wind(v, m):
    st = v.state
    st.wind_dir = (m.direction + 360) % 360
    st.wind_speed = m.speed


def _h_terrain(v, m):
    v.state.terrain_height = m.current_height


def _h_rangefinder(v, m):
    v.state.rangefinder = m.distance


def _h_statustext(v, m):
    text = m.text
    sid = getattr(m, "id", 0)
    if sid:
        # chunked statustext: join chunks with the same id
        parts = v._chunked_statustext.setdefault(sid, [])
        parts.append(text)
        if len(text) == 50:
            return
        text = "".join(parts)
        del v._chunked_statustext[sid]
    entry = (time.time(), m.severity, text)
    v.statustext.append(entry)
    v.emit("statustext", entry)


def _h_autopilot_version(v, m):
    st = v.state
    st.capabilities = m.capabilities
    st.flight_sw_version = m.flight_sw_version
    st.board_version = m.board_version
    st.uid = m.uid
    sw = m.flight_sw_version
    major, minor, patch, typ = (sw >> 24) & 0xFF, (sw >> 16) & 0xFF, (sw >> 8) & 0xFF, sw & 0xFF
    kind = {0: "dev", 64: "alpha", 128: "beta", 192: "rc", 255: "official"}.get(typ, "")
    name = {"copter": "ArduCopter", "plane": "ArduPlane", "rover": "ArduRover",
            "sub": "ArduSub", "tracker": "AntennaTracker"}.get(st.vclass, "ArduPilot")
    st.firmware = f"{name} V{major}.{minor}.{patch}" + ("" if kind == "official" else f" {kind}")
    v.emit("firmware", None)


_HANDLERS = {
    "ATTITUDE": _h_attitude,
    "GLOBAL_POSITION_INT": _h_gpi,
    "GPS_RAW_INT": _h_gps_raw,
    "GPS2_RAW": _h_gps2,
    "VFR_HUD": _h_vfr,
    "SYS_STATUS": _h_sys_status,
    "BATTERY_STATUS": _h_battery,
    "NAV_CONTROLLER_OUTPUT": _h_nav,
    "MISSION_CURRENT": _h_mission_current,
    "HOME_POSITION": _h_home,
    "RC_CHANNELS": _h_rc,
    "SERVO_OUTPUT_RAW": _h_servo,
    "RADIO_STATUS": _h_radio,
    "EKF_STATUS_REPORT": _h_ekf,
    "VIBRATION": _h_vibe,
    "WIND": _h_wind,
    "TERRAIN_REPORT": _h_terrain,
    "RANGEFINDER": _h_rangefinder,
    "STATUSTEXT": _h_statustext,
    "AUTOPILOT_VERSION": _h_autopilot_version,
}
