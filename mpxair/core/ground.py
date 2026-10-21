"""Ground station side: our own location, laptop battery, Follow Me, antenna tracker."""
import math
import os
import threading
import time

from .transport import SerialTransport, TransportError
from .vehicle import bearing, haversine


# ----------------------------------------------------------------- location
def nmea_checksum_ok(line):
    if not line.startswith("$") or "*" not in line:
        return False
    body, _, cs = line[1:].partition("*")
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return calc == int(cs[:2], 16)
    except ValueError:
        return False


def _nmea_deg(value, hemi):
    if not value:
        return None
    dot = value.index(".") if "." in value else len(value)
    deg = float(value[:dot - 2])
    mins = float(value[dot - 2:])
    d = deg + mins / 60.0
    return -d if hemi in ("S", "W") else d


class GroundLocation:
    """Current ground-station position from: manual entry, NMEA serial GPS, or GeoClue."""

    def __init__(self, settings, vehicle=None):
        self.settings = settings
        self.vehicle = vehicle
        self._link_handles = []
        self.lat = self.lon = None
        self.alt = 0.0
        self.sats = 0
        self.fix = False
        self.source = "none"
        self.status = "Not set"
        self.updated = 0.0
        self._stop = threading.Event()
        self._thread = None
        self._geoclue = None
        self.listeners = []
        self.board = {}
        self.hdop = None

    def valid(self):
        if self.lat is None:
            return False
        if self.source == "manual":
            return True
        return time.time() - self.updated < 10

    def _set(self, lat, lon, alt=None, sats=0):
        self.lat, self.lon = lat, lon
        if alt is not None:
            self.alt = alt
        self.sats = sats
        self.fix = True
        self.updated = time.time()
        for cb in list(self.listeners):
            try:
                cb()
            except Exception:
                pass

    def start(self):
        self.stop()
        cfg = self.settings.get("ground_gps") or {}
        self.source = cfg.get("source", "none")
        self._stop = threading.Event()
        if self.source == "manual":
            self._set(float(cfg.get("lat", 0)), float(cfg.get("lon", 0)), float(cfg.get("alt", 0)))
            self.status = "Manual position"
        elif self.source == "nmea":
            dev = cfg.get("device")
            if not dev:
                self.status = "No GPS port chosen"
                return
            self._thread = threading.Thread(target=self._nmea_loop, args=(dev, int(cfg.get("baud", 9600))),
                                            daemon=True, name="ground-gps")
            self._thread.start()
        elif self.source == "geoclue":
            self._start_geoclue()
        elif self.source == "link":
            self._start_link()
        elif self.source == "esp32":
            self._start_board()
        else:
            self.lat = self.lon = None
            self.status = "Not set"

    def stop(self):
        self._stop.set()
        self._geoclue = None
        if self.vehicle:
            for h in self._link_handles:
                self.vehicle.off(h)
        self._link_handles = []

    # ---- RFD/ESP32 ground-station board: its own GPS, read with "#GCS status"
    def _start_board(self):
        self.board = {}
        if not self.vehicle:
            self.status = "No telemetry link"
            return
        self.status = "Connect to the ground station board (top right) to read its GPS"
        self._link_handles = [self.vehicle.on("gcs-board", self._board_reply)]
        threading.Thread(target=self._board_poll, args=(self._stop,), daemon=True,
                         name="gcs-board").start()

    def _board_poll(self, stop):
        said_hello = False
        while not stop.is_set():
            v = self.vehicle
            if v.is_open and getattr(v.transport, "kind", "") in ("serial", "tcp", "udp", "udpci"):
                if not said_hello:
                    v.send_text("#GCS hello")
                    said_hello = True
                v.send_text("#GCS status")
                if v.gcs_board_seen and time.monotonic() - v.gcs_board_seen > 5:
                    self.status = "The ground station board stopped answering"
                elif not v.gcs_board_seen:
                    self.status = "Waiting for the ground station board to answer…"
            else:
                said_hello = False
            stop.wait(1.0)

    def _board_reply(self, r):
        if "fw" not in r:
            return          # replies to other commands ({"ok": …}, {"nmea": …})
        self.board = r
        g = r.get("gps") or {}
        name = r.get("name") or "Ground station"
        if g.get("fix") and g.get("lat") is not None:
            self._set(g["lat"] / 1e7, g["lon"] / 1e7, (g.get("altMm") or 0) / 1000.0, int(g.get("sats") or 0))
            self.hdop = g.get("hdop")
            self.status = f"{name} GPS: {self.sats} satellites, HDOP {self.hdop or 0:.1f}"
        else:
            self.fix = False
            self.sats = int(g.get("sats") or 0)
            self.status = (f"{name} GPS has no fix yet ({self.sats} satellites seen)" if g.get("nmea")
                           else f"{name}: no data from its GPS module")

    def board_follow(self, on):
        """Ask the board to send its position to the aircraft (ArduPilot Follow mode, FOLL_SYSID=255)."""
        if self.vehicle and self.vehicle.is_open:
            return self.vehicle.send_text(f"#GCS follow {1 if on else 0}")
        return False

    # ---- GPS on the telemetry radio board (shares the vehicle's connection)
    def _start_link(self):
        if not self.vehicle:
            self.status = "No telemetry link"
            return
        self.status = "Waiting for GPS data from the radio board (connect first)"
        self._link_handles = [self.vehicle.on("nmea", self._link_nmea),
                              self.vehicle.on("foreign-position", self._link_mavlink)]

    def _link_nmea(self, line):
        before = self.updated
        self._nmea_line(line)
        if self.updated != before:
            self.status = f"Radio board GPS (NMEA), {self.sats} satellites"
        elif not self.fix and "no fix" in self.status:
            self.status = "Radio board GPS has no fix yet (NMEA)"

    def _link_mavlink(self, m):
        name = m._def.name
        try:
            if name in ("GPS_RAW_INT", "GPS2_RAW", "HIL_GPS"):
                if m.fix_type < 2:
                    self.status = f"Radio board GPS (system {m._sysid}) has no fix yet"
                    return
                sats = m.satellites_visible if m.satellites_visible != 255 else 0
                self._set(m.lat / 1e7, m.lon / 1e7, m.alt / 1000.0, sats)
            elif name == "GPS_INPUT":
                if m.fix_type < 2:
                    return
                self._set(m.lat / 1e7, m.lon / 1e7, m.alt, m.satellites_visible)
            elif name == "GLOBAL_POSITION_INT":
                if m.lat == 0 and m.lon == 0:
                    return
                self._set(m.lat / 1e7, m.lon / 1e7, m.alt / 1000.0, self.sats)
            else:
                return
            self.status = f"Radio board GPS (MAVLink, system {m._sysid}), {self.sats} satellites"
        except AttributeError:
            pass

    def _nmea_loop(self, dev, baud):
        while not self._stop.is_set():
            try:
                port = SerialTransport(dev, baud)
            except TransportError as e:
                self.status = str(e)
                self._stop.wait(3)
                continue
            self.status = f"Waiting for GPS fix on {dev}"
            buf = b""
            try:
                while not self._stop.is_set():
                    buf += port.read(0.5)
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self._nmea_line(line.decode("ascii", "ignore").strip())
                    if len(buf) > 4096:
                        buf = b""
            except TransportError as e:
                self.status = str(e)
            finally:
                port.close()
            self._stop.wait(2)

    def _nmea_line(self, line):
        if not nmea_checksum_ok(line):
            return
        f = line.split("*")[0].split(",")
        kind = f[0][3:]
        try:
            if kind == "GGA" and len(f) > 9:
                q = int(f[6] or 0)
                if q == 0:
                    self.fix = False
                    self.status = "GPS has no fix yet"
                    return
                lat = _nmea_deg(f[2], f[3])
                lon = _nmea_deg(f[4], f[5])
                if lat is None or lon is None:
                    return
                self._set(lat, lon, float(f[9] or 0), int(f[7] or 0))
                self.status = f"GPS fix, {self.sats} satellites"
            elif kind == "RMC" and len(f) > 6 and f[2] == "A" and self.source in ("nmea", "link"):
                lat = _nmea_deg(f[3], f[4])
                lon = _nmea_deg(f[5], f[6])
                if lat is not None and lon is not None and time.time() - self.updated > 1.5:
                    self._set(lat, lon, None, self.sats)
        except (ValueError, IndexError):
            pass

    def _start_geoclue(self):
        try:
            import gi
            gi.require_version("Geoclue", "2.0")
            from gi.repository import Geoclue
        except (ImportError, ValueError):
            self.status = "Location service (GeoClue) is not available"
            return
        from . import config

        def ready(_src, res):
            try:
                simple = Geoclue.Simple.new_finish(res)
            except Exception as e:
                self.status = f"Location service refused: {getattr(e, 'message', e)}"
                return
            self._geoclue = simple
            simple.connect("notify::location", lambda *_: self._geoclue_update())
            self._geoclue_update()

        self.status = "Asking the location service…"
        Geoclue.Simple.new(config.APP_ID, Geoclue.AccuracyLevel.EXACT, None, ready)

    def _geoclue_update(self):
        if not self._geoclue:
            return
        loc = self._geoclue.get_location()
        if loc is None:
            return
        alt = loc.get_property("altitude")
        self._set(loc.get_property("latitude"), loc.get_property("longitude"),
                  alt if alt and alt > -1e6 else None)
        acc = loc.get_property("accuracy")
        self.status = f"Location service (±{acc:.0f} m)"


# --------------------------------------------------------------- laptop power
def laptop_power():
    """Return (percent or None, charging bool or None) for this computer's battery."""
    base = "/sys/class/power_supply"
    try:
        names = os.listdir(base)
    except OSError:
        return None, None
    pct = None
    charging = None
    for n in names:
        p = os.path.join(base, n)
        try:
            with open(os.path.join(p, "type")) as fh:
                typ = fh.read().strip()
        except OSError:
            continue
        if typ == "Battery":
            try:
                with open(os.path.join(p, "capacity")) as fh:
                    pct = int(fh.read().strip())
                with open(os.path.join(p, "status")) as fh:
                    s = fh.read().strip()
                charging = s in ("Charging", "Full", "Not charging")
            except (OSError, ValueError):
                pass
        elif typ == "Mains" and charging is None:
            try:
                with open(os.path.join(p, "online")) as fh:
                    charging = fh.read().strip() == "1"
            except OSError:
                pass
    return pct, charging


# ------------------------------------------------------------------ follow me
class FollowMe:
    """Sends the vehicle a guided target at the ground station's position."""

    def __init__(self, vehicle, ground, settings):
        self.v = vehicle
        self.ground = ground
        self.settings = settings
        self.active = False
        self.status = "Off"
        self.sent = 0
        self._thread = None
        self._last = None

    def start(self):
        if self.active:
            return
        self.active = True
        self.sent = 0
        self._last = None
        self._thread = threading.Thread(target=self._loop, daemon=True, name="follow-me")
        self._thread.start()

    def stop(self):
        self.active = False
        self.status = "Off"

    def _loop(self):
        while self.active:
            cfg = self.settings.get("follow_me") or {}
            rate = max(0.2, float(cfg.get("rate_hz", 2)))
            time.sleep(1.0 / rate)
            if not self.v.state.connected:
                self.status = "Waiting for vehicle"
                continue
            if not self.ground.valid():
                self.status = "Waiting for ground position"
                continue
            st = self.v.state
            if st.mode != "GUIDED":
                self.status = f"Paused: vehicle is in {st.mode}, needs GUIDED"
                continue
            lat, lon = self.ground.lat, self.ground.lon
            off = float(cfg.get("offset_m", 0) or 0)
            if off:
                # stay `off` metres behind the ground station, on the vehicle's side
                brg = bearing(lat, lon, st.lat, st.lon)
                lat, lon = _offset(lat, lon, off, brg)
            alt = float(cfg.get("alt", 20))
            if self._last:
                moved = haversine(self._last[0], self._last[1], lat, lon)
                if moved < float(cfg.get("min_move_m", 2)) and time.time() - self._last[2] < 5:
                    self.status = "Following (holding)"
                    continue
            self.v.send("SET_POSITION_TARGET_GLOBAL_INT", **self.v.target, time_boot_ms=0,
                        coordinate_frame=6, type_mask=0x0DF8,
                        lat_int=int(lat * 1e7), lon_int=int(lon * 1e7), alt=alt,
                        vx=0, vy=0, vz=0, afx=0, afy=0, afz=0, yaw=0, yaw_rate=0)
            self._last = (lat, lon, time.time())
            self.sent += 1
            self.status = f"Following — target {haversine(st.lat, st.lon, lat, lon):.0f} m away"


def _offset(lat, lon, dist, brg):
    r = 6378137.0
    d = dist / r
    b = math.radians(brg)
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


# ------------------------------------------------------------ antenna tracker
def tracker_angles(tlat, tlon, talt, vlat, vlon, valt):
    """Azimuth (deg from north) and elevation (deg) from tracker to vehicle."""
    az = bearing(tlat, tlon, vlat, vlon)
    dist = haversine(tlat, tlon, vlat, vlon)
    el = math.degrees(math.atan2(valt - talt, max(dist, 0.1)))
    return az, el, dist


def wrap180(a):
    return (a + 180) % 360 - 180


class AntennaTracker:
    """Points a pan/tilt antenna at the vehicle.
    Outputs: 'maestro' (Pololu Maestro servo controller), 'ardutracker' (serial
    !!!PAN:xxxx,TLT:xxxx lines, PWM) and 'degrees' (same line, angle x10)."""

    def __init__(self, vehicle, ground, settings):
        self.v = vehicle
        self.ground = ground
        self.settings = settings
        self.active = False
        self.port = None
        self.status = "Off"
        self.az = self.el = self.dist = 0.0
        self.pan_pwm = self.tilt_pwm = 1500
        self._thread = None

    def start(self):
        if self.active:
            return
        cfg = self.settings.get("tracker") or {}
        dev = cfg.get("device")
        if not dev:
            raise TransportError("Choose the serial port the tracker is plugged into.")
        self.port = SerialTransport(dev, int(cfg.get("baud", 9600)))
        if cfg.get("type", "maestro") == "maestro":
            for ch in (int(cfg.get("pan_ch", 0)), int(cfg.get("tilt_ch", 1))):
                self.port.write(bytes([0x87, ch, 0, 0]))      # speed: unlimited
                self.port.write(bytes([0x89, ch, 0, 0]))      # accel: unlimited
        self.active = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="tracker")
        self._thread.start()

    def stop(self):
        self.active = False
        if self.port:
            self.port.close()
            self.port = None
        self.status = "Off"

    def _pwm(self, angle, lo, hi, rng, trim, rev, wrap):
        center = (lo + hi) / 2.0
        span = hi - lo
        a = angle - trim
        if wrap:
            a = wrap180(a)
        pwm = span / max(1.0, rng) * a * (-1 if rev else 1) + center
        return int(max(lo, min(hi, pwm)))

    def point(self, az, el):
        cfg = self.settings.get("tracker") or {}
        pan_range = float(cfg.get("pan_range", 360))
        tilt_range = float(cfg.get("tilt_range", 90))
        pan = wrap180(az)
        tilt = el
        if tilt_range > 120 and abs(pan) > 90:
            # 180/180 mounts: flip over the top instead of spinning round
            pan = wrap180(pan + 180)
            tilt = 180 - tilt
        self.pan_pwm = self._pwm(pan, int(cfg.get("pan_min", 992)), int(cfg.get("pan_max", 2000)),
                                 pan_range, float(cfg.get("pan_trim", 0)), cfg.get("pan_rev"), True)
        self.tilt_pwm = self._pwm(tilt, int(cfg.get("tilt_min", 992)), int(cfg.get("tilt_max", 2000)),
                                  tilt_range, float(cfg.get("tilt_trim", 0)) + tilt_range / 2,
                                  cfg.get("tilt_rev"), False)
        typ = cfg.get("type", "maestro")
        if not self.port:
            return
        if typ == "maestro":
            for ch, pwm in ((int(cfg.get("pan_ch", 0)), self.pan_pwm), (int(cfg.get("tilt_ch", 1)), self.tilt_pwm)):
                q = pwm * 4
                self.port.write(bytes([0x84, ch, q & 0x7F, (q >> 7) & 0x7F]))
        elif typ == "ardutracker":
            self.port.write(f"!!!PAN:{self.pan_pwm:04d},TLT:{self.tilt_pwm:04d}\n".encode())
        else:
            self.port.write(f"!!!PAN:{int(pan * 10) % 3600:04d},TLT:{int(tilt * 10):04d}\n".encode())

    def _loop(self):
        while self.active:
            time.sleep(0.1)
            st = self.v.state
            if not st.has_position:
                self.status = "Waiting for vehicle position"
                continue
            if not self.ground.valid():
                self.status = "Waiting for tracker (ground) position"
                continue
            self.az, self.el, self.dist = tracker_angles(
                self.ground.lat, self.ground.lon, self.ground.alt or 0,
                st.lat, st.lon, st.alt_msl)
            try:
                self.point(self.az, self.el)
                self.status = f"Tracking: az {self.az:.0f}°  el {self.el:.0f}°  {self.dist:.0f} m"
            except TransportError as e:
                self.status = f"Tracker port error: {e}"
                self.active = False
