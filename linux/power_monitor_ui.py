#!/usr/bin/env python3
"""Desktop window for the power-monitor logs: current draw, per-component breakdown, units used in the last N hours."""
import datetime as dt
import glob
import io
import json
import math
import os

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib, Gtk  # noqa: E402

LOG_DIR = os.environ.get("POWER_LOG_DIR", os.path.expanduser("~/power-logs"))
REFRESH_S = 10
TABLE_ROWS = 300
STALE_S = 60
CHART_H = 200
GAP_S = 35  # more than 3 missed samples breaks the chart line

COMPONENTS = ["cpu", "gpu", "ram", "ssd", "board", "fans", "network", "other", "psu_loss"]
LABELS = {"cpu": "CPU", "gpu": "GPU", "ram": "RAM", "ssd": "SSDs", "board": "Motherboard",
          "fans": "Fans / cooling", "network": "Network", "other": "USB / other", "psu_loss": "PSU loss"}
COLUMNS = [f"{c}_w" for c in COMPONENTS] + ["est_total_w"]
TOTAL = len(COMPONENTS)  # index of the total in a row's values

# Chart series: (label, rgb, function of a row's values)
SERIES = [
    ("Total", (0.26, 0.52, 0.96), lambda v: v[TOTAL]),
    ("CPU", (0.95, 0.55, 0.15), lambda v: v[0]),
    ("GPU", (0.30, 0.75, 0.40), lambda v: v[1]),
    ("Other parts", (0.65, 0.45, 0.85), lambda v: v[TOTAL] - v[0] - v[1]),
]


class LogStore:
    """Reads the daily CSVs incrementally: each refresh only parses bytes appended since the last one.
    Rows are (timestamp, interval_s, values) with values ordered like COLUMNS."""

    def __init__(self):
        self.files = {}  # path -> {"offset", "index", "rows"}

    def load(self, since):
        first_day = since.date().isoformat()
        out = []
        for path in sorted(glob.glob(os.path.join(LOG_DIR, "????-??-??.csv"))):
            if os.path.basename(path)[:-4] < first_day:
                continue
            ent = self.files.setdefault(path, {"offset": 0, "index": None, "rows": []})
            try:
                if os.path.getsize(path) < ent["offset"]:  # file was replaced/truncated
                    ent.update(offset=0, index=None, rows=[])
                with open(path, "rb") as f:
                    f.seek(ent["offset"])
                    chunk = f.read()
            except OSError:
                continue
            end = chunk.rfind(b"\n") + 1  # ignore a half-written last line
            for line in chunk[:end].decode(errors="replace").splitlines():
                p = line.split(",")
                if ent["index"] is None:  # header: map our columns to positions in this file
                    ent["index"] = [p.index(c) if c in p else None for c in COLUMNS]
                    ent["ts"], ent["iv"] = p.index("timestamp"), p.index("interval_s")
                    continue
                try:
                    vals = tuple(float(p[i]) if i is not None else 0.0 for i in ent["index"])
                    ent["rows"].append((dt.datetime.fromisoformat(p[ent["ts"]]), float(p[ent["iv"]]), vals))
                except (ValueError, IndexError):
                    pass
            ent["offset"] += end
            out.extend(r for r in ent["rows"] if r[0] >= since)
        return out


def load_hardware():
    try:
        with open(os.path.join(LOG_DIR, "hardware.json")) as f:
            return {c["key"]: c for c in json.load(f)["components"]}
    except (OSError, ValueError, KeyError):
        return {}


def load_fans():
    try:
        with open(os.path.join(LOG_DIR, "fans.json")) as f:
            return json.load(f)["fans"]
    except (OSError, ValueError, KeyError):
        return []


def fmt_age(seconds):
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def small(text):
    return f"<small>{GLib.markup_escape_text(text)}</small>"


class PowerWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Power Monitor")
        self.set_default_size(680, 720)
        self.store = LogStore()
        self.rows = []
        self.since = self.until = dt.datetime.now()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(14)
        self.add(box)

        # Current consumption
        self.now_lbl = Gtk.Label(xalign=0)
        self.parts_lbl = Gtk.Label(xalign=0)
        self.status_lbl = Gtk.Label(xalign=0)
        for w in (self.now_lbl, self.parts_lbl, self.status_lbl):
            box.pack_start(w, False, False, 0)
        box.pack_start(Gtk.Separator(), False, False, 4)

        # Last N hours controls
        ctl = Gtk.Box(spacing=6)
        ctl.pack_start(Gtk.Label(label="Last"), False, False, 0)
        self.hours = Gtk.SpinButton.new_with_range(1, 720, 1)
        self.hours.set_value(24)
        self.hours.connect("value-changed", lambda *_: self.refresh())
        ctl.pack_start(self.hours, False, False, 0)
        ctl.pack_start(Gtk.Label(label="hours"), False, False, 0)
        self.price = Gtk.SpinButton.new_with_range(0, 1000, 0.5)
        self.price.set_digits(2)
        self.price.connect("value-changed", lambda *_: self.refresh())
        ctl.pack_end(self.price, False, False, 0)
        ctl.pack_end(Gtk.Label(label="Price / unit"), False, False, 0)
        box.pack_start(ctl, False, False, 0)

        self.units_lbl = Gtk.Label(xalign=0)
        self.stats_lbl = Gtk.Label(xalign=0)
        box.pack_start(self.units_lbl, False, False, 0)
        box.pack_start(self.stats_lbl, False, False, 0)

        self.tabs = tabs = Gtk.Notebook()
        box.pack_start(tabs, True, True, 4)

        # Breakdown tab: one row per component
        self.grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        self.grid.set_border_width(10)
        heads = ["Component", "Hardware", "Now W", "Avg W", "kWh", "Share"]
        for col, text in enumerate(heads):
            lbl = Gtk.Label(xalign=0 if col < 2 else 1)
            lbl.set_markup(f"<b>{text}</b>")
            self.grid.attach(lbl, col, 0, 1, 1)
        self.cells = {}
        for row, c in enumerate(COMPONENTS + ["total"], start=1):
            name = Gtk.Label(xalign=0, yalign=0)
            hw = Gtk.Label(xalign=0, yalign=0)
            hw.set_line_wrap(True)
            hw.set_max_width_chars(34)
            hw.set_hexpand(True)
            nums = [Gtk.Label(xalign=1, yalign=0) for _ in range(4)]
            for col, w in enumerate([name, hw] + nums):
                self.grid.attach(w, col, row, 1, 1)
            self.cells[c] = (name, hw, nums)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.grid)
        tabs.append_page(scroll, Gtk.Label(label="Breakdown"))

        # Chart tab: drawn off-screen with pycairo (python3-gi-cairo isn't needed), shown as an image.
        # EXTERNAL scrolling lets the window shrink below the current image width.
        self.chart = Gtk.Image()
        self.chart_area = Gtk.ScrolledWindow()
        self.chart_area.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        self.chart_area.set_size_request(-1, CHART_H)
        self.chart_area.add(self.chart)
        self.chart_width = 0
        self.chart_area.connect("size-allocate", self.on_chart_resize)
        chart_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        chart_page.set_border_width(8)
        chart_page.pack_start(self.chart_area, False, False, 0)
        tabs.append_page(chart_page, Gtk.Label(label="Chart"))

        # Log tab: every column, newest first
        self.model = Gtk.ListStore(*[str] * (len(COLUMNS) + 1))
        tree = Gtk.TreeView(model=self.model)
        for i, title in enumerate(["Time"] + [LABELS[c] for c in COMPONENTS] + ["Total"]):
            cell = Gtk.CellRendererText()
            if i:
                cell.set_property("xalign", 1.0)
            tree.append_column(Gtk.TreeViewColumn(title, cell, text=i))
        scroll = Gtk.ScrolledWindow()
        scroll.add(tree)
        tabs.append_page(scroll, Gtk.Label(label="Log (W)"))

        note = Gtk.Label(xalign=0)
        note.set_markup(small("Hover a component for how its power is measured or estimated. "
                              "Logs: " + LOG_DIR))
        note.set_line_wrap(True)
        box.pack_start(note, False, False, 0)

        self.connect("destroy", Gtk.main_quit)
        self.refresh()
        GLib.timeout_add_seconds(REFRESH_S, self.refresh)

    def refresh(self):
        hours = self.hours.get_value()
        self.until = dt.datetime.now()
        self.since = self.until - dt.timedelta(hours=hours)
        self.rows = rows = self.store.load(self.since)
        price = self.price.get_value()
        hw = load_hardware()

        if not rows:
            self.now_lbl.set_markup("<span size='xx-large' weight='bold'>— W</span>")
            self.parts_lbl.set_text("")
            self.status_lbl.set_markup(f"<span foreground='#d33'>No data in the last {hours:.0f} h. "
                                       "Is power-monitor.service running?</span>")
            self.units_lbl.set_markup("<span size='large' weight='bold'>0 units (kWh)</span>")
            self.stats_lbl.set_text("")
            self.model.clear()
            self.fill_breakdown(hw, None, None, 0)
            self.render_chart()
            return True

        last = rows[-1]
        now_v = last[2]
        age = (self.until - last[0]).total_seconds()
        self.now_lbl.set_markup(f"<span size='xx-large' weight='bold'>{now_v[TOTAL]:.0f} W</span>"
                                "  now (estimated total at the wall)")
        self.parts_lbl.set_text(f"CPU {now_v[0]:.1f} W   ·   GPU {now_v[1]:.1f} W   ·   "
                                f"rest of system {now_v[TOTAL] - now_v[0] - now_v[1]:.1f} W")
        if age > STALE_S:
            self.status_lbl.set_markup(f"<span foreground='#d33'>Logger looks stopped: last sample "
                                       f"{fmt_age(age)} ago</span>")
        else:
            self.status_lbl.set_markup(f"<span foreground='#2a2'>● Logging</span>  last sample {fmt_age(age)} ago")

        secs = sum(r[1] for r in rows)
        joules = [sum(r[2][i] * r[1] for r in rows) for i in range(len(COLUMNS))]
        kwh = joules[TOTAL] / 3.6e6
        avg_w = joules[TOTAL] / secs
        peak = max(r[2][TOTAL] for r in rows)
        cost = f"   ·   cost {kwh * price:.2f}" if price else ""
        self.units_lbl.set_markup(f"<span size='large' weight='bold'>{kwh:.3f} units (kWh)</span>"
                                  f" in the last {hours:.0f} h{cost}")
        month_cost = f" (cost {avg_w * 0.72 * price:.0f})" if price else ""
        self.stats_lbl.set_text(f"avg {avg_w:.0f} W   ·   peak {peak:.0f} W   ·   logged {secs / 3600:.1f} h\n"
                                f"At this average: {avg_w * 0.024:.2f} kWh/day, "
                                f"{avg_w * 0.72:.0f} kWh/30 days{month_cost}")

        self.fill_breakdown(hw, now_v, joules, secs)
        self.model.clear()
        for ts, _, v in reversed(rows[-TABLE_ROWS:]):
            self.model.append([ts.strftime("%d %b %H:%M:%S")] + [f"{x:.1f}" for x in v])
        self.render_chart()
        return True

    def fill_breakdown(self, hw, now_v, joules, secs):
        total_j = joules[TOTAL] if joules else 0
        for i, c in enumerate(COMPONENTS + ["total"]):
            name, hw_lbl, nums = self.cells[c]
            info = hw.get(c, {})
            source = info.get("source", "")
            measured = source.startswith("Measured")
            if c == "total":
                name.set_markup("<b>Total</b>")
                hw_lbl.set_markup(small("Estimated power drawn from the wall socket"))
            else:
                tag = "" if measured else " <small>(est.)</small>"
                name.set_markup(GLib.markup_escape_text(LABELS[c]) + tag)
                text = info.get("hardware", "")
                if c == "fans":
                    fans = load_fans()
                    if fans:
                        lines = [f"{f['label']}: {f['rpm']} RPM, {f['w']:.1f} W" for f in fans if f["rpm"]]
                        idle = [f["label"] for f in fans if not f["rpm"]]
                        if idle:
                            lines.append("Not spinning / not connected: " + ", ".join(idle))
                        text = "\n".join(lines)
                hw_lbl.set_markup(small(text))
                for w in (name, hw_lbl):
                    w.set_tooltip_text(source or None)
            if now_v is None:
                vals = ["—"] * 4
            else:
                vals = [f"{now_v[i]:.1f}", f"{joules[i] / secs:.1f}", f"{joules[i] / 3.6e6:.3f}",
                        f"{joules[i] / total_j:.0%}" if total_j else "—"]
            for w, text in zip(nums, vals):
                w.set_markup(f"<b>{text}</b>" if c == "total" else text)

    def on_chart_resize(self, widget, alloc):
        if alloc.width != self.chart_width:
            self.chart_width = alloc.width
            GLib.idle_add(self.render_chart)

    def render_chart(self):
        w, h = self.chart_width, CHART_H
        if w < 60:
            return False
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        self.draw_chart(cairo.Context(surface), w, h,
                        self.get_style_context().get_color(Gtk.StateFlags.NORMAL))
        buf = io.BytesIO()
        surface.write_to_png(buf)
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(buf.getvalue())
        loader.close()
        self.chart.set_from_pixbuf(loader.get_pixbuf())
        return False

    def draw_chart(self, cr, w, h, fg):
        L, R, T, B = 44, 8, 20, 18
        pw, ph = w - L - R, h - T - B
        cr.set_font_size(10)

        if not self.rows:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6)
            cr.move_to(L, T + ph / 2)
            cr.show_text("No data to chart")
            return

        # Average samples into one bucket per pixel column
        t0, span = self.since.timestamp(), (self.until - self.since).total_seconds()
        n = int(pw)
        sums = [[0.0] * len(SERIES) for _ in range(n)]
        counts = [0] * n
        for ts, _, v in self.rows:
            x = min(n - 1, max(0, int((ts.timestamp() - t0) / span * n)))
            counts[x] += 1
            for k, (_, _, fn) in enumerate(SERIES):
                sums[x][k] += fn(v)
        top = max((sums[x][0] / counts[x] for x in range(n) if counts[x]), default=1)
        step = 10 ** math.floor(math.log10(max(top, 1)))
        ymax = math.ceil(top * 1.1 / step) * step

        def y_of(val):
            return T + ph - val / ymax * ph

        # Grid and axis labels
        cr.set_line_width(1)
        for i in range(5):
            val = ymax * i / 4
            y = round(y_of(val)) + 0.5
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
            cr.move_to(L, y)
            cr.line_to(L + pw, y)
            cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.65)
            label = f"{val:.0f} W"
            cr.move_to(L - 6 - cr.text_extents(label).width, y + 3)
            cr.show_text(label)
        cr.move_to(L, h - 4)
        cr.show_text(self.since.strftime("%d %b %H:%M"))
        cr.move_to(L + pw - cr.text_extents("now").width, h - 4)
        cr.show_text("now")

        # Lines, broken only where samples are missing for longer than GAP_S (logger off / PC off)
        max_gap_px = max(1.0, GAP_S / span * n)
        cr.set_line_width(1.6)
        for k, (_, rgb, _) in enumerate(SERIES):
            cr.set_source_rgb(*rgb)
            last_x = None
            for x in range(n):
                if not counts[x]:
                    continue
                px, py = L + x + 0.5, y_of(sums[x][k] / counts[x])
                if last_x is not None and x - last_x <= max_gap_px:
                    cr.line_to(px, py)
                else:
                    cr.move_to(px, py)
                    cr.line_to(px + 0.5, py)  # an isolated sample still shows as a dot
                last_x = x
            cr.stroke()

        # Legend
        x = L
        for label, rgb, _ in SERIES:
            cr.set_source_rgb(*rgb)
            cr.rectangle(x, 5, 10, 8)
            cr.fill()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.85)
            cr.move_to(x + 14, 13)
            cr.show_text(label)
            x += 24 + cr.text_extents(label).width


if __name__ == "__main__":
    win = PowerWindow()
    win.show_all()
    win.tabs.grab_focus()  # don't start with the cursor in the hours box
    Gtk.main()
