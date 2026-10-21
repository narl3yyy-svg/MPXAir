"""Ground-station board profiles: how to read the GPS of a ground station that shares the
telemetry serial link with the aircraft.

Every profile has a `kind` that decides how replies are parsed, plus settings the user
can change on the Ground tab. User changes are stored per profile in
settings["ground_profiles"][profile_id] and override the defaults below.
"""
import copy
import json

# kind:
#   text_json  poll with a text command; replies are "<prefix>{json}" lines
#   text_csv   poll with a text command; replies are "<prefix>v1,v2,…" lines
#   telemetry  NMEA sentences or MAVLink GPS messages already mixed into the telemetry
#   mavlink    MAVLink GPS/position messages from a ground system on the link
PRESETS = {
    "rfd_esp32": {
        "name": "RFD900 + ESP32 ground station board",
        "about": "The RFD900 / ESP32-S3 router board (gcs-settings firmware). Its GPS is wired to the "
                 "ESP32 and read with the board's \"#GCS status\" command.",
        "kind": "text_json",
        "poll": "#GCS status", "hello": "#GCS hello", "prefix": "#GCS ", "interval": 1.0,
        "line_end": "\\r",
        "lat": "gps.lat", "lon": "gps.lon", "pos_scale": 1e-7,
        "alt": "gps.altMm", "alt_scale": 0.001,
        "sats": "gps.sats", "fix": "gps.fix", "hdop": "gps.hdop",
        "name_field": "name", "fw_field": "fw", "require_field": "fw",
        "follow_on": "#GCS follow 1", "follow_off": "#GCS follow 0", "follow_state": "follow",
        "release_lines": True,
    },
    "telemetry": {
        "name": "GPS mixed into the telemetry (NMEA or MAVLink)",
        "about": "For ground stations that already send their position on the telemetry link without "
                 "being asked: NMEA sentences ($GPGGA/$GNRMC…) or MAVLink GPS messages. "
                 "Nothing is sent to the board.",
        "kind": "telemetry",
        "sysid": 0, "release_lines": False,
    },
    "mavlink_gps": {
        "name": "MAVLink GPS from the ground station",
        "about": "The ground station sends MAVLink GPS_RAW_INT, GLOBAL_POSITION_INT or GPS_INPUT with "
                 "its own system ID — for example a ground unit broadcasting its position as system 255 "
                 "for ArduPilot's Follow mode.",
        "kind": "mavlink",
        "sysid": 255, "release_lines": False,
    },
    "custom_json": {
        "name": "Custom board — JSON replies",
        "about": "Any board that answers a text command with a line such as  "
                 "#GND {\"lat\":-35.36,\"lon\":149.16,\"alt\":584,\"sats\":11,\"fix\":3}. "
                 "Set the command, the reply prefix and the JSON field names below.",
        "kind": "text_json",
        "poll": "#GND?", "hello": "", "prefix": "#GND ", "interval": 1.0, "line_end": "\\r\\n",
        "lat": "lat", "lon": "lon", "pos_scale": 1.0,
        "alt": "alt", "alt_scale": 1.0,
        "sats": "sats", "fix": "fix", "hdop": "hdop",
        "name_field": "", "fw_field": "", "require_field": "",
        "follow_on": "", "follow_off": "", "follow_state": "",
        "release_lines": False,
    },
    "custom_csv": {
        "name": "Custom board — comma-separated replies",
        "about": "Any board that sends lines such as  $GND,-35.363261,149.165230,584.0,11,3  "
                 "(on request or by itself). Set the prefix and which column holds each value "
                 "(the first value after the prefix is column 1).",
        "kind": "text_csv",
        "poll": "", "hello": "", "prefix": "$GND,", "interval": 1.0, "line_end": "\\r\\n",
        "lat": 1, "lon": 2, "pos_scale": 1.0, "alt": 3, "alt_scale": 1.0,
        "sats": 4, "fix": 5, "hdop": 0,
        "release_lines": False,
    },
}
ORDER = ["rfd_esp32", "telemetry", "mavlink_gps", "custom_json", "custom_csv"]

# Field descriptions for the settings form: key -> (title, help, type)
FIELDS = {
    "poll": ("Poll command", "Text sent to the board to ask for its status. Leave empty if the board "
             "sends its position by itself.", "str"),
    "hello": ("Start command", "Sent once when the link opens (optional).", "str"),
    "prefix": ("Reply prefix", "Replies start with this text. Everything after it is the data.", "str"),
    "line_end": ("Line ending", "Added after each command: \\r, \\n or \\r\\n.", "str"),
    "interval": ("Poll every (seconds)", "How often the poll command is sent.", "float"),
    "lat": ("Latitude field", "JSON path (e.g. gps.lat) or column number.", "field"),
    "lon": ("Longitude field", "JSON path or column number.", "field"),
    "pos_scale": ("Position scale", "Multiply latitude/longitude by this. 0.0000001 when the board sends "
                  "degrees × 10⁷, 1 when it sends plain degrees.", "float"),
    "alt": ("Altitude field", "Altitude above sea level. Leave empty or 0 if not sent.", "field"),
    "alt_scale": ("Altitude scale", "0.001 when the board sends millimetres, 1 for metres.", "float"),
    "sats": ("Satellites field", "Number of satellites (optional).", "field"),
    "fix": ("Fix field", "True/false, or a number where 0 means no fix. Leave empty if the "
            "board only sends a position when it has a fix.", "field"),
    "hdop": ("HDOP field", "Optional.", "field"),
    "name_field": ("Board name field", "Shown on the Ground tab (optional).", "field"),
    "fw_field": ("Firmware field", "Shown on the Ground tab (optional).", "field"),
    "require_field": ("Only use replies that contain", "Ignore other replies that don't have this field "
                      "(optional).", "field"),
    "follow_on": ("Command: start sending position to aircraft", "Optional. For boards that can "
                  "broadcast their position for ArduPilot's Follow mode.", "str"),
    "follow_off": ("Command: stop sending position to aircraft", "Optional.", "str"),
    "follow_state": ("Field that says whether it is sending", "Optional.", "field"),
    "sysid": ("System ID", "Only use GPS messages from this MAVLink system. 0 = any system that isn't "
              "the aircraft.", "int"),
    "release_lines": ("Release DTR and RTS when connecting",
                      "Needed by ESP32 boards, which can reset when these serial control lines change.",
                      "bool"),
}

EDITABLE = {
    "text_json": ["poll", "hello", "prefix", "line_end", "interval", "lat", "lon", "pos_scale", "alt",
                  "alt_scale", "sats", "fix", "hdop", "name_field", "fw_field", "require_field",
                  "follow_on", "follow_off", "follow_state", "release_lines"],
    "text_csv": ["poll", "hello", "prefix", "line_end", "interval", "lat", "lon", "pos_scale", "alt",
                 "alt_scale", "sats", "fix", "hdop", "release_lines"],
    "telemetry": ["sysid", "release_lines"],
    "mavlink": ["sysid", "release_lines"],
}


def profile(settings, pid):
    """Defaults merged with the user's saved changes."""
    base = copy.deepcopy(PRESETS.get(pid) or PRESETS["telemetry"])
    saved = (settings.get("ground_profiles") or {}).get(pid) or {}
    for k, v in saved.items():
        if k in base:
            base[k] = v
    base["id"] = pid if pid in PRESETS else "telemetry"
    return base


def save_profile(settings, pid, values):
    allp = dict(settings.get("ground_profiles") or {})
    allp[pid] = values
    settings.set("ground_profiles", allp)


def reset_profile(settings, pid):
    allp = dict(settings.get("ground_profiles") or {})
    allp.pop(pid, None)
    settings.set("ground_profiles", allp)


def unescape(s):
    return (s or "").replace("\\r", "\r").replace("\\n", "\n").replace("\\t", "\t")


# ------------------------------------------------------------------ parsing
def json_get(obj, path):
    if path in (None, "", 0):
        return None
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def csv_get(values, col):
    try:
        col = int(col)
    except (TypeError, ValueError):
        return None
    if col <= 0 or col > len(values):
        return None
    return values[col - 1].strip()


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fix_ok(v):
    """True/false, or a number where 0 means no fix (works for NMEA quality and fix-type values)."""
    if v is None or v == "":
        return True            # board doesn't say: assume a position means a fix
    if isinstance(v, bool):
        return v
    n = _num(v)
    if n is None:
        return str(v).strip().lower() in ("true", "yes", "fix", "2d", "3d", "a")
    return n >= 1


def parse_reply(p, line):
    """Turn one reply line (str, prefix included) into a dict with lat/lon/alt/sats/hdop/fix,
    plus 'raw' (the decoded object) — or None if it doesn't hold a position report."""
    data = line[len(p["prefix"]):].strip()
    if p["kind"] == "text_json":
        try:
            obj = json.loads(data)
        except ValueError:
            obj = None
            try:
                obj = json.loads("".join(ch for ch in data if 32 <= ord(ch) < 127))
            except ValueError:
                return None
        if not isinstance(obj, dict):
            return None
        if p.get("require_field") and json_get(obj, p["require_field"]) is None:
            return None
        get = lambda f: json_get(obj, f)
        raw = obj
    else:
        vals = data.split(",")
        get = lambda f: csv_get(vals, f)
        raw = vals
    lat, lon = _num(get(p["lat"])), _num(get(p["lon"]))
    out = {"raw": raw, "fix": fix_ok(get(p.get("fix"))), "sats": _num(get(p.get("sats"))),
           "hdop": _num(get(p.get("hdop"))), "alt": None, "lat": None, "lon": None}
    if lat is not None and lon is not None:
        s = float(p.get("pos_scale") or 1.0)
        out["lat"], out["lon"] = lat * s, lon * s
        if not (-90 <= out["lat"] <= 90 and -180 <= out["lon"] <= 180) or (out["lat"] == 0 and out["lon"] == 0):
            out["lat"] = out["lon"] = None
    a = _num(get(p.get("alt")))
    if a is not None:
        out["alt"] = a * float(p.get("alt_scale") or 1.0)
    if p["kind"] == "text_json":
        out["name"] = get(p.get("name_field")) if p.get("name_field") else None
        out["fw"] = get(p.get("fw_field")) if p.get("fw_field") else None
        out["follow"] = get(p.get("follow_state")) if p.get("follow_state") else None
    return out
