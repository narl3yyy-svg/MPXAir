"""Map tile providers, disk cache, background fetching and offline area downloads.

Tiles are cached under ~/.cache/mpxair/tiles/<provider>/<z>/<x>/<y>.<ext>.
Network mode (settings 'map_mode'):
  online   - use cache, download missing tiles, refresh tiles older than map_max_age_days
  cache    - use cache, only download tiles that are missing
  offline  - never touch the network
"""
import math
import os
import queue
import threading
import time
import urllib.error
import urllib.request

from ..core import config

USER_AGENT = "MPXAir/1.0 (ArduPilot ground station; Linux)"


class Provider:
    def __init__(self, pid, name, urls, ext="jpg", max_zoom=19, satellite=False,
                 bulk_ok=True, attribution="", overlay=None):
        self.id = pid
        self.name = name
        self.urls = urls if isinstance(urls, list) else [urls]
        self.ext = ext
        self.max_zoom = max_zoom
        self.satellite = satellite
        self.bulk_ok = bulk_ok
        self.attribution = attribution
        self.overlay = overlay        # id of a provider drawn on top (labels)

    def url(self, z, x, y):
        tmpl = self.urls[(x + y) % len(self.urls)]
        return tmpl.format(z=z, x=x, y=y)


_ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/{}/MapServer/tile/{{z}}/{{y}}/{{x}}"

PROVIDERS = [
    Provider("esri_imagery", "Satellite (Esri World Imagery)", _ESRI.format("World_Imagery"),
             "jpg", 19, True, True, "Imagery © Esri, Maxar, Earthstar Geographics"),
    Provider("esri_hybrid", "Satellite + labels (Esri)", _ESRI.format("World_Imagery"),
             "jpg", 19, True, True, "Imagery © Esri; labels © Esri",
             overlay="esri_labels"),
    Provider("esri_street", "Street map (Esri)", _ESRI.format("World_Street_Map"),
             "jpg", 19, False, True, "© Esri, HERE, Garmin, OpenStreetMap contributors"),
    Provider("esri_topo", "Topographic (Esri)", _ESRI.format("World_Topo_Map"),
             "jpg", 19, False, True, "© Esri, HERE, Garmin, USGS"),
    Provider("osm", "OpenStreetMap", "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
             "png", 19, False, False, "© OpenStreetMap contributors"),
    Provider("opentopo", "OpenTopoMap (terrain)",
             ["https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
              "https://b.tile.opentopomap.org/{z}/{x}/{y}.png",
              "https://c.tile.opentopomap.org/{z}/{x}/{y}.png"],
             "png", 17, False, False, "© OpenTopoMap (CC-BY-SA), © OpenStreetMap contributors"),
]
# label layer used by the hybrid provider (not shown in the picker)
LABELS = Provider("esri_labels", "Labels",
                  _ESRI.format("Reference/World_Boundaries_and_Places"),
                  "png", 19, False, True, "")
_BY_ID = {p.id: p for p in PROVIDERS}
_BY_ID[LABELS.id] = LABELS


def get_provider(pid):
    return _BY_ID.get(pid) or PROVIDERS[0]


# ------------------------------------------------------------- projection
TILE = 256


def latlon_to_world(lat, lon, z):
    """Web Mercator: return global pixel coordinates at zoom z."""
    lat = max(-85.05112878, min(85.05112878, lat))
    n = TILE * (2 ** z)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def world_to_latlon(x, y, z):
    n = TILE * (2 ** z)
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def tiles_in_bbox(north, west, south, east, z):
    x0, y0 = latlon_to_world(north, west, z)
    x1, y1 = latlon_to_world(south, east, z)
    tx0, ty0 = int(x0 // TILE), int(y0 // TILE)
    tx1, ty1 = int(x1 // TILE), int(y1 // TILE)
    m = 2 ** z - 1
    return max(0, tx0), max(0, ty0), min(m, tx1), min(m, ty1)


def count_tiles(north, west, south, east, zmin, zmax):
    total = 0
    for z in range(zmin, zmax + 1):
        x0, y0, x1, y1 = tiles_in_bbox(north, west, south, east, z)
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return total


# ------------------------------------------------------------------ cache
def cache_root():
    return os.path.join(config.cache_dir(), "tiles")


def tile_path(provider, z, x, y):
    return os.path.join(cache_root(), provider.id, str(z), str(x), f"{y}.{provider.ext}")


def cache_size_bytes(pid=None):
    total = 0
    root = cache_root() if pid is None else os.path.join(cache_root(), pid)
    for dp, _dn, fns in os.walk(root):
        for f in fns:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


class NetState:
    """Tracks whether tile downloads are currently working (shown in Ground status)."""
    online = None          # None = unknown
    last_ok = 0.0
    last_fail = 0.0
    fail_count = 0

    @classmethod
    def ok(cls):
        cls.online = True
        cls.last_ok = time.time()
        cls.fail_count = 0

    @classmethod
    def fail(cls):
        cls.fail_count += 1
        cls.last_fail = time.time()
        if cls.fail_count >= 3:
            cls.online = False

    @classmethod
    def should_try(cls):
        # back off for 30 s after repeated failures, then probe again
        if cls.online is False and time.time() - cls.last_fail < 30:
            return False
        return True


def fetch_tile(provider, z, x, y, timeout=10):
    req = urllib.request.Request(provider.url(z, x, y), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        ctype = r.headers.get("Content-Type", "")
    if not data or ("image" not in ctype and not data[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb")):
        raise ValueError("not an image")
    return data


def store_tile(provider, z, x, y, data):
    path = tile_path(provider, z, x, y)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


class TileLoader:
    """Loads tiles for the map views: memory cache -> disk cache -> network (threads).
    on_ready(key) is called from a worker thread when a tile becomes available."""

    def __init__(self, settings, workers=4):
        self.settings = settings
        self.q = queue.LifoQueue()
        self.pending = set()
        self.lock = threading.Lock()
        self.listeners = []
        for i in range(workers):
            threading.Thread(target=self._worker, name=f"tiles-{i}", daemon=True).start()

    def add_listener(self, cb):
        self.listeners.append(cb)

    def mode(self):
        return self.settings.get("map_mode", "online")

    def request(self, provider, z, x, y, gen):
        key = (provider.id, z, x, y)
        with self.lock:
            if key in self.pending:
                return
            self.pending.add(key)
        self.q.put((gen, provider, z, x, y))

    def read_cached(self, provider, z, x, y):
        """Return (bytes or None, stale: bool)."""
        path = tile_path(provider, z, x, y)
        try:
            st = os.stat(path)
        except OSError:
            return None, True
        max_age = float(self.settings.get("map_max_age_days", 30)) * 86400
        stale = (time.time() - st.st_mtime) > max_age
        try:
            with open(path, "rb") as fh:
                return fh.read(), stale
        except OSError:
            return None, True

    def _worker(self):
        while True:
            gen, provider, z, x, y = self.q.get()
            key = (provider.id, z, x, y)
            data = None
            try:
                cached, stale = self.read_cached(provider, z, x, y)
                mode = self.mode()
                want_net = mode != "offline" and (cached is None or (stale and mode == "online"))
                if want_net and NetState.should_try():
                    try:
                        data = fetch_tile(provider, z, x, y)
                        store_tile(provider, z, x, y, data)
                        NetState.ok()
                    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
                        if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                            NetState.ok()
                        else:
                            NetState.fail()
                        data = None
                if data is None:
                    data = cached
            finally:
                with self.lock:
                    self.pending.discard(key)
            for cb in list(self.listeners):
                try:
                    cb(key, data, gen)
                except Exception as e:
                    print(f"[mpxair] tile listener: {e!r}")


class AreaDownloader(threading.Thread):
    """Download every tile of a bounding box over a zoom range (offline maps)."""

    def __init__(self, provider, bbox, zmin, zmax, refresh=False, progress=None, done=None):
        super().__init__(daemon=True, name="tile-area")
        self.provider = provider
        self.bbox = bbox             # (north, west, south, east)
        self.zmin, self.zmax = zmin, zmax
        self.refresh = refresh
        self.progress = progress
        self.done = done
        self.cancel = threading.Event()
        self.total = count_tiles(*bbox, zmin, zmax)
        self.count = 0
        self.failed = 0
        self.downloaded = 0

    def _jobs(self):
        provs = [self.provider]
        if self.provider.overlay:
            provs.append(get_provider(self.provider.overlay))
        for z in range(self.zmin, self.zmax + 1):
            x0, y0, x1, y1 = tiles_in_bbox(*self.bbox, z)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    yield provs, z, x, y

    def run(self):
        err = None
        for provs, z, x, y in self._jobs():
            if self.cancel.is_set():
                err = "Cancelled"
                break
            for p in provs:
                path = tile_path(p, z, x, y)
                if not self.refresh and os.path.exists(path):
                    continue
                for attempt in range(3):
                    try:
                        store_tile(p, z, x, y, fetch_tile(p, z, x, y, timeout=15))
                        self.downloaded += 1
                        NetState.ok()
                        break
                    except urllib.error.HTTPError as e:
                        if e.code == 404:
                            break
                        time.sleep(1 + attempt)
                    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                        time.sleep(1 + attempt)
                else:
                    self.failed += 1
                    NetState.fail()
                time.sleep(0.02)       # be polite to the tile servers
            self.count += 1
            if self.progress:
                self.progress(self.count, self.total, self.failed)
            if self.failed > 50 and self.downloaded == 0:
                err = "Could not reach the map server. Check your internet connection."
                break
        if self.done:
            self.done(err, self)
