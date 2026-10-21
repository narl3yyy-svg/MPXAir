"""Offline maps: download map areas for use without internet, and refresh them later."""
import math
import shutil
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..maps import tiles  # noqa: E402
from .util import alert, button, confirm, toast  # noqa: E402

MAX_TILES = 150_000
AVG_TILE_KB = 22


class OfflineMapsDialog(Adw.Dialog):
    def __init__(self, shared):
        super().__init__(title="Offline maps", content_width=640, content_height=720)
        self.shared = shared
        self.s = shared.settings
        self.job = None
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        tv.set_content(page)
        self.set_child(tv)

        intro = Adw.PreferencesGroup(description=(
            "Download the map around where you fly so it works without internet. "
            "The area is what the map currently shows on the Fly or Plan screen. "
            "Saved areas can be refreshed later when you are back online."))
        page.add(intro)

        grp = Adw.PreferencesGroup(title="New download")
        self.provs = [p for p in tiles.PROVIDERS if p.bulk_ok]
        self.prov_row = Adw.ComboRow(title="Map type", model=Gtk.StringList.new([p.name for p in self.provs]))
        cur = self.s.get("map_provider")
        ids = [p.id for p in self.provs]
        if cur in ids:
            self.prov_row.set_selected(ids.index(cur))
        grp.add(self.prov_row)
        self.area_row = Adw.ComboRow(title="Area", model=Gtk.StringList.new(
            ["What the map shows now", "1 km around the map centre", "3 km around the map centre",
             "10 km around the map centre"]))
        grp.add(self.area_row)
        self.zmin = Adw.SpinRow.new_with_range(1, 19, 1)
        self.zmin.set_title("Least detail (zoom)")
        self.zmax = Adw.SpinRow.new_with_range(1, 19, 1)
        self.zmax.set_title("Most detail (zoom)")
        z = int(round(self._view().zoom))
        self.zmin.set_value(max(1, z - 4))
        self.zmax.set_value(min(19, z + 2))
        grp.add(self.zmin)
        grp.add(self.zmax)
        self.estimate = Adw.ActionRow(title="Estimate")
        grp.add(self.estimate)
        self.name_row = Adw.EntryRow(title="Name for this area")
        self.name_row.set_text(time.strftime("Area %Y-%m-%d"))
        grp.add(self.name_row)
        for w in (self.prov_row, self.area_row):
            w.connect("notify::selected", lambda *_: self._update_estimate())
        for w in (self.zmin, self.zmax):
            w.connect("notify::value", lambda *_: self._update_estimate())
        self.dl_btn = button("Download", self.start, ["suggested-action"])
        grp.set_header_suffix(self.dl_btn)
        page.add(grp)
        note = Adw.PreferencesGroup(description=(
            "OpenStreetMap and OpenTopoMap don't allow bulk downloading, so they are not offered here. "
            "Tiles you view in them are still cached for offline use."))
        page.add(note)

        prog = Adw.PreferencesGroup()
        self.progress = Gtk.ProgressBar(show_text=True, visible=False, margin_top=4)
        prog.add(self.progress)
        page.add(prog)

        self.saved = Adw.PreferencesGroup(title="Saved areas")
        self.refresh_all_btn = button("Refresh all", self.refresh_all, tooltip="Download newer tiles for every saved area")
        self.saved.set_header_suffix(self.refresh_all_btn)
        page.add(self.saved)

        cache = Adw.PreferencesGroup(title="Storage")
        self.cache_row = Adw.ActionRow(title="Map cache on disk", subtitle="…")
        clear = button("Clear…", self.clear_cache, ["destructive-action"], valign=Gtk.Align.CENTER)
        self.cache_row.add_suffix(clear)
        cache.add(self.cache_row)
        page.add(cache)

        self._fill_saved()
        self._update_estimate()
        GLib.idle_add(self._update_cache_size)
        self.connect("closed", lambda *_: self.job and self.job.cancel.set())

    # ----------------------------------------------------------------- helpers
    def _view(self):
        win = self.shared.window
        name = win.stack.get_visible_child_name() if win else "fly"
        page = win.pages.get(name) if win else None
        from .mapview import MapView
        mv = getattr(page, "map", None)
        return mv if isinstance(mv, MapView) else win.pages["fly"].map

    def _bbox(self):
        mv = self._view()
        i = self.area_row.get_selected()
        if i == 0 and mv.get_width() > 0:
            return mv.bounds()
        km = {1: 1, 2: 3, 3: 10}.get(i, 1)
        dlat = km / 111.32
        dlon = km / (111.32 * max(0.1, math.cos(math.radians(mv.lat))))
        return (mv.lat + dlat, mv.lon - dlon, mv.lat - dlat, mv.lon + dlon)

    def _update_estimate(self):
        zmin, zmax = int(self.zmin.get_value()), int(self.zmax.get_value())
        if zmin > zmax:
            self.estimate.set_subtitle("“Least detail” must not be more than “Most detail”.")
            self.dl_btn.set_sensitive(False)
            return
        prov = self.provs[self.prov_row.get_selected()]
        n = tiles.count_tiles(*self._bbox(), zmin, zmax)
        if prov.overlay:
            n *= 2
        mb = n * AVG_TILE_KB / 1024
        ok = n <= MAX_TILES
        self.estimate.set_subtitle(f"{n:,} tiles · about {mb:,.0f} MB"
                                   + ("" if ok else f" — too many (limit {MAX_TILES:,}). Lower the detail or the area."))
        self.dl_btn.set_sensitive(ok and self.job is None)

    def _update_cache_size(self):
        size = tiles.cache_size_bytes()
        self.cache_row.set_subtitle(f"{size / 1e6:,.0f} MB in {tiles.cache_root()}")
        return False

    def _fill_saved(self):
        # remove old rows
        for r in getattr(self, "_saved_rows", []):
            self.saved.remove(r)
        self._saved_rows = []
        areas = self.s.get("offline_areas") or []
        for i, a in enumerate(areas):
            when = time.strftime("%Y-%m-%d", time.localtime(a.get("updated", 0)))
            prov = tiles.get_provider(a["provider"])
            row = Adw.ActionRow(title=a.get("name", f"Area {i + 1}"),
                                subtitle=f"{prov.name} · zoom {a['zmin']}–{a['zmax']} · updated {when}")
            row.add_suffix(button("", lambda a=a: self._show_area(a), ["flat"], icon="find-location-symbolic",
                                  tooltip="Show on the map", valign=Gtk.Align.CENTER))
            row.add_suffix(button("", lambda a=a: self._run(a, refresh=True), ["flat"], icon="view-refresh-symbolic",
                                  tooltip="Download newer tiles now", valign=Gtk.Align.CENTER))
            row.add_suffix(button("", lambda i=i: self._forget(i), ["flat"], icon="user-trash-symbolic",
                                  tooltip="Remove from the list (tiles stay cached)", valign=Gtk.Align.CENTER))
            self.saved.add(row)
            self._saved_rows.append(row)
        if not areas:
            row = Adw.ActionRow(title="No saved areas yet")
            self.saved.add(row)
            self._saved_rows.append(row)
        self.refresh_all_btn.set_sensitive(bool(areas))

    def _show_area(self, a):
        n, w, s, e = a["bbox"]
        self._view().fit([(n, w), (s, e)])

    def _forget(self, i):
        areas = list(self.s.get("offline_areas") or [])
        if 0 <= i < len(areas):
            del areas[i]
            self.s.set("offline_areas", areas)
            self._fill_saved()

    # ---------------------------------------------------------------- actions
    def start(self):
        if self.s.get("map_mode") == "offline":
            alert(self, "Offline mode is on", "Switch the map to online in Preferences to download maps.")
            return
        prov = self.provs[self.prov_row.get_selected()]
        area = {"name": self.name_row.get_text().strip() or "Area", "provider": prov.id,
                "bbox": list(self._bbox()), "zmin": int(self.zmin.get_value()),
                "zmax": int(self.zmax.get_value()), "updated": time.time()}
        self._run(area, refresh=False, save=True)

    def refresh_all(self):
        areas = list(self.s.get("offline_areas") or [])
        if not areas:
            return
        confirm(self, "Refresh every saved area?",
                "All tiles in your saved areas will be downloaded again, so you get the newest maps. "
                "This uses your internet connection.", "Refresh",
                lambda: self._run_many(areas))

    def _run_many(self, areas):
        if not areas:
            return
        self._run(areas[0], refresh=True, then=lambda: self._run_many(areas[1:]))

    def _run(self, area, refresh, save=False, then=None):
        if self.job is not None:
            toast("A download is already running")
            return
        prov = tiles.get_provider(area["provider"])
        self.progress.set_visible(True)
        self.progress.set_fraction(0)
        self.dl_btn.set_sensitive(False)

        def prog(c, t, failed):
            GLib.idle_add(lambda: (self.progress.set_fraction(c / max(1, t)),
                                   self.progress.set_text(f"{area['name']}: {c:,} of {t:,} tiles"
                                                          + (f" · {failed} failed" if failed else "")), False)[-1])

        def done(err, job):
            def ui():
                self.job = None
                self.dl_btn.set_sensitive(True)
                if err:
                    self.progress.set_text(err)
                else:
                    self.progress.set_text(f"{area['name']}: done ({job.downloaded:,} tiles downloaded)")
                    area["updated"] = time.time()
                    areas = list(self.s.get("offline_areas") or [])
                    if save:
                        areas.append(area)
                    else:
                        for a in areas:
                            if a.get("bbox") == area.get("bbox") and a.get("provider") == area.get("provider"):
                                a["updated"] = area["updated"]
                    self.s.set("offline_areas", areas)
                    self._fill_saved()
                    self.shared.tex_cache.invalidate(prov.id)
                    self._update_cache_size()
                    if then:
                        then()
                return False
            GLib.idle_add(ui)

        self.job = tiles.AreaDownloader(prov, tuple(area["bbox"]), area["zmin"], area["zmax"], refresh, prog, done)
        self.job.start()

    def clear_cache(self):
        def go():
            if self.job:
                self.job.cancel.set()
            shutil.rmtree(tiles.cache_root(), ignore_errors=True)
            self.shared.tex_cache.invalidate()
            self._update_cache_size()
            toast("Map cache cleared")
        confirm(self, "Delete all cached maps?", "Offline areas will need to be downloaded again.",
                "Delete", go, destructive=True)
