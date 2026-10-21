"""Developer aid: MPXAIR_SCREENSHOT=/dir saves PNGs of every page (used for testing).

MPXAIR_SCREENSHOT_SCRIPT may hold '|'-separated steps run before the shots, e.g.
"connect:demo|wait:8|tab:fly|shot:fly|tab:plan|shot:plan|quit".
"""
import os

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402


def save_widget_png(widget, path):
    w, h = widget.get_width(), widget.get_height()
    if w <= 0 or h <= 0:
        return False
    paintable = Gtk.WidgetPaintable.new(widget)
    snap = Gtk.Snapshot()
    paintable.snapshot(snap, w, h)
    node = snap.to_node()
    if node is None:
        return False
    renderer = widget.get_native().get_renderer()
    tex = renderer.render_texture(node, None)
    tex.save_to_png(path)
    return True


def schedule_screenshots(win, outdir):
    os.makedirs(outdir, exist_ok=True)
    script = os.environ.get("MPXAIR_SCREENSHOT_SCRIPT",
                            "wait:3|" + "|".join(f"tab:{p}|wait:1|shot:{p}" for p in win.pages) + "|quit")
    steps = [s.strip() for s in script.split("|") if s.strip()]

    def run(i=0):
        while i < len(steps):
            cmd, _, arg = steps[i].partition(":")
            i += 1
            if cmd == "wait":
                GLib.timeout_add(int(float(arg) * 1000), lambda n=i: run(n) and False)
                return False
            if cmd == "tab":
                win.stack.set_visible_child_name(arg)
            elif cmd == "shot":
                ok = save_widget_png(win, os.path.join(outdir, f"{arg}.png"))
                print(f"[screenshot] {arg}: {'saved' if ok else 'FAILED'}", flush=True)
            elif cmd == "connect":
                win.connect_bar.debug_connect(arg)
            elif cmd == "call":
                # call:page.method  (debug only)
                page, _, meth = arg.partition(".")
                getattr(win.pages[page], meth)()
            elif cmd == "py":
                # debug only: run a python expression with `win` in scope
                try:
                    exec(arg, {"win": win, "pages": win.pages, "shared": win.shared})
                except Exception as e:
                    print(f"[screenshot] py error: {e!r}", flush=True)
            elif cmd == "sizes":
                def walk(w, depth=0):
                    if depth > int(arg or 6):
                        return
                    print("  " * depth + f"{type(w).__name__} {w.get_width()}x{w.get_height()}")
                    c = w.get_first_child()
                    while c:
                        walk(c, depth + 1)
                        c = c.get_next_sibling()
                walk(win)
            elif cmd == "quit":
                win.close()
                # an open dialog swallows the first close: make sure the app really ends
                GLib.timeout_add(500, lambda: win.get_application().quit() or False)
                return False
        return False

    GLib.idle_add(run)
