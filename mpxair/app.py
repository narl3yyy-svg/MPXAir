"""MPXAir application: shared state and the main window."""
import collections
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .core import config  # noqa: E402
from .core.ground import AntennaTracker, FollowMe, GroundLocation  # noqa: E402
from .core.vehicle import Vehicle  # noqa: E402
from .maps.tiles import TileLoader  # noqa: E402
from .ui.mapview import TextureCache  # noqa: E402
from .ui.util import Toaster, alert, idle, toast  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


class Hub:
    """Tiny app-wide event bus for UI-to-UI notifications (main thread only)."""

    def __init__(self):
        self._subs = collections.defaultdict(list)

    def on(self, name, cb):
        self._subs[name].append(cb)

    def emit(self, name, *args):
        for cb in list(self._subs.get(name, ())):
            try:
                cb(*args)
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"[mpxair] hub {name}: {e!r}")


class Shared:
    """Everything the pages share."""

    def __init__(self):
        self.settings = config.Settings()
        self.vehicle = Vehicle(self.settings)
        self.hub = Hub()
        self.loader = TileLoader(self.settings)
        self.tex_cache = TextureCache(self.loader)
        self.ground = GroundLocation(self.settings, self.vehicle)
        self.follow = FollowMe(self.vehicle, self.ground, self.settings)
        self.tracker = AntennaTracker(self.vehicle, self.ground, self.settings)
        self.mission = []        # list[MissionItem] last read from / written to the vehicle
        self.fence = []
        self.rally = []
        self.demo = None
        self.window = None
        # forward vehicle events onto the main loop as hub events
        v = self.vehicle
        for ev in ("connected", "disconnected", "statustext", "link_lost", "link_restored",
                   "armed", "mode", "home", "firmware"):
            v.on(ev, lambda arg=None, _ev=ev: idle(self.hub.emit, "vehicle-" + _ev, arg))

    def start_demo(self):
        if self.demo is None:
            from .sim.demo import DemoServer
            c = self.settings.get("map_center") or [-35.363261, 149.165230]
            self.demo = DemoServer(5790, c[0], c[1])
            self.demo.start()
        return 5790


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, shared):
        super().__init__(application=app, title="MPXAir")
        self.shared = shared
        shared.window = self
        w, h = shared.settings.get("window") or (1440, 900)
        # never open bigger than the screen (a too-wide window hides the right-hand panels)
        try:
            mons = Gdk.Display.get_default().get_monitors()
            if mons.get_n_items():
                geo = mons.get_item(0).get_geometry()
                w, h = min(w, geo.width), min(h, geo.height - 40)
        except Exception:
            pass
        self.set_default_size(w, h)
        if shared.settings.get("maximized", True):
            self.maximize()
        self.set_size_request(900, 600)

        from .ui.connect import ConnectBar
        from .ui.fly import FlyPage
        from .ui.plan import PlanPage
        from .ui.setup import SetupPage
        from .ui.params_page import ParamsPage
        from .ui.logs_page import LogsPage
        from .ui.ground_page import GroundPage

        self.toasts = Adw.ToastOverlay()
        Toaster.overlay = self.toasts
        self.stack = Adw.ViewStack(vexpand=True)
        self.pages = {}
        for pid, title, icon, cls in (
                ("fly", "Fly", "mark-location-symbolic", FlyPage),
                ("plan", "Plan", "find-location-symbolic", PlanPage),
                ("setup", "Setup", "preferences-system-symbolic", SetupPage),
                ("params", "Parameters", "view-list-symbolic", ParamsPage),
                ("logs", "Logs", "document-open-recent-symbolic", LogsPage),
                ("ground", "Ground", "network-wireless-symbolic", GroundPage)):
            page = cls(shared)
            self.pages[pid] = page
            self.stack.add_titled_with_icon(page, pid, title, icon)

        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        menu = Gio.Menu()
        menu.append("Preferences", "app.preferences")
        menu.append("Offline Maps…", "app.offline-maps")
        menu.append("Open Telemetry Log (.tlog)…", "app.open-tlog")
        menu.append("Manual (online)", "app.manual")
        menu.append("Keyboard Shortcuts", "app.shortcuts")
        menu.append("About MPXAir", "app.about")
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                  tooltip_text="Main menu")
        header.pack_start(menu_btn)
        logo = Gtk.Label(label="MPXAir", css_classes=["heading"])
        header.pack_start(logo)

        self.connect_bar = ConnectBar(shared)
        header.pack_end(self.connect_bar)

        self.progress = Gtk.ProgressBar(css_classes=["osd"], visible=False)
        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.progress)
        box.append(self.stack)
        tv.set_content(box)
        bar = Adw.ViewSwitcherBar(stack=self.stack)
        tv.add_bottom_bar(bar)
        # medium widths: icons above short labels so every tab stays readable
        bp_mid = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1360sp"))
        bp_mid.add_setter(switcher, "policy", Adw.ViewSwitcherPolicy.NARROW)
        self.add_breakpoint(bp_mid)
        self.toasts.set_child(tv)
        self.set_content(self.toasts)

        # narrow windows: switch to bottom tab bar
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1000sp"))
        bp.add_setter(bar, "reveal", True)
        bp.add_setter(switcher, "visible", False)
        self.add_breakpoint(bp)

        last = shared.settings.get("last_tab", "fly")
        if last in self.pages:
            self.stack.set_visible_child_name(last)
        self.stack.connect("notify::visible-child-name", self._tab_changed)
        self.connect("close-request", self._on_close)

        shared.hub.on("vehicle-statustext", self._statustext)
        shared.hub.on("vehicle-link_lost", lambda *_: toast("Telemetry lost — no data from the vehicle", 5))
        shared.hub.on("vehicle-link_restored", lambda *_: toast("Telemetry restored"))
        shared.hub.on("vehicle-armed", lambda a: toast("Armed" if a else "Disarmed"))
        shared.hub.on("vehicle-mode", lambda m: toast(f"Mode: {m}"))

        sc = Gtk.ShortcutController(scope=Gtk.ShortcutScope.GLOBAL)
        for i, pid in enumerate(self.pages, 1):
            sc.add_shortcut(Gtk.Shortcut(
                trigger=Gtk.ShortcutTrigger.parse_string(f"<Alt>{i}"),
                action=Gtk.CallbackAction.new(lambda *_a, p=pid: self.stack.set_visible_child_name(p) or True)))
        self.add_controller(sc)

    def set_progress(self, frac, text=None):
        if frac is None:
            self.progress.set_visible(False)
            return
        self.progress.set_visible(True)
        self.progress.set_fraction(max(0.0, min(1.0, frac)))

    def _tab_changed(self, *_):
        name = self.stack.get_visible_child_name()
        self.shared.settings.set("last_tab", name, save=False)
        page = self.pages.get(name)
        if page and hasattr(page, "on_shown"):
            page.on_shown()

    def _statustext(self, entry):
        _ts, sev, text = entry
        if sev <= 3:
            toast(text, 5)

    def _on_close(self, *_):
        s = self.shared
        w, h = self.get_default_size()
        s.settings.set("window", [w, h], save=False)
        s.settings.set("maximized", self.is_maximized(), save=False)
        s.settings.save()
        s.follow.stop()
        s.tracker.stop()
        s.ground.stop()
        s.vehicle.disconnect()
        if s.demo:
            s.demo.stop()
        return False


class MPXAirApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=config.APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.shared = None
        self.win = None
        self.auto_connect = False
        GLib.set_application_name("MPXAir")

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(HERE, "ui", "style.css"))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        icon_dir = os.path.join(HERE, "data", "icons")
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(icon_dir)
        Gtk.Window.set_default_icon_name("mpxair")
        for name, cb, accel in (("preferences", self._prefs, "<Ctrl>comma"),
                                ("offline-maps", self._offline, None),
                                ("open-tlog", self._open_tlog, None),
                                ("shortcuts", self._shortcuts, "<Ctrl>question"),
                                ("manual", lambda *_: open_docs(self.win), "F1"),
                                ("about", self._about, None),
                                ("quit", lambda *_: self.win.close(), "<Ctrl>q")):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
            if accel:
                self.set_accels_for_action(f"app.{name}", [accel])

    def do_activate(self):
        if self.win is None:
            self.shared = Shared()
            self._apply_theme()
            self.win = MainWindow(self, self.shared)
            self.shared.ground.start()
        self.win.present()
        if self.auto_connect and not self.shared.vehicle.is_open:
            # --connect: connect to the last-used device as soon as the window is up
            GLib.timeout_add(800, lambda: self.win.connect_bar.toggle() and False)
            self.auto_connect = False
        shot = os.environ.get("MPXAIR_SCREENSHOT")
        if shot:
            from .ui.debug import schedule_screenshots
            schedule_screenshots(self.win, shot)

    def _apply_theme(self):
        sm = Adw.StyleManager.get_default()
        t = self.shared.settings.get("theme", "system")
        sm.set_color_scheme({"light": Adw.ColorScheme.FORCE_LIGHT,
                             "dark": Adw.ColorScheme.FORCE_DARK}.get(t, Adw.ColorScheme.DEFAULT))

    def _prefs(self, *_):
        from .ui.prefs import PreferencesDialog
        PreferencesDialog(self.shared, on_theme=self._apply_theme).present(self.win)

    def _offline(self, *_):
        from .ui.offline_maps import OfflineMapsDialog
        OfflineMapsDialog(self.shared).present(self.win)

    def _open_tlog(self, *_):
        self.win.stack.set_visible_child_name("logs")
        self.win.pages["logs"].open_tlog()

    def _shortcuts(self, *_):
        text = ("F1  open the online manual\n"
                "Alt+1 … Alt+6  switch screens\n"
                "Ctrl+,  preferences\n"
                "Ctrl+Q  quit\n\n"
                "Map: scroll to zoom, drag to pan, right-click for actions.\n"
                "Plan: click the map to add a waypoint, drag a waypoint to move it.")
        alert(self.win, "Keyboard Shortcuts", text)

    def _about(self, *_):
        about = Adw.AboutDialog(
            application_name="MPXAir", application_icon="mpxair", version=config.VERSION,
            developer_name="narl3yyy-svg",
            developers=["narl3yyy-svg https://github.com/narl3yyy-svg"],
            issue_url=config.REPO_URL + "/issues",
            comments="A modern ArduPilot ground control station for Linux, "
                     "modelled on Mission Planner.",
            license_type=Gtk.License.GPL_3_0,
            website=config.REPO_URL)
        about.add_credit_section("Inspired by", ["Mission Planner by Michael Oborne and the ArduPilot team https://ardupilot.org/planner/"])
        about.add_legal_section("MAVLink message definitions", "© MAVLink project", Gtk.License.MIT_X11, None)
        about.add_legal_section("Map data",
                                "Esri World Imagery / Street / Topo © Esri and contributors. "
                                "OpenStreetMap © OpenStreetMap contributors (ODbL). "
                                "OpenTopoMap © OpenTopoMap (CC-BY-SA).", Gtk.License.CUSTOM, None)
        about.present(self.win)


def open_docs(parent, page=""):
    """Open the MPXAir manual (GitHub Pages) in the web browser."""
    Gtk.UriLauncher.new(config.DOCS_URL + page).launch(parent, None, None)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv[1:]
    if "-h" in args or "--help" in args:
        print("MPXAir — ArduPilot ground control station\n\n"
              "  mpxair             start MPXAir\n"
              "  mpxair --connect   start and connect to the last-used device\n"
              "  mpxair --version   show the version")
        return 0
    if "--version" in args:
        print(f"MPXAir {config.VERSION}")
        return 0
    app = MPXAirApp()
    app.auto_connect = "--connect" in args
    return app.run(sys.argv[:1])
