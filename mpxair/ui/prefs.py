"""Preferences dialog."""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from ..core import config  # noqa: E402
from ..core.params import ParamDocs  # noqa: E402
from ..maps import tiles  # noqa: E402
from .util import background, button, toast  # noqa: E402

MAP_MODES = [("online", "Online — download and refresh old tiles"),
             ("cache", "Saver — only download tiles I don't have"),
             ("offline", "Offline — never use the internet")]


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, shared, on_theme=None):
        super().__init__(title="Preferences")
        self.shared = shared
        self.s = shared.settings
        self.on_theme = on_theme

        # ------------------------------------------------------------ general
        gen = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        g = Adw.PreferencesGroup(title="Appearance")
        themes = [("system", "Follow the system"), ("light", "Light"), ("dark", "Dark")]
        theme = Adw.ComboRow(title="Style", model=Gtk.StringList.new([t[1] for t in themes]))
        ids = [t[0] for t in themes]
        theme.set_selected(ids.index(self.s.get("theme", "system")) if self.s.get("theme") in ids else 0)
        theme.connect("notify::selected", lambda r, *_: self._set("theme", ids[r.get_selected()], self.on_theme))
        g.add(theme)
        gen.add(g)
        g = Adw.PreferencesGroup(title="Flying")
        self._spin(g, "Default waypoint altitude (m)", "default_alt", 1, 5000)
        self._spin(g, "Default takeoff altitude (m)", "takeoff_alt", 1, 500, default=10)
        rec = Adw.SwitchRow(title="Record telemetry logs (.tlog)",
                            subtitle=f"Saved in {config.tlog_dir()}", active=self.s.get("record_tlog", True))
        rec.connect("notify::active", lambda r, *_: self._set("record_tlog", r.get_active()))
        g.add(rec)
        gen.add(g)
        self.add(gen)

        # --------------------------------------------------------------- maps
        mp = Adw.PreferencesPage(title="Maps", icon_name="mark-location-symbolic")
        g = Adw.PreferencesGroup(title="Map source")
        pids = [p.id for p in tiles.PROVIDERS]
        prov = Adw.ComboRow(title="Map type", model=Gtk.StringList.new([p.name for p in tiles.PROVIDERS]))
        prov.set_selected(pids.index(self.s.get("map_provider")) if self.s.get("map_provider") in pids else 0)
        prov.connect("notify::selected", lambda r, *_: self._set_provider(pids[r.get_selected()]))
        g.add(prov)
        mids = [m[0] for m in MAP_MODES]
        mode = Adw.ComboRow(title="Internet use", model=Gtk.StringList.new([m[1] for m in MAP_MODES]))
        mode.set_selected(mids.index(self.s.get("map_mode", "online")) if self.s.get("map_mode") in mids else 0)
        mode.connect("notify::selected", lambda r, *_: self._set("map_mode", mids[r.get_selected()]))
        g.add(mode)
        self._spin(g, "Refresh tiles older than (days)", "map_max_age_days", 1, 3650)
        off = Adw.ActionRow(title="Offline maps", subtitle="Download areas for flying without internet",
                            activatable=True)
        off.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        off.connect("activated", lambda *_: (self.close(), shared.window.get_application().activate_action("offline-maps", None)))
        g.add(off)
        mp.add(g)
        self.add(mp)

        # ---------------------------------------------------------- telemetry
        tp = Adw.PreferencesPage(title="Telemetry", icon_name="network-wireless-symbolic")
        g = Adw.PreferencesGroup(title="Telemetry rates (per second)",
                                 description="Lower these on slow radio links. They take effect on the next connection.")
        rates = dict(self.s.get("stream_rates") or {})
        for key, title in (("attitude", "Attitude and HUD"), ("position", "Position"), ("status", "Status and battery"),
                           ("sensors", "Sensors"), ("rc", "Radio channels")):
            r = Adw.SpinRow.new_with_range(0, 50, 1)
            r.set_title(title)
            r.set_value(float(rates.get(key, 2)))
            r.connect("notify::value", lambda row, *_a, k=key: self._set_rate(k, row.get_value()))
            g.add(r)
        tp.add(g)
        g = Adw.PreferencesGroup(title="Parameter descriptions",
                                 description="MPXAir ships descriptions for every parameter. Newer firmware adds "
                                             "parameters; download the latest descriptions from ArduPilot to see them.")
        self.docs_row = Adw.ActionRow(title="Descriptions", subtitle=ParamDocs.source_text())
        self.docs_row.add_suffix(button("Update", self._update_docs, valign=Gtk.Align.CENTER))
        g.add(self.docs_row)
        tp.add(g)
        self.add(tp)

    def _set(self, key, value, then=None):
        self.s.set(key, value)
        if then:
            then()

    def _set_rate(self, key, value):
        rates = dict(self.s.get("stream_rates") or {})
        rates[key] = int(value)
        self.s.set("stream_rates", rates)

    def _set_provider(self, pid):
        self.s.set("map_provider", pid)
        self.shared.hub.emit("map-provider", pid)

    def _spin(self, grp, title, key, lo, hi, default=None):
        r = Adw.SpinRow.new_with_range(lo, hi, 1)
        r.set_title(title)
        r.set_value(float(self.s.get(key, default if default is not None else lo)))
        r.connect("notify::value", lambda row, *_: self._set(key, row.get_value()))
        grp.add(r)
        return r

    def _update_docs(self):
        self.docs_row.set_subtitle("Downloading…")

        def done(n):
            self.docs_row.set_subtitle(ParamDocs.source_text())
            toast(f"Downloaded descriptions for {n} vehicle types")
            self.shared.hub.emit("params-loaded", True)
        background(ParamDocs.update_online, done,
                   lambda e: self.docs_row.set_subtitle(f"Download failed: {e}"))
