"""Settings (JSON in ~/.config/mpxair) and standard folders.

MPXAIR_HOME overrides every folder (used by the portable run.sh --portable mode).
"""
import json
import os
import threading

APP_ID = "io.github.narl3yyy_svg.MPXAir"
REPO_URL = "https://github.com/narl3yyy-svg/MPXAir"
DOCS_URL = "https://narl3yyy-svg.github.io/MPXAir/"
VERSION = "1.2.1"


def _base(env, default):
    home = os.environ.get("MPXAIR_HOME")
    if home:
        return os.path.join(home, os.path.basename(default))
    return os.environ.get(env) and os.path.join(os.environ[env], "mpxair") or default


def config_dir():
    d = _base("XDG_CONFIG_HOME", os.path.expanduser("~/.config/mpxair"))
    os.makedirs(d, exist_ok=True)
    return d


def cache_dir():
    d = _base("XDG_CACHE_HOME", os.path.expanduser("~/.cache/mpxair"))
    os.makedirs(d, exist_ok=True)
    return d


def data_dir():
    """User documents: telemetry logs, downloaded logs, missions."""
    home = os.environ.get("MPXAIR_HOME")
    d = os.path.join(home, "data") if home else os.path.expanduser("~/Documents/MPXAir")
    os.makedirs(d, exist_ok=True)
    return d


def tlog_dir():
    d = os.path.join(data_dir(), "telemetry-logs")
    os.makedirs(d, exist_ok=True)
    return d


def log_dir():
    d = os.path.join(data_dir(), "flight-logs")
    os.makedirs(d, exist_ok=True)
    return d


DEFAULTS = {
    "connection": {"type": "serial", "device": "", "baud": 115200, "host": "127.0.0.1",
                   "port": 14550, "tcp_port": 5760},
    "map_provider": "esri_hybrid",
    "map_mode": "online",
    "map_max_age_days": 30,
    "map_center": [-35.3632, 149.1652],
    "map_zoom": 16,
    "offline_areas": [],
    "default_alt": 50,
    "wp_radius": 2,
    "units_speed": "m/s",
    "units_alt": "m",
    "record_tlog": True,
    "speech": False,
    "stream_rates": {"attitude": 10, "position": 4, "status": 2, "sensors": 2, "rc": 2},
    "quick_fields": ["alt", "groundspeed", "dist_home", "climb", "yaw", "voltage"],
    "theme": "system",
    "ground_gps": {"source": "none", "device": "", "baud": 9600, "lat": 0.0, "lon": 0.0, "alt": 0.0},
    "tracker": {"type": "maestro", "device": "", "baud": 9600,
                "pan_ch": 0, "tilt_ch": 1, "pan_min": 992, "pan_max": 2000,
                "pan_range": 360, "pan_trim": 0, "tilt_min": 992, "tilt_max": 2000,
                "tilt_range": 90, "tilt_trim": 0, "pan_rev": False, "tilt_rev": False,
                "mav_host": "", "mav_port": 0},
    "follow_me": {"alt": 20, "offset_m": 0, "min_move_m": 2, "rate_hz": 2},
    "window": [1440, 900],
    "last_tab": "fly",
}


class Settings:
    def __init__(self):
        self.path = os.path.join(config_dir(), "settings.json")
        self.lock = threading.Lock()
        self.data = json.loads(json.dumps(DEFAULTS))
        try:
            with open(self.path) as fh:
                loaded = json.load(fh)
            for k, v in loaded.items():
                if isinstance(v, dict) and isinstance(self.data.get(k), dict):
                    self.data[k].update(v)
                else:
                    self.data[k] = v
        except (OSError, ValueError):
            pass

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]

    def set(self, key, value, save=True):
        self.data[key] = value
        if save:
            self.save()

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w") as fh:
                    json.dump(self.data, fh, indent=2)
                os.replace(tmp, self.path)
            except OSError as e:
                print(f"[mpxair] could not save settings: {e}")
