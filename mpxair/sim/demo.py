"""Built-in demo drone: a simple simulated ArduCopter that speaks MAVLink over TCP.

It is not a physics simulator (use ArduPilot SITL for that). It supports enough
of the protocol to try every MPXAir screen: telemetry, arming, modes, takeoff,
guided goto, missions/fence/rally, parameters, calibration flows and log listing.

Run standalone:  python3 -m mpxair.sim.demo [--port 5790] [--lat .. --lon ..]
"""
import argparse
import math
import random
import socket
import threading
import time

from ..mavlink.codec import Encoder, Parser

COPTER_MODES = {"STABILIZE": 0, "ACRO": 1, "ALT_HOLD": 2, "AUTO": 3, "GUIDED": 4, "LOITER": 5,
                "RTL": 6, "CIRCLE": 7, "LAND": 9, "POSHOLD": 16, "BRAKE": 17, "SMART_RTL": 21}

DEFAULT_PARAMS = {
    "SYSID_THISMAV": 1, "FRAME_CLASS": 1, "FRAME_TYPE": 1, "ARMING_CHECK": 1,
    "FLTMODE_CH": 5, "FLTMODE1": 0, "FLTMODE2": 2, "FLTMODE3": 5, "FLTMODE4": 6,
    "FLTMODE5": 3, "FLTMODE6": 9, "SIMPLE": 0, "SUPER_SIMPLE": 0,
    "RTL_ALT": 1500, "RTL_LOIT_TIME": 5000, "RTL_ALT_FINAL": 0, "LAND_SPEED": 50,
    "WPNAV_SPEED": 500, "WPNAV_RADIUS": 200, "WPNAV_SPEED_UP": 250, "WPNAV_SPEED_DN": 150,
    "PILOT_SPEED_UP": 250, "ANGLE_MAX": 3000,
    "FS_THR_ENABLE": 1, "FS_THR_VALUE": 975, "FS_GCS_ENABLE": 0, "FS_EKF_ACTION": 1,
    "BATT_MONITOR": 4, "BATT_CAPACITY": 5200, "BATT_LOW_VOLT": 10.5, "BATT_CRT_VOLT": 10.0,
    "BATT_FS_LOW_ACT": 2, "BATT_FS_CRT_ACT": 1, "BATT_LOW_MAH": 0, "BATT_CRT_MAH": 0,
    "FENCE_ENABLE": 0, "FENCE_TYPE": 7, "FENCE_ALT_MAX": 100, "FENCE_RADIUS": 150,
    "FENCE_ACTION": 1, "FENCE_MARGIN": 2,
    "COMPASS_USE": 1, "COMPASS_OFS_X": 5, "COMPASS_OFS_Y": 13, "COMPASS_OFS_Z": -18,
    "COMPASS_CAL_FIT": 16, "COMPASS_ORIENT": 0, "COMPASS_EXTERNAL": 1,
    "INS_ACCOFFS_X": 0.001, "INS_ACCOFFS_Y": 0.001, "INS_ACCOFFS_Z": 0.001,
    "INS_ACCSCAL_X": 1, "INS_ACCSCAL_Y": 1, "INS_ACCSCAL_Z": 1,
    "AHRS_ORIENTATION": 0, "AHRS_EKF_TYPE": 3, "EK3_ENABLE": 1,
    "ATC_RAT_RLL_P": 0.135, "ATC_RAT_RLL_I": 0.135, "ATC_RAT_RLL_D": 0.0036,
    "ATC_RAT_PIT_P": 0.135, "ATC_RAT_PIT_I": 0.135, "ATC_RAT_PIT_D": 0.0036,
    "ATC_RAT_YAW_P": 0.18, "ATC_RAT_YAW_I": 0.018, "ATC_ANG_RLL_P": 4.5,
    "ATC_ANG_PIT_P": 4.5, "ATC_ANG_YAW_P": 4.5, "MOT_SPIN_ARM": 0.1, "MOT_SPIN_MIN": 0.15,
    "MOT_THST_HOVER": 0.35, "MOT_PWM_TYPE": 0, "SERIAL1_BAUD": 57, "SERIAL1_PROTOCOL": 2,
    "SERIAL2_BAUD": 57, "SERIAL2_PROTOCOL": 2, "GPS_TYPE": 1, "GPS_TYPE2": 0,
    "LOG_BITMASK": 176126, "LOG_BACKEND_TYPE": 1, "WP_YAW_BEHAVIOR": 2,
    "RCMAP_ROLL": 1, "RCMAP_PITCH": 2, "RCMAP_THROTTLE": 3, "RCMAP_YAW": 4,
    "SR0_POSITION": 4, "SR0_EXTRA1": 10, "SR0_EXTRA2": 10, "SR0_EXT_STAT": 2,
}
for _ch in range(1, 17):
    DEFAULT_PARAMS[f"RC{_ch}_MIN"] = 1100
    DEFAULT_PARAMS[f"RC{_ch}_MAX"] = 1900
    DEFAULT_PARAMS[f"RC{_ch}_TRIM"] = 1500
    DEFAULT_PARAMS[f"RC{_ch}_REVERSED"] = 0
    DEFAULT_PARAMS[f"RC{_ch}_DZ"] = 20 if _ch <= 4 else 0
    DEFAULT_PARAMS[f"RC{_ch}_OPTION"] = 0
    DEFAULT_PARAMS[f"SERVO{_ch}_FUNCTION"] = 32 + _ch if _ch <= 4 else 0
    DEFAULT_PARAMS[f"SERVO{_ch}_MIN"] = 1100
    DEFAULT_PARAMS[f"SERVO{_ch}_MAX"] = 1900
    DEFAULT_PARAMS[f"SERVO{_ch}_TRIM"] = 1500
DEFAULT_PARAMS["RC3_TRIM"] = 1100
INT_PARAMS = {k for k, v in DEFAULT_PARAMS.items() if isinstance(v, int)}

ACCEL_POSITIONS = [1, 2, 3, 4, 5, 6]
ACCEL_NAMES = {1: "level", 2: "on its LEFT side", 3: "on its RIGHT side",
               4: "nose DOWN", 5: "nose UP", 6: "on its BACK"}


def _offset(lat, lon, dn, de):
    dlat = dn / 6378137.0
    dlon = de / (6378137.0 * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


def _dist_bearing(lat1, lon1, lat2, lon2):
    dn = math.radians(lat2 - lat1) * 6378137.0
    de = math.radians(lon2 - lon1) * 6378137.0 * math.cos(math.radians(lat1))
    return math.hypot(dn, de), math.degrees(math.atan2(de, dn)) % 360, dn, de


class DemoCopter:
    def __init__(self, lat=-35.363261, lon=149.165230, alt_msl=584.0):
        self.home = (lat, lon, alt_msl)
        self.lat, self.lon = lat, lon
        self.alt = 0.0                 # relative
        self.yaw = 0.0
        self.roll = self.pitch = 0.0
        self.vn = self.ve = self.vz = 0.0
        self.armed = False
        self.mode = 0
        self.target = None             # (lat, lon, alt)
        self.mission = {0: [], 1: [], 2: []}
        self.wp = 0
        self.voltage = 12.6
        self.mah = 0.0
        self.params = dict(DEFAULT_PARAMS)
        self.param_names = sorted(self.params)
        self.texts = []
        self.landing = False
        self.takeoff_alt = None
        self.loiter_until = None
        self.boot = time.monotonic()
        self.accel_cal_step = None
        self.mag_cal = None
        self.upload = None
        self.logs = [(1, 120_000, int(time.time()) - 86400), (2, 340_000, int(time.time()) - 3600)]

    def ms(self):
        return int((time.monotonic() - self.boot) * 1000)

    def say(self, text, sev=6):
        self.texts.append((sev, text))

    # --------------------------------------------------------------- physics
    def step(self, dt):
        mode = self.mode
        speed = self.params.get("WPNAV_SPEED", 500) / 100.0
        target_vn = target_ve = 0.0
        target_vz = 0.0
        if not self.armed:
            self.vn = self.ve = self.vz = 0
            self.roll = self.pitch = 0
            return
        if self.takeoff_alt is not None:
            target_vz = 2.5 if self.alt < self.takeoff_alt - 0.2 else 0.0
            if target_vz == 0.0:
                self.takeoff_alt = None
                self.say("Takeoff complete")
        elif mode == COPTER_MODES["LAND"] or self.landing:
            target_vz = -1.0 if self.alt > 10 else -0.5
        elif mode == COPTER_MODES["RTL"]:
            rtl_alt = self.params.get("RTL_ALT", 1500) / 100.0
            d, brg, dn, de = _dist_bearing(self.lat, self.lon, self.home[0], self.home[1])
            if self.alt < rtl_alt - 0.5 and d > 2:
                target_vz = 2.5
            elif d > 1.0:
                s = min(speed, d)
                target_vn, target_ve = s * dn / d, s * de / d
                self.yaw = brg
            else:
                target_vz = -1.0 if self.alt > 10 else -0.5
        elif mode in (COPTER_MODES["GUIDED"], COPTER_MODES["AUTO"]) and self.target:
            tlat, tlon, talt = self.target
            d, brg, dn, de = _dist_bearing(self.lat, self.lon, tlat, tlon)
            if d > 0.5:
                s = min(speed, d * 0.8 + 0.2)
                target_vn, target_ve = s * dn / d, s * de / d
                self.yaw = brg
            dz = talt - self.alt
            target_vz = max(-1.5, min(2.5, dz))
            if mode == COPTER_MODES["AUTO"] and d < max(1.0, self.params.get("WPNAV_RADIUS", 200) / 100.0) and abs(dz) < 1:
                self._advance_mission()
        elif mode == COPTER_MODES["AUTO"]:
            self._advance_mission()
        elif mode == COPTER_MODES["CIRCLE"]:
            target_vn = 3 * math.cos(math.radians(self.yaw + 90))
            target_ve = 3 * math.sin(math.radians(self.yaw + 90))
            self.yaw = (self.yaw + 6 * dt) % 360
        # first-order response
        k = min(1.0, dt * 2.0)
        self.vn += (target_vn - self.vn) * k
        self.ve += (target_ve - self.ve) * k
        self.vz += (target_vz - self.vz) * k
        self.lat, self.lon = _offset(self.lat, self.lon, self.vn * dt, self.ve * dt)
        self.alt = max(0.0, self.alt + self.vz * dt)
        # tilt follows acceleration-ish (velocity in body frame)
        c, s = math.cos(math.radians(self.yaw)), math.sin(math.radians(self.yaw))
        fwd = self.vn * c + self.ve * s
        right = -self.vn * s + self.ve * c
        self.pitch = -fwd * 2.0 + random.gauss(0, 0.3)
        self.roll = right * 2.0 + random.gauss(0, 0.3)
        # battery
        amps = 12 + 6 * math.hypot(self.vn, self.ve) / 5 if self.alt > 0.2 else 1.0
        self.mah += amps * dt / 3.6
        self.voltage = max(9.6, 12.6 - self.mah / 5200 * 2.4)
        if self.alt <= 0.05 and self.vz <= 0 and (mode == COPTER_MODES["LAND"] or self.landing
                                                   or mode == COPTER_MODES["RTL"]):
            if self.armed and self.takeoff_alt is None:
                self.armed = False
                self.landing = False
                self.say("Disarming motors")
                if mode == COPTER_MODES["RTL"]:
                    self.mode = COPTER_MODES["LAND"]
        self.current = amps

    def _advance_mission(self):
        items = self.mission[0]
        if self.wp == 0:
            self.wp = 1
        while self.wp < len(items):
            it = items[self.wp]
            cmd = it["command"]
            if cmd == 22:    # takeoff
                if self.alt < it["z"] - 0.5:
                    self.target = (self.lat, self.lon, it["z"])
                    return
            elif cmd in (16, 82, 17, 18, 19, 31):
                tgt = (it["x"] or self.lat, it["y"] or self.lon, it["z"] or self.alt)
                if self.target != tgt:
                    self.target = tgt
                    self.say(f"Mission: {self.wp} WP")
                    return
                if self.loiter_until is None and cmd == 19 and it["p1"] > 0:
                    self.loiter_until = time.monotonic() + it["p1"]
                    return
                if self.loiter_until and time.monotonic() < self.loiter_until:
                    return
                self.loiter_until = None
            elif cmd == 20:   # RTL
                self.mode = COPTER_MODES["RTL"]
                self.say("Mission: RTL")
                return
            elif cmd == 21:   # land
                if it["x"]:
                    d, _b, _n, _e = _dist_bearing(self.lat, self.lon, it["x"], it["y"])
                    if d > 1:
                        self.target = (it["x"], it["y"], self.alt)
                        return
                self.landing = True
                self.target = None
                return
            self.wp += 1
            self.target = None
        if self.wp >= len(items) and items:
            self.target = None

    def takeoff(self, alt):
        if not self.armed or self.mode != COPTER_MODES["GUIDED"]:
            return 4
        self.takeoff_alt = alt
        self.say(f"Takeoff to {alt:.0f} m")
        return 0


class DemoServer:
    def __init__(self, port=5790, lat=-35.363261, lon=149.165230):
        self.port = port
        self.sim = DemoCopter(lat, lon)
        self.enc = Encoder(1, 1)
        self.enc_radio = Encoder(51, 68)      # SiK radio status has its own sequence numbers
        self.enc_ground = Encoder(255, 190)   # a ground unit's own GPS messages
        self.sock = None
        self.client = None
        self.parser = Parser()
        self.running = False
        self.lock = threading.Lock()
        self.ground_gps = True        # NMEA lines mixed into the telemetry
        self.gcs_board = True         # answers "#GCS status" like the RFD/ESP32 board
        self.board_follow = False
        self.vehicle_off = False      # True: only the ground board answers (aircraft powered off)
        self.json_board = True        # answers "#GND?" with "#GND {json}" (custom JSON board example)
        self.csv_board = False        # sends "$GND,lat,lon,alt,sats,fix" every second
        self.mavlink_ground = False   # sends GPS_RAW_INT as system 255 (a ground unit's own GPS)

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", self.port))
        self.sock.listen(1)
        self.sock.settimeout(0.5)
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True, name="demo-accept").start()
        threading.Thread(target=self._sim_loop, daemon=True, name="demo-sim").start()

    def stop(self):
        self.running = False
        for s in (self.client, self.sock):
            try:
                if s:
                    s.close()
            except OSError:
                pass

    def send(self, name, **kw):
        c = self.client
        if not c:
            return
        enc = {51: self.enc_radio, 255: self.enc_ground}.get(kw.get("_sysid"), self.enc)
        try:
            with self.lock:
                c.sendall(enc.encode(name, **kw))
        except OSError:
            self.client = None

    def _accept_loop(self):
        while self.running:
            try:
                c, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.client = c
            self.parser = Parser()
            threading.Thread(target=self._rx_loop, args=(c,), daemon=True).start()

    def _rx_loop(self, c):
        while self.running and self.client is c:
            try:
                data = c.recv(4096)
            except OSError:
                break
            if not data:
                break
            if self.gcs_board and b"#GCS" in data:
                self._gcs_commands(data)
            if self.json_board and b"#GND?" in data:
                self._raw_line("#GND " + __import__("json").dumps(self._ground_fix_dict()))
            for m in self.parser.feed(data):
                try:
                    self.handle(m)
                except Exception as e:
                    print("[demo] error handling", m.msg_type, e)
        if self.client is c:
            self.client = None

    # ------------------------------------------------------------- periodic
    def _sim_loop(self):
        sim = self.sim
        last = time.monotonic()
        tick = 0
        while self.running:
            time.sleep(0.05)
            now = time.monotonic()
            dt = now - last
            last = now
            sim.step(dt)
            tick += 1
            if not self.client:
                continue
            if self.vehicle_off:
                continue
            self._stream(tick)
            self._calibration(tick)

    def _stream(self, tick):
        sim = self.sim
        ms = sim.ms()
        if tick % 2 == 0:   # 10 Hz
            self.send("ATTITUDE", time_boot_ms=ms, roll=math.radians(sim.roll),
                      pitch=math.radians(sim.pitch), yaw=math.radians(((sim.yaw + 180) % 360) - 180),
                      rollspeed=0, pitchspeed=0, yawspeed=0)
            gs = math.hypot(sim.vn, sim.ve)
            self.send("VFR_HUD", airspeed=gs, groundspeed=gs, heading=int(sim.yaw) % 360,
                      throttle=int(35 + sim.vz * 5) if sim.armed else 0,
                      alt=sim.home[2] + sim.alt, climb=sim.vz)
        if tick % 5 == 0:   # 4 Hz
            self.send("GLOBAL_POSITION_INT", time_boot_ms=ms, lat=int(sim.lat * 1e7),
                      lon=int(sim.lon * 1e7), alt=int((sim.home[2] + sim.alt) * 1000),
                      relative_alt=int(sim.alt * 1000), vx=int(sim.vn * 100), vy=int(sim.ve * 100),
                      vz=int(-sim.vz * 100), hdg=int(sim.yaw * 100) % 36000)
            self.send("GPS_RAW_INT", time_usec=ms * 1000, fix_type=3, lat=int(sim.lat * 1e7),
                      lon=int(sim.lon * 1e7), alt=int((sim.home[2] + sim.alt) * 1000), eph=78,
                      epv=120, vel=int(math.hypot(sim.vn, sim.ve) * 100),
                      cog=int(sim.yaw * 100), satellites_visible=14)
            if sim.target and sim.armed:
                d, brg, _n, _e = _dist_bearing(sim.lat, sim.lon, sim.target[0], sim.target[1])
                self.send("NAV_CONTROLLER_OUTPUT", nav_roll=sim.roll, nav_pitch=sim.pitch,
                          nav_bearing=int(brg), target_bearing=int(brg), wp_dist=int(d),
                          alt_error=sim.target[2] - sim.alt, aspd_error=0, xtrack_error=0)
        if tick % 10 == 0:  # 2 Hz
            sensors = 0x0020FC2F | (1 << 28)
            self.send("SYS_STATUS", onboard_control_sensors_present=sensors,
                      onboard_control_sensors_enabled=sensors,
                      onboard_control_sensors_health=sensors, load=230,
                      voltage_battery=int(sim.voltage * 1000),
                      current_battery=int(getattr(sim, "current", 1.0) * 100),
                      battery_remaining=max(0, int(100 - sim.mah / 52)), drop_rate_comm=0)
            self.send("BATTERY_STATUS", id=0, current_consumed=int(sim.mah),
                      voltages=[int(sim.voltage * 1000)] + [65535] * 9, battery_remaining=
                      max(0, int(100 - sim.mah / 52)), current_battery=int(getattr(sim, "current", 1.0) * 100))
            self.send("MISSION_CURRENT", seq=sim.wp, total=len(sim.mission[0]))
            rc = [1500] * 18
            rc[2] = 1100
            rc[4] = [1165, 1295, 1425, 1555, 1685, 1815][[0, 2, 5, 6, 3, 9].index(sim.mode)] \
                if sim.mode in (0, 2, 5, 6, 3, 9) else 1500
            if self.sim.accel_cal_step is None and getattr(self, "_rc_wiggle", False):
                t = time.monotonic()
                rc[0] = int(1500 + 400 * math.sin(t * 2))
                rc[1] = int(1500 + 400 * math.cos(t * 1.7))
                rc[2] = int(1500 + 400 * math.sin(t * 1.3))
                rc[3] = int(1500 + 400 * math.cos(t * 2.3))
            kw = {f"chan{i + 1}_raw": rc[i] for i in range(18)}
            self.send("RC_CHANNELS", time_boot_ms=ms, chancount=8, rssi=230, **kw)
            out = [1500 if sim.armed else 1000] * 16
            kw = {f"servo{i + 1}_raw": out[i] for i in range(16)}
            self.send("SERVO_OUTPUT_RAW", time_usec=ms * 1000, port=0, **kw)
            self.send("VIBRATION", time_usec=ms * 1000, vibration_x=random.uniform(3, 8),
                      vibration_y=random.uniform(3, 8), vibration_z=random.uniform(5, 12))
            self.send("EKF_STATUS_REPORT", flags=0x3FF if True else 0, velocity_variance=0.05,
                      pos_horiz_variance=0.04, pos_vert_variance=0.06, compass_variance=0.03,
                      terrain_alt_variance=0)
            self.send("RADIO_STATUS", rssi=180, remrssi=175, txbuf=95, noise=40, remnoise=42,
                      rxerrors=0, fixed=0, _sysid=51, _compid=68)
        if tick % 20 == 0:  # 1 Hz
            base = 1 | 4 | 8 | 16 | (128 if sim.armed else 0)
            self.send("HEARTBEAT", type=2, autopilot=3, base_mode=base, custom_mode=sim.mode,
                      system_status=4 if sim.armed else 3, mavlink_version=3)
            self.send("WIND", direction=230, speed=3.2, speed_z=0)
            if self.ground_gps:
                self._send_ground_gps()
            if self.csv_board:
                f = self._ground_fix_dict()
                self._raw_line(f"$GND,{f['lat']:.7f},{f['lon']:.7f},{f['alt']:.1f},{f['sats']},{f['fix']}")
            if self.mavlink_ground:
                f = self._ground_fix_dict()
                self.send("GPS_RAW_INT", time_usec=0, fix_type=3, lat=int(f["lat"] * 1e7), lon=int(f["lon"] * 1e7),
                          alt=int(f["alt"] * 1000), eph=90, epv=120, vel=0, cog=0, satellites_visible=f["sats"],
                          _sysid=255, _compid=190)
        while sim.texts:
            sev, text = sim.texts.pop(0)
            self.send("STATUSTEXT", severity=sev, text=text)

    def _gcs_commands(self, data):
        """Pretend to be the RFD900/ESP32 ground-station board: answer "#GCS …" commands."""
        import json
        for part in data.split(b"#GCS ")[1:]:
            cmd = part.split(b"\r")[0].split(b"\n")[0].decode("ascii", "ignore").strip()
            if cmd.startswith("follow"):
                self.board_follow = cmd.endswith("1")
                reply = {"ok": "Follow " + ("on" if self.board_follow else "off")}
            elif cmd in ("status", "hello"):
                lat, lon = _offset(self.sim.home[0], self.sim.home[1], -40, -35)
                reply = {
                    "fw": "1.7-demo", "elrs": "3.5", "name": "Demo ground station", "link": "connected",
                    "rssi": -61, "lq": 100, "snr": 9, "txMav": False, "uid": [1, 2, 3, 4, 5, 6],
                    "up": int(time.monotonic() - self.sim.boot), "rcHz": 25, "follow": self.board_follow,
                    "cnt": {"rc": 1000, "follow": 50 if self.board_follow else 0, "usbToRfd": 1, "rfdToUsb": 1},
                    "gps": {"fix": True, "q": 1, "sats": 11, "hdop": 0.8, "lat": int(lat * 1e7),
                            "lon": int(lon * 1e7), "altMm": int(self.sim.home[2] * 1000), "spd": 0.0,
                            "crs": 0, "time": time.strftime("%H%M%S", time.gmtime()), "date": "211026",
                            "pps": 100, "nmea": 900, "bad": 0},
                }
            else:
                continue
            c = self.client
            if c:
                try:
                    with self.lock:
                        c.sendall(b"#GCS " + json.dumps(reply).encode() + b"\n")
                except OSError:
                    self.client = None

    def _ground_fix_dict(self):
        lat, lon = _offset(self.sim.home[0], self.sim.home[1], -30, 25)
        return {"lat": lat, "lon": lon, "alt": self.sim.home[2], "sats": 10, "fix": 3}

    def _raw_line(self, text):
        c = self.client
        if c:
            try:
                with self.lock:
                    c.sendall(text.encode() + b"\r\n")
            except OSError:
                self.client = None

    def _send_ground_gps(self):
        """Pretend the ground radio board has a GPS: NMEA GGA lines mixed into the stream."""
        lat, lon = _offset(self.sim.home[0], self.sim.home[1], -25, -20)

        def dm(v, is_lat):
            a = abs(v)
            d = int(a)
            m = (a - d) * 60
            return (f"{d:02d}{m:07.4f}" if is_lat else f"{d:03d}{m:07.4f}"), \
                ("N" if v >= 0 else "S") if is_lat else ("E" if v >= 0 else "W")
        la, ns = dm(lat, True)
        lo, ew = dm(lon, False)
        body = f"GPGGA,{time.strftime('%H%M%S', time.gmtime())}.00,{la},{ns},{lo},{ew},1,09,0.9,{self.sim.home[2]:.1f},M,0.0,M,,"
        cs = 0
        for ch in body:
            cs ^= ord(ch)
        c = self.client
        if c:
            try:
                with self.lock:
                    c.sendall(f"${body}*{cs:02X}\r\n".encode())
            except OSError:
                self.client = None

    def _calibration(self, tick):
        sim = self.sim
        if sim.mag_cal is not None and tick % 4 == 0:
            sim.mag_cal = min(100, sim.mag_cal + 4)
            for cid in (0, 1):
                self.send("MAG_CAL_PROGRESS", compass_id=cid, cal_mask=3,
                          cal_status=3 if sim.mag_cal < 100 else 4, attempt=1,
                          completion_pct=sim.mag_cal, completion_mask=[255] * 10,
                          direction_x=0, direction_y=0, direction_z=0)
            if sim.mag_cal >= 100:
                for cid in (0, 1):
                    self.send("MAG_CAL_REPORT", compass_id=cid, cal_mask=3, cal_status=4,
                              autosaved=0, fitness=random.uniform(3, 9),
                              ofs_x=random.uniform(-50, 50), ofs_y=random.uniform(-50, 50),
                              ofs_z=random.uniform(-50, 50), diag_x=1, diag_y=1, diag_z=1,
                              offdiag_x=0, offdiag_y=0, offdiag_z=0, orientation_confidence=1,
                              old_orientation=0, new_orientation=0, scale_factor=1)
                sim.mag_cal = None

    # --------------------------------------------------------------- handlers
    def _param_value(self, name):
        sim = self.sim
        v = sim.params[name]
        self.send("PARAM_VALUE", param_id=name, param_value=float(v),
                  param_type=6 if name in INT_PARAMS else 9,
                  param_count=len(sim.param_names), param_index=sim.param_names.index(name))

    def _ack(self, cmd, result=0):
        self.send("COMMAND_ACK", command=cmd, result=result, target_system=255, target_component=190)

    def handle(self, m):
        sim = self.sim
        t = m.msg_type
        if t == "PARAM_REQUEST_LIST":
            def stream():
                for i, n in enumerate(sim.param_names):
                    if random.random() < 0.02:
                        continue        # drop a few, like a lossy radio would
                    self._param_value(n)
                    if i % 20 == 0:
                        time.sleep(0.01)
            threading.Thread(target=stream, daemon=True).start()
        elif t == "PARAM_REQUEST_READ":
            if m.param_index >= 0 and m.param_index < len(sim.param_names):
                self._param_value(sim.param_names[m.param_index])
            elif m.param_id in sim.params:
                self._param_value(m.param_id)
        elif t == "PARAM_SET":
            if m.param_id in sim.params:
                v = m.param_value
                sim.params[m.param_id] = int(round(v)) if m.param_id in INT_PARAMS else v
                self._param_value(m.param_id)
        elif t == "COMMAND_LONG":
            self._command(m)
        elif t == "COMMAND_INT":
            if m.command == 192:      # DO_REPOSITION
                sim.target = (m.x / 1e7, m.y / 1e7, m.z)
                self._ack(m.command)
            else:
                self._ack(m.command, 3)
        elif t == "SET_MODE":
            sim.mode = m.custom_mode
        elif t == "MISSION_REQUEST_LIST":
            items = sim.mission[m.mission_type]
            self.send("MISSION_COUNT", target_system=255, target_component=190, count=len(items),
                      mission_type=m.mission_type)
        elif t in ("MISSION_REQUEST_INT", "MISSION_REQUEST"):
            items = sim.mission[m.mission_type]
            if m.seq < len(items):
                it = items[m.seq]
                self.send("MISSION_ITEM_INT", target_system=255, target_component=190, seq=m.seq,
                          frame=it["frame"], command=it["command"], current=0, autocontinue=1,
                          param1=it["p1"], param2=it["p2"], param3=it["p3"], param4=it["p4"],
                          x=int(it["x"] * 1e7), y=int(it["y"] * 1e7), z=it["z"],
                          mission_type=m.mission_type)
        elif t == "MISSION_COUNT":
            sim.upload = (m.mission_type, m.count, [])
            if m.count == 0:
                sim.mission[m.mission_type] = []
                self.send("MISSION_ACK", target_system=255, target_component=190, type=0,
                          mission_type=m.mission_type)
            else:
                self.send("MISSION_REQUEST_INT", target_system=255, target_component=190, seq=0,
                          mission_type=m.mission_type)
        elif t == "MISSION_ITEM_INT":
            if m.current == 2:          # guided goto
                sim.target = (m.x / 1e7, m.y / 1e7, m.z)
                self.send("MISSION_ACK", target_system=255, target_component=190, type=0)
                return
            if not sim.upload:
                return
            mt, count, items = sim.upload
            if m.seq != len(items):
                self.send("MISSION_REQUEST_INT", target_system=255, target_component=190,
                          seq=len(items), mission_type=mt)
                return
            items.append(dict(frame=m.frame, command=m.command, p1=m.param1, p2=m.param2,
                              p3=m.param3, p4=m.param4, x=m.x / 1e7, y=m.y / 1e7, z=m.z))
            if len(items) < count:
                self.send("MISSION_REQUEST_INT", target_system=255, target_component=190,
                          seq=len(items), mission_type=mt)
            else:
                sim.mission[mt] = items
                sim.upload = None
                if mt == 0 and items:
                    sim.home = (items[0]["x"] or sim.home[0], items[0]["y"] or sim.home[1], sim.home[2])
                self.send("MISSION_ACK", target_system=255, target_component=190, type=0,
                          mission_type=mt)
                sim.say(f"Flight plan received ({count} items)")
        elif t == "MISSION_CLEAR_ALL":
            sim.mission[m.mission_type] = []
            self.send("MISSION_ACK", target_system=255, target_component=190, type=0,
                      mission_type=m.mission_type)
        elif t == "MISSION_SET_CURRENT":
            sim.wp = m.seq
            sim.target = None
        elif t == "LOG_REQUEST_LIST":
            for i, (lid, size, ts) in enumerate(sim.logs):
                self.send("LOG_ENTRY", id=lid, num_logs=len(sim.logs), last_log_num=sim.logs[-1][0],
                          time_utc=ts, size=size)
        elif t == "LOG_REQUEST_DATA":
            threading.Thread(target=self._send_log, args=(m.id, m.ofs, m.count), daemon=True).start()
        elif t == "LOG_ERASE":
            sim.logs = []

    def _send_log(self, lid, ofs, count):
        entry = next((l for l in self.sim.logs if l[0] == lid), None)
        if not entry:
            return
        data = _fake_bin_log(entry[1], self.sim)
        end = min(len(data), ofs + count) if count != 0xFFFFFFFF else len(data)
        pos = ofs
        while pos < end and self.client:
            chunk = data[pos:pos + 90]
            self.send("LOG_DATA", id=lid, ofs=pos, count=len(chunk), data=list(chunk) + [0] * (90 - len(chunk)))
            pos += len(chunk)
            if len(chunk) < 90:
                break
            if pos % (90 * 50) == 0:
                time.sleep(0.005)
        if end == len(data) and len(data) % 90 == 0:
            self.send("LOG_DATA", id=lid, ofs=len(data), count=0, data=[0] * 90)

    def _command(self, m):
        sim = self.sim
        c = m.command
        if c == 400:          # arm/disarm
            if m.param1 == 1:
                if sim.params.get("ARMING_CHECK") and m.param2 != 2989 and sim.voltage < 10.5:
                    sim.say("PreArm: Battery below minimum arming voltage", 2)
                    return self._ack(c, 4)
                sim.armed = True
                sim.say("Arming motors")
            else:
                if sim.alt > 0.3 and m.param2 != 21196:
                    sim.say("Disarm: vehicle is flying", 3)
                    return self._ack(c, 4)
                sim.armed = False
                sim.takeoff_alt = None
                sim.say("Disarming motors")
            self._ack(c)
        elif c == 176:        # set mode
            num = int(m.param2)
            if num not in COPTER_MODES.values():
                return self._ack(c, 4)
            sim.mode = num
            sim.landing = False
            if num == COPTER_MODES["AUTO"]:
                sim.wp = max(1, sim.wp)
                sim.target = None
            if num in (COPTER_MODES["LOITER"], COPTER_MODES["POSHOLD"], COPTER_MODES["BRAKE"],
                       COPTER_MODES["ALT_HOLD"], COPTER_MODES["GUIDED"]):
                sim.target = (sim.lat, sim.lon, sim.alt)
            self._ack(c)
        elif c == 22:         # takeoff
            self._ack(c, sim.takeoff(m.param7))
        elif c == 300:        # mission start
            sim.mode = COPTER_MODES["AUTO"]
            sim.wp = 1
            sim.target = None
            self._ack(c)
        elif c == 224:        # set current
            sim.wp = int(m.param1)
            sim.target = None
            self._ack(c)
        elif c == 512:        # request message
            mid = int(m.param1)
            self._ack(c)
            if mid == 242:
                h = sim.home
                self.send("HOME_POSITION", latitude=int(h[0] * 1e7), longitude=int(h[1] * 1e7),
                          altitude=int(h[2] * 1000), x=0, y=0, z=0, q=[1, 0, 0, 0],
                          approach_x=0, approach_y=0, approach_z=0)
            elif mid == 148:
                self.send("AUTOPILOT_VERSION", capabilities=0xFFEF,
                          flight_sw_version=(4 << 24) | (6 << 16) | (0 << 8) | 255,
                          board_version=0, uid=0x1234)
                self.sim.say("ArduCopter V4.6.0 (MPXAir demo drone)")
        elif c == 179:        # set home
            if m.param1 == 1:
                sim.home = (sim.lat, sim.lon, sim.home[2] + sim.alt)
            else:
                sim.home = (m.param5, m.param6, m.param7)
            self._ack(c)
            h = sim.home
            self.send("HOME_POSITION", latitude=int(h[0] * 1e7), longitude=int(h[1] * 1e7),
                      altitude=int(h[2] * 1000), x=0, y=0, z=0, q=[1, 0, 0, 0],
                      approach_x=0, approach_y=0, approach_z=0)
        elif c == 241:        # preflight calibration
            if sim.armed:
                return self._ack(c, 1)
            if m.param5 == 1:
                self._ack(c)
                sim.accel_cal_step = 0
                self._accel_request()
            elif m.param5 == 2:
                self._ack(c)
                sim.say("Calibration successful")
            elif m.param1 == 1:
                self._ack(c)
                sim.say("Gyro calibration complete")
            elif m.param3 == 1:
                self._ack(c)
                sim.say("Barometer calibration complete")
            else:
                self._ack(c)
        elif c == 42429:      # accelcal vehicle pos (confirmation from GCS)
            if sim.accel_cal_step is not None:
                sim.accel_cal_step += 1
                if sim.accel_cal_step >= len(ACCEL_POSITIONS):
                    sim.accel_cal_step = None
                    for ax in "XYZ":
                        sim.params[f"INS_ACCOFFS_{ax}"] = round(random.uniform(-0.2, 0.2), 4)
                    self.send("COMMAND_LONG", target_system=255, target_component=190,
                              command=42429, param1=16777215)
                    sim.say("Calibration successful")
                else:
                    self._accel_request()
        elif c == 42424:      # start mag cal
            sim.mag_cal = 0
            self._ack(c)
        elif c == 42425:      # accept mag cal
            self._ack(c)
            sim.say("Compass calibration saved")
        elif c == 42426:      # cancel mag cal
            sim.mag_cal = None
            self._ack(c)
        elif c == 246:        # reboot
            self._ack(c)
            sim.say("Rebooting")
        elif c == 178:        # change speed
            sim.params["WPNAV_SPEED"] = int(m.param2 * 100)
            self._ack(c)
        elif c == 183:        # set servo
            self._ack(c)
        elif c in (511, 520):
            self._ack(c)
        else:
            self._ack(c, 3)   # unsupported

    def _accel_request(self):
        pos = ACCEL_POSITIONS[self.sim.accel_cal_step]
        self.sim.say(f"Place vehicle {ACCEL_NAMES[pos]} and press any key.")
        self.send("COMMAND_LONG", target_system=255, target_component=190, command=42429,
                  param1=pos)


def _fake_bin_log(size, sim):
    """Build a small, valid DataFlash .bin log (FMT + PARM + ATT + GPS) of roughly `size` bytes."""
    import struct
    out = bytearray()

    _DF = {"Q": "Q", "f": "f", "I": "I", "L": "i", "e": "i", "B": "B", "h": "h"}

    def fmt(mtype, name, f, labels, length=None):
        if length is None:
            length = 3 + struct.calcsize("<" + "".join(_DF[c] for c in f))
        out.extend(b"\xa3\x95\x80" + struct.pack("<BB4s16s64s", mtype, length, name.encode(),
                                                  f.encode(), labels.encode()))

    fmt(128, "FMT", "BBnNZ", "Type,Length,Name,Format,Columns", 89)
    fmt(129, "ATT", "Qfff", "TimeUS,Roll,Pitch,Yaw")
    fmt(130, "GPS", "QLLfB", "TimeUS,Lat,Lng,Alt,NSats")
    fmt(131, "BAT", "Qff", "TimeUS,Volt,Curr")
    t = 0
    i = 0
    while len(out) < size:
        t += 100_000
        i += 1
        out.extend(b"\xa3\x95\x81" + struct.pack("<Qfff", t, 10 * math.sin(i / 20), 5 * math.cos(i / 30), (i * 2) % 360))
        lat, lon = _offset(sim.home[0], sim.home[1], 30 * math.sin(i / 50), 30 * math.cos(i / 50))
        out.extend(b"\xa3\x95\x82" + struct.pack("<QiifB", t, int(lat * 1e7), int(lon * 1e7), 20 + 10 * math.sin(i / 40), 14))
        out.extend(b"\xa3\x95\x83" + struct.pack("<Qff", t, 12.6 - i * 0.0005, 15 + 3 * math.sin(i / 10)))
    return bytes(out)


def main():
    ap = argparse.ArgumentParser(description="MPXAir demo drone (MAVLink over TCP)")
    ap.add_argument("--port", type=int, default=5790)
    ap.add_argument("--lat", type=float, default=-35.363261)
    ap.add_argument("--lon", type=float, default=149.165230)
    a = ap.parse_args()
    srv = DemoServer(a.port, a.lat, a.lon)
    srv.start()
    print(f"Demo drone listening on tcp:127.0.0.1:{a.port} (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
