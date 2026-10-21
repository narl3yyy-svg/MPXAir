"""Preferences dialog."""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from ..core import config  # noqa: E402
from ..core.params import ParamDocs  # noqa: E402
from ..maps import tiles  # noqa: E402
from .util import RadioRows, background, button, combo_row, toast  # noqa: E402

MAP_MODES = [("online", "Online",
              "Download maps as you look at them and refresh saved tiles when they get old."),
             ("cache", "Saver",
              "Only download tiles you don't have yet. Saved tiles are never refreshed."),
             ("offline", "Offline",
              "Never use the internet. Only maps already saved on this computer are shown.")]


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
        ids = [t[0] for t in themes]
        theme = combo_row("Style", [t[1] for t in themes],
                          ids.index(self.s.get("theme", "system")) if self.s.get("theme") in ids else 0,
                          lambda i: self._set("theme", ids[i], self.on_theme),
                          tooltip="Light or dark colours, or follow Ubuntu's setting")
        g.add(theme)
        gen.add(g)
        g = Adw.PreferencesGroup(title="Flying")
        self._spin(g, "Default waypoint altitude (m)", "default_alt", 1, 5000).set_tooltip_text(
            "Height above home given to new waypoints you add on the Plan screen")
        self._spin(g, "Default takeoff altitude (m)", "takeoff_alt", 1, 500, default=10).set_tooltip_text(
            "Height the Takeoff button climbs to, unless you type another")
        from .layers import TRAIL_CHOICES
        secs = [c[0] for c in TRAIL_CHOICES]
        cur = int(self.s.get("trail_seconds", 20))
        g.add(combo_row("Flight trail on the map", [c[1] for c in TRAIL_CHOICES],
                        secs.index(cur) if cur in secs else 1,
                        lambda i: self._set_trail(secs[i]),
                        tooltip="Older parts of the trail are cleared automatically; "
                                "only this much stays on the map"))
        rec = Adw.SwitchRow(title="Record telemetry logs (.tlog)",
                            subtitle=f"Saved in {config.tlog_dir()}", active=self.s.get("record_tlog", True))
        rec.connect("notify::active", lambda r, *_: self._set("record_tlog", r.get_active()))
        g.add(rec)
        gen.add(g)
        self.add(gen)

        # --------------------------------------------------------------- maps
        mp = self.maps_page = Adw.PreferencesPage(title="Maps", icon_name="mark-location-symbolic")
        g = Adw.PreferencesGroup(title="Map source")
        pids = [p.id for p in tiles.PROVIDERS]
        prov = combo_row("Map type", [p.name for p in tiles.PROVIDERS],
                         pids.index(self.s.get("map_provider")) if self.s.get("map_provider") in pids else 0,
                         lambda i: self._set_provider(pids[i]),
                         tooltip="The map shown on the Fly, Plan and Logs screens")
        g.add(prov)
        mp.add(g)
        g = Adw.PreferencesGroup(title="Internet use", description="How MPXAir downloads map tiles.")
        RadioRows(g, MAP_MODES, self.s.get("map_mode", "online"), lambda mid: self._set("map_mode", mid))
        age = self._spin(g, "Refresh tiles older than (days)", "map_max_age_days", 1, 3650)
        age.set_subtitle("Online mode only: older saved tiles are downloaded again")
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

    def _set_trail(self, seconds):
        fly = self.shared.window.pages.get("fly") if self.shared.window else None
        if fly:
            fly.set_trail_seconds(seconds)
        else:
            self.s.set("trail_seconds", seconds)

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
