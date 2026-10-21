"""Mission, geofence and rally point transfer, plus .waypoints file IO."""
import os
import time
import xml.etree.ElementTree as ET

from ..mavlink import defs

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

MISSION, FENCE, RALLY = 0, 1, 2
GLOBAL_FRAMES = {0, 3, 5, 6, 10, 11}
FRAME_NAMES = {3: "Relative", 0: "Absolute", 10: "Terrain", 2: "Mission"}

CMD_NAMES = {v: n[0].replace("MAV_CMD_", "") for v, n in defs.ENUMS["MAV_CMD"][1].items()}
CMD_IDS = {n: v for v, n in CMD_NAMES.items()}
# MP's short names (mavcmd.xml) map to MAV_CMD_NAV_x or MAV_CMD_x
_SHORT = {n.replace("NAV_", "", 1): v for v, n in CMD_NAMES.items() if n.startswith("NAV_")}


class MissionItem:
    __slots__ = ("seq", "frame", "command", "current", "autocontinue",
                 "p1", "p2", "p3", "p4", "x", "y", "z", "mission_type")

    def __init__(self, command=16, x=0.0, y=0.0, z=0.0, p1=0.0, p2=0.0, p3=0.0, p4=0.0,
                 frame=3, seq=0, current=0, autocontinue=1, mission_type=MISSION):
        self.seq, self.frame, self.command = seq, frame, command
        self.current, self.autocontinue = current, autocontinue
        self.p1, self.p2, self.p3, self.p4 = p1, p2, p3, p4
        self.x, self.y, self.z = x, y, z
        self.mission_type = mission_type

    def copy(self):
        return MissionItem(self.command, self.x, self.y, self.z, self.p1, self.p2, self.p3,
                           self.p4, self.frame, self.seq, self.current, self.autocontinue,
                           self.mission_type)

    @property
    def name(self):
        return command_short_name(self.command)

    @property
    def has_location(self):
        return self.frame in GLOBAL_FRAMES and not (self.x == 0 and self.y == 0) \
            and self.command in LOCATION_CMDS

    def to_msg_kwargs(self, target):
        if self.frame in GLOBAL_FRAMES:
            x, y = int(round(self.x * 1e7)), int(round(self.y * 1e7))
        else:
            x, y = int(round(self.x)), int(round(self.y))
        return dict(**target, seq=self.seq, frame=self.frame, command=self.command,
                    current=self.current, autocontinue=self.autocontinue,
                    param1=self.p1, param2=self.p2, param3=self.p3, param4=self.p4,
                    x=x, y=y, z=float(self.z), mission_type=self.mission_type)

    @classmethod
    def from_msg(cls, m):
        if m._def.name == "MISSION_ITEM_INT":
            if m.frame in GLOBAL_FRAMES:
                x, y = m.x / 1e7, m.y / 1e7
            else:
                x, y = float(m.x), float(m.y)
        else:
            x, y = m.x, m.y
        return cls(m.command, x, y, m.z, m.param1, m.param2, m.param3, m.param4,
                   m.frame, m.seq, m.current, m.autocontinue, getattr(m, "mission_type", 0))


# commands whose x/y are a location
LOCATION_CMDS = {16, 17, 18, 19, 21, 30, 31, 82, 85, 94, 201, 195, 5000, 5001,
                 5002, 5003, 5004, 5100, 5101, 179, 189, 36}


def command_short_name(cmd):
    n = CMD_NAMES.get(cmd, str(cmd))
    return n[4:] if n.startswith("NAV_") else n


def command_id(short):
    if short in _SHORT:
        return _SHORT[short]
    return CMD_IDS.get(short)


class CommandLabels:
    """Per-vehicle command list and parameter labels (mavcmd.xml, as used by MP)."""
    SECTION = {"copter": "AC2", "plane": "APM", "rover": "APRover", "sub": "AC2",
               "tracker": "AC2"}
    _cache = {}

    @classmethod
    def for_class(cls, vclass):
        sec = cls.SECTION.get(vclass, "AC2")
        if sec in cls._cache:
            return cls._cache[sec]
        out = {}
        try:
            root = ET.parse(os.path.join(DATA_DIR, "mavcmd.xml")).getroot()
            node = root.find(sec)
            for c in (node if node is not None else []):
                cid = command_id(c.tag)
                if cid is None:
                    continue
                labels = [(c.findtext(t) or "").strip() for t in ("P1", "P2", "P3", "P4", "X", "Y", "Z")]
                out[cid] = labels
        except (OSError, ET.ParseError):
            pass
        if not out:
            out = {16: ["Delay", "", "", "", "Lat", "Long", "Alt"]}
        cls._cache[sec] = out
        return out


class MissionManager:
    def __init__(self, vehicle):
        self.v = vehicle

    def download(self, mission_type=MISSION, progress=None, cancel=None):
        """Returns list of MissionItem or raises RuntimeError. Worker thread only."""
        v = self.v
        tgt = v.target
        cnt = v.request_and_wait(
            lambda: v.send("MISSION_REQUEST_LIST", **tgt, mission_type=mission_type),
            "MISSION_COUNT", lambda m: getattr(m, "mission_type", 0) == mission_type,
            timeout=2.0, retries=4)
        if cnt is None:
            raise RuntimeError("The vehicle did not answer the mission request.")
        items = []
        for seq in range(cnt.count):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("Cancelled")
            m = v.request_and_wait(
                lambda s=seq: v.send("MISSION_REQUEST_INT", **tgt, seq=s, mission_type=mission_type),
                ("MISSION_ITEM_INT", "MISSION_ITEM"), lambda m, s=seq: m.seq == s,
                timeout=1.5, retries=5)
            if m is None:
                raise RuntimeError(f"Timed out reading item {seq} of {cnt.count}.")
            items.append(MissionItem.from_msg(m))
            if progress:
                progress(seq + 1, cnt.count)
        v.send("MISSION_ACK", **tgt, type=0, mission_type=mission_type)
        return items

    def upload(self, items, mission_type=MISSION, progress=None, cancel=None):
        """Upload items (seq renumbered). Raises RuntimeError on failure. Worker thread only."""
        v = self.v
        tgt = v.target
        items = [it.copy() for it in items]
        for i, it in enumerate(items):
            it.seq = i
            it.mission_type = mission_type
            it.current = 0
        n = len(items)
        names = ("MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK")
        cond = lambda m: getattr(m, "mission_type", 0) == mission_type
        req = v.request_and_wait(
            lambda: v.send("MISSION_COUNT", **tgt, count=n, mission_type=mission_type),
            names, cond, timeout=2.0, retries=4)
        sent = set()
        while True:
            if req is None:
                raise RuntimeError("The vehicle stopped responding during upload.")
            if cancel is not None and cancel.is_set():
                v.send("MISSION_ACK", **tgt, type=1, mission_type=mission_type)
                raise RuntimeError("Cancelled")
            if req._def.name == "MISSION_ACK":
                if req.type == 0:
                    if progress:
                        progress(n, n)
                    return True
                err = defs.ENUMS["MAV_MISSION_RESULT"][1].get(req.type, (str(req.type), ""))
                raise RuntimeError(f"The vehicle rejected the mission: {err[1] or err[0]}")
            seq = req.seq
            if seq >= n:
                raise RuntimeError(f"Vehicle asked for item {seq} but only {n} exist.")
            item = items[seq]
            sent.add(seq)
            if progress:
                progress(len(sent), n)
            req = v.request_and_wait(
                lambda it=item: v.send("MISSION_ITEM_INT", **it.to_msg_kwargs(tgt)),
                names, cond, timeout=1.5, retries=5)

    def clear(self, mission_type=MISSION):
        v = self.v
        ack = v.request_and_wait(
            lambda: v.send("MISSION_CLEAR_ALL", **v.target, mission_type=mission_type),
            "MISSION_ACK", None, timeout=2.0, retries=3)
        return ack is not None and ack.type == 0


# ------------------------------------------------------------------ file IO
def save_waypoints(path, items):
    """QGC WPL 110 format, which Mission Planner reads and writes."""
    with open(path, "w") as fh:
        fh.write("QGC WPL 110\n")
        for i, it in enumerate(items):
            fh.write("\t".join([
                str(i), "1" if i == 0 else "0", str(it.frame), str(it.command),
                f"{it.p1:.8f}", f"{it.p2:.8f}", f"{it.p3:.8f}", f"{it.p4:.8f}",
                f"{it.x:.8f}", f"{it.y:.8f}", f"{it.z:.6f}", str(it.autocontinue),
            ]) + "\n")


def load_waypoints(path):
    items = []
    with open(path, "r", errors="replace") as fh:
        header = fh.readline().strip()
        if not header.startswith("QGC WPL"):
            raise ValueError("Not a .waypoints file (missing 'QGC WPL' header)")
        for line in fh:
            parts = line.strip().split("\t") if "\t" in line else line.split()
            if len(parts) < 12:
                continue
            it = MissionItem(
                command=int(float(parts[3])), frame=int(float(parts[2])),
                p1=float(parts[4]), p2=float(parts[5]), p3=float(parts[6]), p4=float(parts[7]),
                x=float(parts[8]), y=float(parts[9]), z=float(parts[10]),
                autocontinue=int(float(parts[11])), seq=int(float(parts[0])))
            items.append(it)
    return items


def save_poly(path, points):
    with open(path, "w") as fh:
        fh.write("#saved by MPXAir\n")
        for lat, lon in points:
            fh.write(f"{lat:.8f} {lon:.8f}\n")


def load_poly(path):
    pts = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(",", " ").split()
            if len(parts) >= 2:
                pts.append((float(parts[0]), float(parts[1])))
    return pts
