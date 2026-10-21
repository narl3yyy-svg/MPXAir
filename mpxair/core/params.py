"""Parameter download / set, .param file IO, and parameter documentation."""
import collections
import math
import os
import threading
import time
import xml.etree.ElementTree as ET

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

INT_TYPES = {1, 2, 3, 4, 5, 6, 7, 8}       # MAV_PARAM_TYPE_UINT8 .. INT64


class ParamManager:
    def __init__(self, vehicle):
        self.v = vehicle
        self.values = collections.OrderedDict()
        self.types = {}
        self.index = {}
        self.count = 0
        self.complete = False
        self._lock = threading.Lock()
        self._received = set()
        self._last_rx = 0.0
        vehicle.on("PARAM_VALUE", self._on_value)

    def reset(self):
        with self._lock:
            self.values.clear()
            self.types.clear()
            self.index.clear()
            self.count = 0
            self.complete = False
            self._received = set()

    def _on_value(self, m):
        if self.v.state.sysid and m._sysid != self.v.state.sysid:
            return
        name = m.param_id
        val = m.param_value
        if m.param_type in INT_TYPES and math.isfinite(val):
            val = int(round(val))
        with self._lock:
            new = name not in self.values
            self.values[name] = val
            self.types[name] = m.param_type
            if m.param_index != 65535:
                self.index[name] = m.param_index
                self._received.add(m.param_index)
            if m.param_count and m.param_count != 65535:
                self.count = m.param_count
            self._last_rx = time.monotonic()
            if self.count and len(self._received) >= self.count:
                self.complete = True
        self.v.emit("param", name, val, new)

    def get(self, name, default=None):
        return self.values.get(name, default)

    def fetch(self, progress=None, cancel=None):
        """Download all parameters. progress(received, total). Worker thread only.
        Returns True when the full list arrived."""
        self.reset()
        v = self.v
        tgt = v.target
        self._last_rx = time.monotonic()
        v.send("PARAM_REQUEST_LIST", **tgt)
        start = time.monotonic()
        rounds_without_progress = 0
        last_count = 0
        while True:
            if cancel is not None and cancel.is_set():
                return False
            if not v.is_open:
                return False
            time.sleep(0.1)
            with self._lock:
                got = len(self._received)
                total = self.count
            if progress:
                progress(got, total)
            if total and got >= total:
                self.complete = True
                return True
            idle = time.monotonic() - self._last_rx
            if not total:
                if time.monotonic() - start > 6:
                    # nothing at all: re-request the list a couple of times
                    if rounds_without_progress >= 3:
                        return False
                    rounds_without_progress += 1
                    start = time.monotonic()
                    v.send("PARAM_REQUEST_LIST", **tgt)
                continue
            if idle > 1.0:
                # stream has paused: ask for the missing ones individually
                with self._lock:
                    missing = [i for i in range(total) if i not in self._received]
                for i in missing[:20]:
                    v.send("PARAM_REQUEST_READ", **tgt, param_id="", param_index=i)
                    time.sleep(0.005)
                self._last_rx = time.monotonic()
                if got == last_count:
                    rounds_without_progress += 1
                    if rounds_without_progress > 15:
                        return False
                else:
                    rounds_without_progress = 0
                last_count = got

    def set(self, name, value, retries=3):
        """Set a parameter and wait for the autopilot to echo it. Worker thread only.
        Returns (ok, message)."""
        v = self.v
        ptype = self.types.get(name, 9)
        fval = float(value)

        def send():
            v.send("PARAM_SET", **v.target, param_id=name, param_value=fval, param_type=ptype)

        m = v.request_and_wait(send, "PARAM_VALUE", lambda m: m.param_id == name,
                               timeout=1.5, retries=retries)
        if m is None:
            return False, f"No reply when setting {name}"
        got = m.param_value
        if abs(got - fval) > max(1e-4, abs(fval) * 1e-5):
            return False, f"{name} was rejected (it is still {got:g})"
        return True, "ok"

    def refresh(self, name):
        v = self.v
        m = v.request_and_wait(
            lambda: v.send("PARAM_REQUEST_READ", **v.target, param_id=name, param_index=-1),
            "PARAM_VALUE", lambda m: m.param_id == name, timeout=1.0, retries=3)
        return m.param_value if m else None


def format_value(val):
    if isinstance(val, int):
        return str(val)
    if not math.isfinite(val):
        return str(val)
    s = f"{val:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def save_param_file(path, values):
    with open(path, "w") as fh:
        fh.write(f"# MPXAir parameter file, saved {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        for name in sorted(values):
            fh.write(f"{name},{format_value(values[name])}\n")


# never written from a file (counters, calibration ground values, read-only info)
SKIP_ON_LOAD = {"SYSID_SW_MREV", "WP_TOTAL", "CMD_TOTAL", "FENCE_TOTAL", "SYS_NUM_RESETS",
                "ARSPD_OFFSET", "GND_ABS_PRESS", "GND_TEMP", "BARO1_GND_PRESS", "BARO2_GND_PRESS",
                "BARO3_GND_PRESS", "BARO_GND_TEMP", "CMD_INDEX", "LOG_LASTFILE", "FORMAT_VERSION",
                "STAT_BOOTCNT", "STAT_FLTTIME", "STAT_RUNTIME", "STAT_RESET", "SYSID_SW_TYPE"}


def load_param_file(path):
    """Read MP (NAME,VALUE), MAVProxy (NAME VALUE) and QGC (tab separated) formats."""
    out = collections.OrderedDict()
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                parts = line.split("\t")
                if len(parts) >= 4 and parts[0].isdigit():
                    name, val = parts[2], parts[3]       # QGC: sys comp name value type
                else:
                    name, val = parts[0], parts[1]
            elif "," in line:
                name, val = line.split(",", 1)
            else:
                parts = line.split()
                if len(parts) < 2:
                    continue
                name, val = parts[0], parts[1]
            name = name.strip()
            try:
                f = float(val.strip())
            except ValueError:
                continue
            out[name] = int(f) if f.is_integer() and "." not in val else f
    return out


class ParamDocs:
    """Parameter documentation.

    Bundled: ParameterMetaDataBackup.xml (the same file Mission Planner ships).
    Optional: newer apm.pdef.xml files downloaded from autotest.ardupilot.org, which
    take priority when present (they cover parameters added in newer firmware).
    """
    SECTION = {"copter": "ArduCopter2", "plane": "ArduPlane", "rover": "ArduRover",
               "sub": "ArduSub", "tracker": "ArduTracker"}
    ONLINE = {"copter": "ArduCopter", "plane": "ArduPlane", "rover": "Rover",
              "sub": "ArduSub", "tracker": "AntennaTracker"}
    URL = "https://autotest.ardupilot.org/Parameters/{}/apm.pdef.xml"
    # descriptions that exactly match a released firmware (what Mission Planner uses)
    VERSIONED = {"copter": "Copter", "plane": "Plane", "rover": "Rover", "sub": "Sub", "tracker": "Tracker"}
    VURL = "https://autotest.ardupilot.org/Parameters/versioned/{}/stable-{}/apm.pdef.xml"

    _cache = {}
    _version = {}           # vclass -> "4.7.1" of the connected aircraft

    @classmethod
    def online_dir(cls):
        from . import config
        d = os.path.join(config.cache_dir(), "paramdocs")
        os.makedirs(d, exist_ok=True)
        return d

    @classmethod
    def for_class(cls, vclass):
        """Bundled docs, overridden by ArduPilot's latest (if downloaded), overridden by the
        docs for the connected aircraft's exact firmware version (if downloaded)."""
        key = vclass if vclass in cls.SECTION else "copter"
        ver = cls._version.get(key)
        ck = (key, ver)
        if ck not in cls._cache:
            docs = cls._load_bundled(cls.SECTION[key])
            docs.update(cls._load_pdef(os.path.join(cls.online_dir(), cls.ONLINE[key] + ".xml")))
            if ver:
                docs.update(cls._load_pdef(cls._versioned_path(key, ver)))
            cls._cache[ck] = docs
        return cls._cache[ck]

    @classmethod
    def _versioned_path(cls, vclass, ver):
        return os.path.join(cls.online_dir(), f"{cls.VERSIONED.get(vclass, 'Copter')}-{ver}.xml")

    @staticmethod
    def version_of(flight_sw_version):
        """'4.7.1' for an official release, else None (betas/dev builds have no versioned docs)."""
        sw = int(flight_sw_version or 0)
        if not sw or (sw & 0xFF) != 255:
            return None
        return f"{(sw >> 24) & 0xFF}.{(sw >> 16) & 0xFF}.{(sw >> 8) & 0xFF}"

    @classmethod
    def use_firmware(cls, vclass, flight_sw_version, download=True):
        """Pick the docs for this firmware, downloading them once if needed (worker thread).
        Returns a short status text."""
        ver = cls.version_of(flight_sw_version)
        key = vclass if vclass in cls.SECTION else "copter"
        if not ver or key not in cls.VERSIONED:
            return None
        path = cls._versioned_path(key, ver)
        if not os.path.exists(path) and download:
            import urllib.request
            try:
                req = urllib.request.Request(cls.VURL.format(cls.VERSIONED[key], ver),
                                             headers={"User-Agent": "MPXAir/1.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                ET.fromstring(data)
                with open(path + ".part", "wb") as fh:
                    fh.write(data)
                os.replace(path + ".part", path)
            except Exception as e:
                cls._version.pop(key, None)
                return f"Descriptions for {ver} couldn't be downloaded ({e.__class__.__name__}); using the bundled ones"
        if os.path.exists(path):
            cls._version[key] = ver
            return f"Descriptions matched to firmware {ver}"
        return None

    @classmethod
    def source_text(cls):
        path = os.path.join(cls.online_dir(), "ArduCopter.xml")
        if os.path.exists(path):
            return "Bundled, plus ArduPilot's latest (downloaded " + \
                time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path))) + ")"
        return "Bundled with MPXAir (from Mission Planner)"

    @classmethod
    def update_online(cls):
        import urllib.request
        n = 0
        for vclass, name in cls.ONLINE.items():
            req = urllib.request.Request(cls.URL.format(name), headers={"User-Agent": "MPXAir/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            ET.fromstring(data)          # must parse before we keep it
            path = os.path.join(cls.online_dir(), name + ".xml")
            with open(path + ".part", "wb") as fh:
                fh.write(data)
            os.replace(path + ".part", path)
            for k in [k for k in cls._cache if k[0] == vclass]:
                cls._cache.pop(k, None)
            n += 1
        return n

    @staticmethod
    def _finish(d):
        if "Values" in d and "values" not in d:
            vals = []
            for part in d["Values"].split(","):
                if ":" in part:
                    k, lbl = part.split(":", 1)
                    try:
                        vals.append((float(k), lbl.strip()))
                    except ValueError:
                        pass
            d["values"] = vals
        if "Bitmask" in d and "bits" not in d:
            bits = []
            for part in d["Bitmask"].split(","):
                if ":" in part:
                    k, lbl = part.split(":", 1)
                    try:
                        bits.append((int(k), lbl.strip()))
                    except ValueError:
                        pass
            d["bits"] = bits
        if "Range" in d and "range" not in d:
            try:
                lo, hi = d["Range"].split()
                d["range"] = (float(lo), float(hi))
            except ValueError:
                pass
        return d

    @classmethod
    def _load_bundled(cls, section):
        path = os.path.join(DATA_DIR, "ParameterMetaDataBackup.xml")
        docs = {}
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            return docs
        sec = root.find(section)
        if sec is None:
            return docs
        for p in sec:
            d = {"name": p.tag}
            for child in p:
                d[child.tag] = (child.text or "").strip()
            docs[p.tag] = cls._finish(d)
        return docs

    @classmethod
    def _load_pdef(cls, path):
        docs = {}
        if not os.path.exists(path):
            return docs
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            return docs
        for p in root.iter("param"):
            name = p.get("name", "").split(":")[-1]
            if not name:
                continue
            d = {"name": name, "DisplayName": p.get("humanName", ""),
                 "Description": p.get("documentation", ""), "User": p.get("user", "")}
            for f in p.findall("field"):
                d[f.get("name")] = (f.text or "").strip()
            vals = p.find("values")
            if vals is not None:
                out = []
                for v in vals.findall("value"):
                    try:
                        out.append((float(v.get("code")), (v.text or "").strip()))
                    except (TypeError, ValueError):
                        pass
                d["values"] = out
            docs[name] = cls._finish(d)
        return docs
