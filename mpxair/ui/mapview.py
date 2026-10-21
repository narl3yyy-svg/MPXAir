"""Slippy map widget drawn with Gtk.Snapshot (GPU tiles) and Cairo overlays.

Pages add overlay layers: objects with draw(cr, view) and optionally
hit(view, x, y) -> handle, drag(view, handle, lat, lon), drag_end(view, handle).
"""
import collections
import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

from ..maps import tiles  # noqa: E402
from .util import snapshot_cairo  # noqa: E402

TILE = tiles.TILE
MAX_TEXTURES = 900


class TextureCache:
    """Shared across map views: (provider, z, x, y) -> Gdk.Texture (or False = missing)."""

    def __init__(self, loader):
        self.loader = loader
        self.items = collections.OrderedDict()
        self.views = []
        self.gen = 0
        loader.add_listener(self._on_tile)

    def get(self, key):
        tex = self.items.get(key)
        if tex is not None:
            self.items.move_to_end(key)
        return tex

    def _on_tile(self, key, data, gen):
        GLib.idle_add(self._store, key, data)

    def _store(self, key, data):
        tex = False
        if data:
            try:
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
            except GLib.Error:
                tex = False
        self.items[key] = tex
        while len(self.items) > MAX_TEXTURES:
            self.items.popitem(last=False)
        for v in self.views:
            v.queue_draw()
        return False

    def invalidate(self, provider_id=None):
        if provider_id is None:
            self.items.clear()
        else:
            for k in [k for k in self.items if k[0] == provider_id]:
                del self.items[k]
        for v in self.views:
            v.queue_draw()


class MapView(Gtk.Widget):
    __gtype_name__ = "MPXMapView"

    def __init__(self, cache, settings, lat=None, lon=None, zoom=None):
        super().__init__(hexpand=True, vexpand=True, focusable=True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.cache = cache
        cache.views.append(self)
        self.settings = settings
        c = settings.get("map_center") or [0, 0]
        self.lat = lat if lat is not None else c[0]
        self.lon = lon if lon is not None else c[1]
        self.zoom = zoom if zoom is not None else float(settings.get("map_zoom", 16))
        self.provider = tiles.get_provider(settings.get("map_provider"))
        self.layers = []
        self.on_click = None          # cb(lat, lon, n_press, x, y)
        self.on_context = None        # cb(lat, lon, x, y)
        self.on_moved = None          # cb()
        self.on_hover = None          # cb(lat, lon)
        self.mouse_latlon = None
        self._drag = None
        self._drag_handle = None
        self._drag_layer = None
        self._moved = False
        self.set_cursor(Gdk.Cursor.new_from_name("default"))

        drag = Gtk.GestureDrag(button=1)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)

        mdrag = Gtk.GestureDrag(button=2)
        mdrag.connect("drag-begin", lambda g, x, y: self._pan_begin(x, y))
        mdrag.connect("drag-update", lambda g, dx, dy: self._pan_update(dx, dy))
        self.add_controller(mdrag)

        click = Gtk.GestureClick(button=0)
        click.connect("released", self._clicked)
        self.add_controller(click)

        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL
                                           | Gtk.EventControllerScrollFlags.DISCRETE)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        self.add_controller(motion)
        self._mouse = (0, 0)

        zoom_g = Gtk.GestureZoom()
        zoom_g.connect("scale-changed", self._pinch)
        zoom_g.connect("begin", lambda *_: setattr(self, "_pinch_zoom", self.zoom))
        self.add_controller(zoom_g)
        self._pinch_zoom = self.zoom

    # ----------------------------------------------------------- projection
    @property
    def izoom(self):
        return max(1, min(self.provider.max_zoom + 2, int(math.floor(self.zoom + 1e-6))))

    def _scale(self):
        return 2 ** (self.zoom - self.izoom)

    def center_world(self):
        return tiles.latlon_to_world(self.lat, self.lon, self.izoom)

    def to_screen(self, lat, lon):
        z = self.izoom
        s = self._scale()
        cx, cy = self.center_world()
        x, y = tiles.latlon_to_world(lat, lon, z)
        w, h = self.get_width(), self.get_height()
        return (x - cx) * s + w / 2, (y - cy) * s + h / 2

    def to_latlon(self, sx, sy):
        z = self.izoom
        s = self._scale()
        cx, cy = self.center_world()
        w, h = self.get_width(), self.get_height()
        return tiles.world_to_latlon(cx + (sx - w / 2) / s, cy + (sy - h / 2) / s, z)

    def meters_per_pixel(self):
        return 156543.03392 * math.cos(math.radians(self.lat)) / (2 ** self.zoom)

    def bounds(self):
        w, h = self.get_width(), self.get_height()
        n, wst = self.to_latlon(0, 0)
        s, e = self.to_latlon(w, h)
        return n, wst, s, e

    # --------------------------------------------------------------- control
    def set_center(self, lat, lon, zoom=None):
        self.lat, self.lon = lat, lon
        if zoom is not None:
            self.zoom = max(2.0, min(self.provider.max_zoom + 2.0, zoom))
        self.queue_draw()
        if self.on_moved:
            self.on_moved()

    def set_provider(self, pid):
        self.provider = tiles.get_provider(pid)
        self.zoom = min(self.zoom, self.provider.max_zoom + 2.0)
        self.queue_draw()

    def zoom_at(self, delta, sx=None, sy=None):
        if sx is None:
            sx, sy = self.get_width() / 2, self.get_height() / 2
        lat0, lon0 = self.to_latlon(sx, sy)
        self.zoom = max(2.0, min(self.provider.max_zoom + 2.0, self.zoom + delta))
        # keep the point under the cursor fixed
        x1, y1 = self.to_screen(lat0, lon0)
        self._shift(x1 - sx, y1 - sy)
        self.queue_draw()
        if self.on_moved:
            self.on_moved()

    def fit(self, points, pad=60):
        if not points:
            return
        lats = [p[0] for p in points]
        lons = [p[1] for p in points]
        lat_c = (max(lats) + min(lats)) / 2
        lon_c = (max(lons) + min(lons)) / 2
        w = max(100, self.get_width() - 2 * pad)
        h = max(100, self.get_height() - 2 * pad)
        for z in range(self.provider.max_zoom, 1, -1):
            x0, y0 = tiles.latlon_to_world(max(lats), min(lons), z)
            x1, y1 = tiles.latlon_to_world(min(lats), max(lons), z)
            if abs(x1 - x0) <= w and abs(y1 - y0) <= h:
                break
        self.set_center(lat_c, lon_c, float(min(z, 18)))

    def _shift(self, dx, dy):
        cx, cy = self.center_world()
        s = self._scale()
        self.lat, self.lon = tiles.world_to_latlon(cx + dx / s, cy + dy / s, self.izoom)

    # ---------------------------------------------------------------- input
    def _hit(self, x, y):
        for layer in reversed(self.layers):
            hit = getattr(layer, "hit", None)
            if hit:
                h = hit(self, x, y)
                if h is not None:
                    return layer, h
        return None, None

    def _drag_begin(self, g, x, y):
        self.grab_focus()
        self._moved = False
        layer, h = self._hit(x, y)
        if h is not None and getattr(layer, "drag", None):
            self._drag_layer, self._drag_handle = layer, h
        else:
            self._drag_layer = None
        self._drag = (x, y, self.lat, self.lon)

    def _drag_update(self, g, dx, dy):
        if self._drag is None:
            return
        if abs(dx) + abs(dy) > 3:
            self._moved = True
        x0, y0, lat0, lon0 = self._drag
        if self._drag_layer is not None:
            lat, lon = self.to_latlon(x0 + dx, y0 + dy)
            self._drag_layer.drag(self, self._drag_handle, lat, lon)
            self.queue_draw()
            return
        self.lat, self.lon = lat0, lon0
        self._shift(-dx, -dy)
        self.queue_draw()

    def _drag_end(self, g, dx, dy):
        if self._drag_layer is not None and self._moved:
            end = getattr(self._drag_layer, "drag_end", None)
            if end:
                end(self, self._drag_handle)
        elif self._moved and self.on_moved:
            self.on_moved()
        self._drag_layer = None
        self._drag = None

    def _pan_begin(self, x, y):
        self._drag = (x, y, self.lat, self.lon)

    def _pan_update(self, dx, dy):
        if self._drag:
            x0, y0, lat0, lon0 = self._drag
            self.lat, self.lon = lat0, lon0
            self._shift(-dx, -dy)
            self.queue_draw()

    def _clicked(self, g, n, x, y):
        btn = g.get_current_button()
        lat, lon = self.to_latlon(x, y)
        if btn == 3:
            if self.on_context:
                self.on_context(lat, lon, x, y)
            return
        if btn == 1 and not self._moved:
            layer, h = self._hit(x, y)
            if h is not None and getattr(layer, "clicked", None):
                layer.clicked(self, h, n)
                return
            if self.on_click:
                self.on_click(lat, lon, n, x, y)
        self._moved = False

    def _scroll(self, c, dx, dy):
        x, y = self._mouse
        self.zoom_at(-1 if dy > 0 else 1, x, y)
        return True

    def _pinch(self, g, scale):
        self.zoom = max(2.0, min(self.provider.max_zoom + 2.0,
                                 self._pinch_zoom + math.log2(max(0.01, scale))))
        self.queue_draw()

    def _motion(self, c, x, y):
        self._mouse = (x, y)
        self.mouse_latlon = self.to_latlon(x, y)
        layer, h = self._hit(x, y)
        name = "pointer" if h is not None else "default"
        self.set_cursor(Gdk.Cursor.new_from_name(name))
        if self.on_hover:
            self.on_hover(*self.mouse_latlon)
        if any(getattr(lay, "track_mouse", False) for lay in self.layers):
            self.queue_draw()

    # ------------------------------------------------------------ rendering
    def _texture(self, prov, z, x, y):
        n = 2 ** z
        if y < 0 or y >= n:
            return None
        x %= n
        key = (prov.id, z, x, y)
        tex = self.cache.get(key)
        if tex is None:
            self.cache.loader.request(prov, z, x, y, self.cache.gen)
            return None
        return tex or None

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        bg = Gdk.RGBA()
        bg.parse("#2b2f36" if self.provider.satellite else "#e8e4dc")
        snap.append_color(bg, Graphene.Rect().init(0, 0, w, h))
        z = self.izoom
        s = self._scale()
        ts = TILE * s
        cx, cy = self.center_world()
        left = cx - (w / 2) / s
        top = cy - (h / 2) / s
        tx0, ty0 = int(math.floor(left / TILE)), int(math.floor(top / TILE))
        tx1 = int(math.floor((left + w / s) / TILE))
        ty1 = int(math.floor((top + h / s) / TILE))
        zt = min(z, self.provider.max_zoom)
        provs = [self.provider]
        if self.provider.overlay:
            provs.append(tiles.get_provider(self.provider.overlay))
        filt = Gsk.ScalingFilter.TRILINEAR if s != 1 else Gsk.ScalingFilter.NEAREST
        for prov in provs:
            for tx in range(tx0, tx1 + 1):
                for ty in range(ty0, ty1 + 1):
                    sx = (tx * TILE - left) * s
                    sy = (ty * TILE - top) * s
                    rect = Graphene.Rect().init(sx, sy, ts + 0.5, ts + 0.5)
                    if zt < z:
                        # beyond the provider's max zoom: magnify the deepest tile
                        self._draw_parent(snap, prov, z, tx, ty, z - zt, rect, sx, sy, ts)
                        continue
                    tex = self._texture(prov, z, tx, ty)
                    if tex:
                        snap.append_scaled_texture(tex, filt, rect)
                    elif prov is self.provider:
                        self._draw_parent(snap, prov, z, tx, ty, 1, rect, sx, sy, ts, request=False)
        def overlay(cr):
            for layer in self.layers:
                cr.save()
                try:
                    layer.draw(cr, self)
                except Exception as e:
                    print(f"[mpxair] map layer {type(layer).__name__}: {e!r}")
                cr.restore()
            self._draw_scale(cr, w, h)
        snapshot_cairo(self, snap, overlay, 0, 0, w, h)

    def _draw_parent(self, snap, prov, z, tx, ty, up, rect, sx, sy, ts, request=True):
        for d in range(up, min(up + 4, z)):
            pz = z - d
            f = 2 ** d
            px, py = tx // f, ty // f
            n = 2 ** pz
            key = (prov.id, pz, px % n, py)
            tex = self.cache.get(key)
            if tex is None and request and d == up:
                tex = self._texture(prov, pz, px, py)
            if tex:
                big = ts * f
                ox = sx - (tx - px * f) * ts
                oy = sy - (ty - py * f) * ts
                snap.push_clip(rect)
                snap.append_scaled_texture(tex, Gsk.ScalingFilter.LINEAR,
                                           Graphene.Rect().init(ox, oy, big, big))
                snap.pop()
                return True
        return False

    def _draw_scale(self, cr, w, h):
        mpp = self.meters_per_pixel()
        target = mpp * 120
        nice = 1
        for v in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000,
                  100000, 200000, 500000, 1000000):
            if v <= target:
                nice = v
        px = nice / mpp
        x0, y0 = 14, h - 30
        cr.set_line_width(4)
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.move_to(x0, y0)
        cr.line_to(x0 + px, y0)
        cr.stroke()
        cr.set_line_width(2)
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.move_to(x0, y0)
        cr.line_to(x0 + px, y0)
        cr.stroke()
        txt = f"{nice / 1000:g} km" if nice >= 1000 else f"{nice} m"
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(11)
        outline_text(cr, x0, y0 - 6, txt)


# ---------------------------------------------------------------- drawing kit
def outline_text(cr, x, y, text, rgb=(1, 1, 1), size=None, center=False):
    if size:
        cr.set_font_size(size)
    if center:
        ext = cr.text_extents(text)
        x -= ext.width / 2 + ext.x_bearing
        y -= ext.height / 2 + ext.y_bearing
    cr.move_to(x, y)
    cr.text_path(text)
    cr.set_source_rgba(0, 0, 0, 0.75)
    cr.set_line_width(3)
    cr.stroke_preserve()
    cr.set_source_rgb(*rgb)
    cr.fill()


def draw_vehicle(cr, x, y, heading, vclass="copter", color=(0.95, 0.25, 0.2), size=18):
    cr.save()
    cr.translate(x, y)
    cr.rotate(math.radians(heading))
    if vclass == "plane":
        pts = [(0, -size), (size * 0.18, -size * 0.3), (size, size * 0.1), (size, size * 0.3),
               (size * 0.18, size * 0.15), (size * 0.12, size * 0.7), (size * 0.45, size),
               (-size * 0.45, size), (-size * 0.12, size * 0.7), (-size * 0.18, size * 0.15),
               (-size, size * 0.3), (-size, size * 0.1), (-size * 0.18, -size * 0.3)]
    else:
        pts = [(0, -size), (size * 0.7, size * 0.75), (0, size * 0.35), (-size * 0.7, size * 0.75)]
    cr.move_to(*pts[0])
    for p in pts[1:]:
        cr.line_to(*p)
    cr.close_path()
    cr.set_source_rgba(0, 0, 0, 0.35)
    cr.set_line_width(5)
    cr.stroke_preserve()
    cr.set_source_rgb(*color)
    cr.fill_preserve()
    cr.set_source_rgb(1, 1, 1)
    cr.set_line_width(1.5)
    cr.stroke()
    cr.restore()


def draw_marker(cr, x, y, text, color=(0.2, 0.5, 0.95), r=11, selected=False):
    cr.arc(x, y, r + (3 if selected else 0), 0, 2 * math.pi)
    cr.set_source_rgba(0, 0, 0, 0.35)
    cr.set_line_width(4)
    cr.stroke_preserve()
    cr.set_source_rgb(*color)
    cr.fill_preserve()
    cr.set_source_rgb(1, 1, 1)
    cr.set_line_width(2 if not selected else 3)
    cr.stroke()
    cr.select_font_face("Sans", 0, 1)
    cr.set_font_size(11 if len(text) < 3 else 9)
    ext = cr.text_extents(text)
    cr.move_to(x - ext.width / 2 - ext.x_bearing, y - ext.height / 2 - ext.y_bearing)
    cr.set_source_rgb(1, 1, 1)
    cr.show_text(text)


def draw_home(cr, x, y):
    cr.save()
    cr.translate(x, y)
    cr.move_to(0, -13)
    cr.line_to(12, -2)
    cr.line_to(8, -2)
    cr.line_to(8, 10)
    cr.line_to(-8, 10)
    cr.line_to(-8, -2)
    cr.line_to(-12, -2)
    cr.close_path()
    cr.set_source_rgba(0, 0, 0, 0.4)
    cr.set_line_width(4)
    cr.stroke_preserve()
    cr.set_source_rgb(0.15, 0.7, 0.35)
    cr.fill_preserve()
    cr.set_source_rgb(1, 1, 1)
    cr.set_line_width(1.5)
    cr.stroke()
    cr.select_font_face("Sans", 0, 1)
    cr.set_font_size(9)
    cr.move_to(-4, 7)
    cr.show_text("H")
    cr.restore()


def circle_radius_px(view, lat, meters):
    mpp = 156543.03392 * math.cos(math.radians(lat)) / (2 ** view.zoom)
    return meters / mpp
