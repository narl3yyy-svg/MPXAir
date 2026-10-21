"""A form of parameter rows built from the parameter documentation, with a Save button."""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from ..core.params import ParamDocs, format_value  # noqa: E402
from .util import background, button, combo_row, label, short_description, toast  # noqa: E402


def param_tooltip(name, d):
    """Hover text for a parameter: what it does, in a sentence or two, plus its name and units."""
    parts = []
    if d.get("Description"):
        parts.append(short_description(d["Description"]))
    meta = name + (f" · units: {d['Units']}" if d.get("Units") else "")
    if d.get("RebootRequired", "").lower() == "true":
        meta += " · needs a reboot"
    parts.append(meta)
    return "\n".join(parts)


class ParamForm(Gtk.Box):
    """spec: list of (title or None, [name or (alt1, alt2, ...)]) groups."""

    def __init__(self, vehicle, spec, on_saved=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.v = vehicle
        self.spec = spec
        self.on_saved = on_saved
        self.getters = {}
        self.originals = {}
        self.build()

    def resolve(self, names):
        if isinstance(names, str):
            names = (names,)
        for n in names:
            if n in self.v.params.values:
                return n
        return None

    def build(self):
        child = self.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.remove(child)
            child = nxt
        self.getters.clear()
        self.originals.clear()
        if not self.v.params.values:
            self.append(label("Connect to a vehicle to change these settings.", ["dim-label"]))
            return
        docs = ParamDocs.for_class(self.v.state.vclass)
        any_rows = False
        for title, names in self.spec:
            grp = Adw.PreferencesGroup(title=title or "")
            n_rows = 0
            for entry in names:
                name = self.resolve(entry)
                if not name:
                    continue
                row = self._row(name, docs.get(name, {}))
                grp.add(row)
                n_rows += 1
            if n_rows:
                self.append(grp)
                any_rows = True
        if not any_rows:
            self.append(label("This vehicle does not have these settings.", ["dim-label"]))
            return
        bar = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        bar.append(button("Revert", self.build))
        bar.append(button("Save", self.save, ["suggested-action"]))
        self.append(bar)

    def _row(self, name, d):
        cur = self.v.params.get(name)
        self.originals[name] = cur
        title = d.get("DisplayName") or name
        tip = param_tooltip(name, d)
        if d.get("values"):
            vals = list(d["values"])
            if not any(abs(k - float(cur)) < 1e-6 for k, _l in vals):
                vals.append((float(cur), f"{format_value(cur)} (custom)"))
            sel = next(i for i, (k, _l) in enumerate(vals) if abs(k - float(cur)) < 1e-6)
            row = combo_row(title, [l for _k, l in vals], sel, tooltip=tip, tag=name)
            self.getters[name] = lambda: vals[row.get_selected()][0]
        else:
            lo, hi = d.get("range", (-1e7, 1e7))
            lo, hi = min(lo, float(cur)), max(hi, float(cur))
            is_int = isinstance(cur, int)
            try:
                step = float(d.get("Increment") or (1 if is_int else 0.1))
            except ValueError:
                step = 1
            row = Adw.SpinRow.new_with_range(lo, hi, step)
            row.set_title(title)
            row.set_subtitle(name + (f" · {d['Units']}" if d.get("Units") else "")
                             + (f" · {d['range'][0]:g} to {d['range'][1]:g}" if d.get("range") else ""))
            row.set_digits(0 if is_int else 2)
            row.set_value(float(cur))
            row.set_tooltip_text(tip)
            self.getters[name] = lambda: row.get_value()
        return row

    def changes(self):
        out = {}
        for name, g in self.getters.items():
            val = g()
            old = self.originals[name]
            if abs(float(val) - float(old)) > 1e-6:
                out[name] = int(round(val)) if isinstance(old, int) else val
        return out

    def save(self):
        ch = self.changes()
        if not ch:
            toast("Nothing changed")
            return
        if not self.v.state.connected:
            toast("Connect to a vehicle first")
            return

        def work():
            errs = []
            for n, val in ch.items():
                ok, msg = self.v.params.set(n, val)
                if not ok:
                    errs.append(msg)
            return errs

        def done(errs):
            if errs:
                toast("; ".join(errs), 6)
            else:
                toast(f"Saved {len(ch)} setting{'s' if len(ch) != 1 else ''}")
            self.build()
            if self.on_saved:
                self.on_saved()
        background(work, done)
