#!/usr/bin/env python3
"""Desktop window for the power-monitor logs: current draw, per-component breakdown, Monitor Now sessions,
per-process power, threshold alerts and an analytics dashboard."""
import csv
import datetime as dt
import glob
import io
import json
import math
import os
import subprocess

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib, Gtk  # noqa: E402

LOG_DIR = os.environ.get("POWER_LOG_DIR", os.path.expanduser("~/power-logs"))
REFRESH_S = 10
TABLE_ROWS = 300
STALE_S = 60
CHART_H = 220
GAP_S = 35  # more than 3 missed samples breaks a chart line
MIN_SESSION_S = 10

COMPONENTS = ["cpu", "gpu", "ram", "ssd", "board", "fans", "network", "other", "psu_loss"]
LABELS = {"cpu": "CPU", "gpu": "GPU", "ram": "RAM", "ssd": "SSDs", "board": "Motherboard",
          "fans": "Fans / cooling", "network": "Network", "other": "USB / other", "psu_loss": "PSU loss"}
COLUMNS = [f"{c}_w" for c in COMPONENTS] + ["est_total_w"]
TOTAL = len(COMPONENTS)  # index of the total in a row's values
CONFIG_DEFAULTS = {"alert_enabled": True, "alert_watts": 250.0, "alert_seconds": 30, "notify": True}

# Validated categorical palette (fixed order, light / dark steps). Network + USB fold into "Other"
# so stacked charts never need more than 8 hues.
PALETTE = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
}
STACK = [("CPU", ["cpu"]), ("GPU", ["gpu"]), ("RAM", ["ram"]), ("SSDs", ["ssd"]), ("Motherboard", ["board"]),
         ("Fans", ["fans"]), ("PSU loss", ["psu_loss"]), ("Other", ["network", "other"])]
SEQ_BLUE = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6",
            "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]
CRITICAL = "#d03b3b"
SURFACE = {"light": "#fcfcfb", "dark": "#1a1a19"}


def hex_rgb(h):
    return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))


def read_json(name, default=None):
    try:
        with open(os.path.join(LOG_DIR, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_csv(name):
    try:
        with open(os.path.join(LOG_DIR, name), newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def write_json_atomic(name, data):
    path = os.path.join(LOG_DIR, name)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, indent=2)
    os.replace(path + ".tmp", path)


def notify(title, body):
    try:
        subprocess.Popen(["notify-send", "-a", "Power Monitor", "-i", "dialog-information", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def fmt_age(seconds):
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def fmt_dur(secs):
    secs = int(round(secs))
    h, m, s = secs // 3600, secs % 3600 // 60, secs % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_wh(wh):
    if wh >= 1000:
        return f"{wh / 1000:.3f} kWh"
    return f"{wh:.1f} Wh" if wh >= 10 else f"{wh:.2f} Wh" if wh >= 0.1 else f"{wh * 1000:.0f} mWh"


def small(text):
    return f"<small>{GLib.markup_escape_text(text)}</small>"


class CsvTail:
    """Reads a family of daily CSVs incrementally: each refresh only parses bytes appended since the last one.
    parse(index, parts) turns a line into a row tuple whose first item is the timestamp."""

    def __init__(self, prefix, parse, columns):
        self.prefix, self.parse, self.columns = prefix, parse, columns
        self.files = {}

    def load(self, since):
        first_day = since.date().isoformat()
        out = []
        for path in sorted(glob.glob(os.path.join(LOG_DIR, f"{self.prefix}????-??-??.csv"))):
            if os.path.basename(path)[len(self.prefix):-4] < first_day:
                continue
            ent = self.files.setdefault(path, {"offset": 0, "index": None, "rows": []})
            try:
                with open(path, "rb") as f:
                    header = f.readline()
                    # Start over if the file was truncated or rewritten with a different header (format upgrade).
                    if os.path.getsize(path) < ent["offset"] or ent.get("header", header) != header:
                        ent.update(offset=0, index=None, rows=[])
                    ent["header"] = header
                    f.seek(ent["offset"])
                    chunk = f.read()
            except OSError:
                continue
            end = chunk.rfind(b"\n") + 1  # ignore a half-written last line
            for line in chunk[:end].decode(errors="replace").splitlines():
                p = next(csv.reader([line])) if '"' in line else line.split(",")
                if ent["index"] is None:  # header: map our columns to positions in this file
                    ent["index"] = {c: (p.index(c) if c in p else None) for c in self.columns}
                    continue
                try:
                    ent["rows"].append(self.parse(ent["index"], p))
                except (ValueError, IndexError, TypeError):
                    pass
            ent["offset"] += end
            out.extend(r for r in ent["rows"] if r[0] >= since)
        return out


def num(p, i):
    return float(p[i]) if i is not None and p[i] else 0.0


def parse_power(ix, p):
    """(timestamp, interval_s, values ordered like COLUMNS)"""
    return (dt.datetime.fromisoformat(p[ix["timestamp"]]), float(p[ix["interval_s"]]),
            tuple(num(p, ix[c]) for c in COLUMNS))


def parse_proc(ix, p):
    """(timestamp, interval_s, process, cpu_pct, cpu_w, gpu_pct, gpu_w, total_w)"""
    return (dt.datetime.fromisoformat(p[ix["timestamp"]]), float(p[ix["interval_s"]]), p[ix["process"]],
            num(p, ix["cpu_pct"]), num(p, ix["cpu_w"]), num(p, ix["gpu_pct"]), num(p, ix["gpu_w"]),
            num(p, ix["total_w"]))


PROC_COLUMNS = ["timestamp", "interval_s", "process", "cpu_pct", "cpu_w", "gpu_pct", "gpu_w", "total_w"]


class CairoImage:
    """A chart drawn off-screen with pycairo and shown as an image (python3-gi-cairo isn't needed).
    draw(cr, w, hits) draws and returns the height it used; hover text comes from rectangles added to hits."""

    def __init__(self, draw, height=None):
        self.draw_fn, self.fixed_h = draw, height
        self.image = Gtk.Image()
        self.image.set_has_tooltip(True)
        self.image.connect("query-tooltip", self.on_tooltip)
        self.width = 0
        self.hits = []
        self.widget = Gtk.ScrolledWindow()
        # EXTERNAL lets the window shrink below the image width; tall dashboards scroll vertically.
        self.widget.set_policy(Gtk.PolicyType.EXTERNAL,
                               Gtk.PolicyType.NEVER if height else Gtk.PolicyType.AUTOMATIC)
        if height:
            self.widget.set_size_request(-1, height)
        self.widget.add(self.image)
        self.widget.connect("size-allocate", self.on_resize)

    def on_resize(self, widget, alloc):
        if alloc.width != self.width:
            self.width = alloc.width
            GLib.idle_add(self.render)

    def render(self):
        w = self.width - (0 if self.fixed_h else 14)  # leave room for the vertical scrollbar
        if w < 80:
            return False
        h = self.fixed_h or 3000
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        self.hits = []
        used = self.draw_fn(cairo.Context(surface), w, self.hits) or h
        used = int(min(h, used))
        if used < h:  # crop to the height actually drawn
            cropped = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, used)
            cr = cairo.Context(cropped)
            cr.set_source_surface(surface, 0, 0)
            cr.paint()
            surface = cropped
        buf = io.BytesIO()
        surface.write_to_png(buf)
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(buf.getvalue())
        loader.close()
        self.image.set_from_pixbuf(loader.get_pixbuf())
        return False

    def on_tooltip(self, widget, x, y, keyboard, tooltip):
        alloc = self.image.get_allocation()
        pb = self.image.get_pixbuf()
        if pb is None:
            return False
        ox = x - max(0, (alloc.width - pb.get_width()) / 2)
        oy = y - max(0, (alloc.height - pb.get_height()) / 2)
        for hx, hy, hw, hh, text in reversed(self.hits):
            if hx <= ox <= hx + hw and hy <= oy <= hy + hh:
                tooltip.set_text(text)
                return True
        return False


class PowerWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Power Monitor")
        self.set_default_size(800, 800)
        self.power = CsvTail("", parse_power, ["timestamp", "interval_s"] + COLUMNS)
        self.procs = CsvTail("procs-", parse_proc, PROC_COLUMNS)
        self.rows, self.proc_rows, self.today_rows, self.yday_rows = [], [], [], []
        self.since = self.until = dt.datetime.now()
        self.session = None
        self.hw = {}
        self.cfg = dict(CONFIG_DEFAULTS)
        self.alerts = []

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(14)
        self.add(box)

        # Current consumption
        self.now_lbl = Gtk.Label(xalign=0)
        self.parts_lbl = Gtk.Label(xalign=0)
        self.status_lbl = Gtk.Label(xalign=0)
        self.alert_lbl = Gtk.Label(xalign=0)
        self.alert_lbl.set_line_wrap(True)
        for w in (self.now_lbl, self.parts_lbl, self.status_lbl, self.alert_lbl):
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
        self.price.set_value(float((read_json("ui.json", {}) or {}).get("price", 0)))
        self.price.connect("value-changed", self.on_price)
        ctl.pack_end(self.price, False, False, 0)
        ctl.pack_end(Gtk.Label(label="Price / unit"), False, False, 0)
        box.pack_start(ctl, False, False, 0)

        self.units_lbl = Gtk.Label(xalign=0)
        self.stats_lbl = Gtk.Label(xalign=0)
        box.pack_start(self.units_lbl, False, False, 0)
        box.pack_start(self.stats_lbl, False, False, 0)

        self.tabs = Gtk.Notebook()
        self.tabs.set_scrollable(True)
        box.pack_start(self.tabs, True, True, 4)
        self.build_breakdown()
        self.build_monitor()
        self.build_processes()
        self.build_alerts()
        self.analytics = CairoImage(self.draw_analytics)
        self.add_tab(self.analytics.widget, "Analytics")
        self.chart = CairoImage(self.draw_chart, CHART_H)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.set_border_width(8)
        page.pack_start(self.chart.widget, False, False, 0)
        self.add_tab(page, "Chart")
        self.build_log()

        note = Gtk.Label(xalign=0)
        note.set_markup(small("CPU and GPU are measured; other parts are estimated (hover a component for how). "
                              "Logs: " + LOG_DIR))
        note.set_line_wrap(True)
        box.pack_start(note, False, False, 0)

        self.connect("destroy", Gtk.main_quit)
        self.refresh()
        GLib.timeout_add_seconds(REFRESH_S, self.refresh)
        GLib.timeout_add_seconds(1, self.tick_session)

    # ------------------------------------------------------------------ layout helpers

    def add_tab(self, widget, title):
        self.tabs.append_page(widget, Gtk.Label(label=title))

    def tree(self, columns, types, sort_cols=None, text_cols=(0,)):
        """TreeView with display columns; sort_cols maps a column to a hidden numeric model column.
        Columns in text_cols are left-aligned, the rest (numbers) right-aligned."""
        model = Gtk.ListStore(*types)
        view = Gtk.TreeView(model=model)
        for i, title in enumerate(columns):
            cell = Gtk.CellRendererText()
            if i not in text_cols:
                cell.set_property("xalign", 1.0)
            col = Gtk.TreeViewColumn(title, cell, text=i)
            if sort_cols and i in sort_cols:
                col.set_sort_column_id(sort_cols[i])
            col.set_resizable(True)
            view.append_column(col)
        scroll = Gtk.ScrolledWindow()
        scroll.add(view)
        return model, view, scroll

    def theme(self):
        fg = self.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        mode = "dark" if (fg.red + fg.green + fg.blue) / 3 > 0.5 else "light"
        return mode, (fg.red, fg.green, fg.blue)

    # ------------------------------------------------------------------ tabs

    def build_breakdown(self):
        self.grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        self.grid.set_border_width(10)
        for col, text in enumerate(["Component", "Hardware", "Now W", "Avg W", "kWh", "Share"]):
            lbl = Gtk.Label(xalign=0 if col < 2 else 1)
            lbl.set_markup(f"<b>{text}</b>")
            self.grid.attach(lbl, col, 0, 1, 1)
        self.cells = {}
        for row, c in enumerate(COMPONENTS + ["total"], start=1):
            name = Gtk.Label(xalign=0, yalign=0)
            hw = Gtk.Label(xalign=0, yalign=0)
            hw.set_line_wrap(True)
            hw.set_max_width_chars(38)
            hw.set_hexpand(True)
            nums = [Gtk.Label(xalign=1, yalign=0) for _ in range(4)]
            for col, w in enumerate([name, hw] + nums):
                self.grid.attach(w, col, row, 1, 1)
            self.cells[c] = (name, hw, nums)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.grid)
        self.add_tab(scroll, "Breakdown")

    def build_monitor(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_border_width(10)

        intro = Gtk.Label(xalign=0)
        intro.set_markup(small("Tick what to watch, set how long (10 s minimum), press Start. "
                               "Results update with every reading (every 10 s) and are saved when it ends."))
        intro.set_line_wrap(True)
        page.pack_start(intro, False, False, 0)

        top = Gtk.Box(spacing=12)
        page.pack_start(top, True, True, 0)

        # Checklist: system items and recent processes
        self.mon_store = Gtk.ListStore(bool, str, str)  # checked, label, key ("c:cpu" / "p:firefox")
        view = Gtk.TreeView(model=self.mon_store)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self.on_mon_toggle)
        view.append_column(Gtk.TreeViewColumn("", toggle, active=0))
        view.append_column(Gtk.TreeViewColumn("Item", Gtk.CellRendererText(), text=1))
        view.set_headers_visible(False)
        scroll = Gtk.ScrolledWindow()
        scroll.set_size_request(260, -1)
        scroll.add(view)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        left.pack_start(scroll, True, True, 0)
        btns = Gtk.Box(spacing=4)
        for label, fn in (("All", lambda *_: self.set_all_checks(True)), ("None", lambda *_: self.set_all_checks(False)),
                          ("Refresh processes", lambda *_: self.fill_monitor_items())):
            b = Gtk.Button(label=label)
            b.connect("clicked", fn)
            btns.pack_start(b, False, False, 0)
        left.pack_start(btns, False, False, 0)
        top.pack_start(left, False, False, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        top.pack_start(right, True, True, 0)
        dur = Gtk.Box(spacing=4)
        dur.pack_start(Gtk.Label(label="Monitor for"), False, False, 0)
        self.mon_h = Gtk.SpinButton.new_with_range(0, 99, 1)
        self.mon_m = Gtk.SpinButton.new_with_range(0, 59, 1)
        self.mon_s = Gtk.SpinButton.new_with_range(0, 59, 1)
        self.mon_m.set_value(1)
        for spin, unit in ((self.mon_h, "h"), (self.mon_m, "m"), (self.mon_s, "s")):
            spin.set_width_chars(2)
            dur.pack_start(spin, False, False, 0)
            dur.pack_start(Gtk.Label(label=unit), False, False, 2)
        self.mon_btn = Gtk.Button(label="Start")
        self.mon_btn.get_style_context().add_class("suggested-action")
        self.mon_btn.connect("clicked", self.on_mon_start_stop)
        dur.pack_end(self.mon_btn, False, False, 0)
        right.pack_start(dur, False, False, 0)

        self.mon_progress = Gtk.ProgressBar(show_text=True)
        right.pack_start(self.mon_progress, False, False, 0)
        self.mon_status = Gtk.Label(xalign=0)
        self.mon_status.set_line_wrap(True)
        right.pack_start(self.mon_status, False, False, 0)

        self.mon_model, _, scroll = self.tree(["Item", "Now W", "Avg W", "Peak W", "Energy", "Readings"],
                                              [str] * 6)
        right.pack_start(scroll, True, True, 0)
        self.add_tab(page, "Monitor Now")
        self.fill_monitor_items()

    def build_processes(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        page.set_border_width(8)
        note = Gtk.Label(xalign=0)
        note.set_markup(small("Power each program caused above idle, from its share of CPU time and GPU use. "
                              "Click a column to sort."))
        note.set_line_wrap(True)
        page.pack_start(note, False, False, 0)
        cols = ["Process", "Energy", "Avg W", "Peak W", "CPU W", "GPU W", "Max CPU %", "Share"]
        self.proc_model, _, scroll = self.tree(cols, [str] * 8 + [float] * 7,
                                               {1: 8, 2: 9, 3: 10, 4: 11, 5: 12, 6: 13, 7: 14})
        self.proc_model.set_sort_column_id(8, Gtk.SortType.DESCENDING)
        page.pack_start(scroll, True, True, 0)
        self.add_tab(page, "Processes")

    def build_alerts(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_border_width(10)
        cfg = {**CONFIG_DEFAULTS, **(read_json("config.json", {}) or {})}
        row = Gtk.Box(spacing=6)
        self.al_on = Gtk.CheckButton(label="Alert when total ≥")
        self.al_on.set_active(bool(cfg["alert_enabled"]))
        self.al_w = Gtk.SpinButton.new_with_range(10, 5000, 10)
        self.al_w.set_value(float(cfg["alert_watts"]))
        self.al_s = Gtk.SpinButton.new_with_range(10, 3600, 10)
        self.al_s.set_value(float(cfg["alert_seconds"]))
        self.al_notify = Gtk.CheckButton(label="Desktop notification")
        self.al_notify.set_active(bool(cfg["notify"]))
        save = Gtk.Button(label="Save")
        save.connect("clicked", self.on_save_alerts)
        for w in (self.al_on, self.al_w, Gtk.Label(label="W for at least"), self.al_s, Gtk.Label(label="s"),
                  self.al_notify):
            row.pack_start(w, False, False, 0)
        row.pack_end(save, False, False, 0)
        page.pack_start(row, False, False, 0)
        self.al_saved = Gtk.Label(xalign=0)
        page.pack_start(self.al_saved, False, False, 0)
        self.al_banner = Gtk.Label(xalign=0)
        self.al_banner.set_line_wrap(True)
        page.pack_start(self.al_banner, False, False, 0)
        self.al_model, _, scroll = self.tree(
            ["Start", "Duration", "Peak W", "Avg W", "Energy", "Limit W", "Top processes"], [str] * 7,
            text_cols=(0, 6))
        page.pack_start(scroll, True, True, 0)
        self.add_tab(page, "Alerts")

    def build_log(self):
        self.model, _, scroll = self.tree(["Time"] + [LABELS[c] for c in COMPONENTS] + ["Total"],
                                          [str] * (len(COLUMNS) + 1))
        self.add_tab(scroll, "Log (W)")

    # ------------------------------------------------------------------ events

    def on_price(self, *_):
        try:
            write_json_atomic("ui.json", {"price": self.price.get_value()})
        except OSError:
            pass
        self.refresh()

    def on_save_alerts(self, *_):
        cfg = {"alert_enabled": self.al_on.get_active(), "alert_watts": self.al_w.get_value(),
               "alert_seconds": int(self.al_s.get_value()), "notify": self.al_notify.get_active()}
        try:
            write_json_atomic("config.json", cfg)
            self.al_saved.set_markup(small(f"Saved {dt.datetime.now():%H:%M:%S}. The logger picks it up "
                                           "with its next reading."))
        except OSError as e:
            self.al_saved.set_markup(f"<span foreground='{CRITICAL}'>Couldn't save: "
                                     f"{GLib.markup_escape_text(str(e))}</span>")
        self.refresh()

    def on_mon_toggle(self, cell, path):
        if self.session:
            return  # the selection is fixed while a session runs
        self.mon_store[path][0] = not self.mon_store[path][0]

    def set_all_checks(self, value):
        if not self.session:
            for row in self.mon_store:
                row[0] = value

    def fill_monitor_items(self):
        if self.session:
            return
        checked = {row[2] for row in self.mon_store if row[0]} or {"c:total"}
        self.mon_store.clear()
        for key, label in [("c:total", "Total (wall)")] + [(f"c:{c}", LABELS[c]) for c in COMPONENTS]:
            self.mon_store.append([key in checked, label, key])
        recent = self.procs.load(dt.datetime.now() - dt.timedelta(minutes=15))
        energy = {}
        for r in recent:
            energy[r[2]] = energy.get(r[2], 0.0) + r[7] * r[1]
        for name in sorted(energy, key=lambda n: -energy[n])[:30]:
            self.mon_store.append([f"p:{name}" in checked, f"⚙ {name}", f"p:{name}"])

    def on_mon_start_stop(self, *_):
        if self.session:
            self.finish_session(stopped=True)
            return
        total = int(self.mon_h.get_value()) * 3600 + int(self.mon_m.get_value()) * 60 + int(self.mon_s.get_value())
        items = [(row[2], row[1].replace("⚙ ", "", 1)) for row in self.mon_store if row[0]]
        if total < MIN_SESSION_S:
            self.mon_status.set_markup(f"<span foreground='{CRITICAL}'>Pick at least {MIN_SESSION_S} seconds.</span>")
            return
        if not items:
            self.mon_status.set_markup(f"<span foreground='{CRITICAL}'>Tick at least one item to monitor.</span>")
            return
        start = dt.datetime.now().replace(microsecond=0)
        self.session = {"start": start, "end": start + dt.timedelta(seconds=total), "secs": total, "items": items}
        self.mon_btn.set_label("Stop")
        self.mon_btn.get_style_context().remove_class("suggested-action")
        self.mon_btn.get_style_context().add_class("destructive-action")
        for spin in (self.mon_h, self.mon_m, self.mon_s):
            spin.set_sensitive(False)
        self.tick_session()

    def session_results(self):
        """Per item: (label, now W, avg W, peak W, Wh, readings) over the readings logged during the session."""
        s = self.session
        power = [r for r in self.power.load(s["start"]) if s["start"] < r[0] <= s["end"]]
        procs = {}
        for r in self.procs.load(s["start"]):
            if s["start"] < r[0] <= s["end"]:
                procs.setdefault(r[0], {})
                procs[r[0]][r[2]] = procs[r[0]].get(r[2], 0.0) + r[7]
        out = []
        for key, label in s["items"]:
            kind, name = key.split(":", 1)
            series = []
            for ts, iv, v in power:
                if kind == "c":
                    w = v[TOTAL] if name == "total" else v[COMPONENTS.index(name)]
                else:
                    w = procs.get(ts, {}).get(name, 0.0)
                series.append((w, iv))
            if series:
                secs = sum(iv for _, iv in series)
                j = sum(w * iv for w, iv in series)
                out.append((label, series[-1][0], j / secs, max(w for w, _ in series), j / 3600, len(series)))
            else:
                out.append((label, None, None, None, 0.0, 0))
        return out

    def tick_session(self):
        s = self.session
        if not s:
            return True
        now = dt.datetime.now()
        elapsed = min(s["secs"], (now - s["start"]).total_seconds())
        self.mon_progress.set_fraction(elapsed / s["secs"])
        self.mon_progress.set_text(f"{fmt_dur(elapsed)} / {fmt_dur(s['secs'])}  ·  {fmt_dur(s['secs'] - elapsed)} left")
        results = self.session_results()
        self.mon_model.clear()
        for label, now_w, avg, peak, wh, n in results:
            if n:
                self.mon_model.append([label, f"{now_w:.1f}", f"{avg:.1f}", f"{peak:.1f}", fmt_wh(wh), str(n)])
            else:
                self.mon_model.append([label, "—", "—", "—", "—", "waiting"])
        last = self.power.load(now - dt.timedelta(minutes=1))
        nxt = REFRESH_S - (now - last[-1][0]).total_seconds() if last else REFRESH_S
        self.mon_status.set_markup(small(f"Monitoring since {s['start']:%H:%M:%S}, until {s['end']:%H:%M:%S}. "
                                         f"Next reading in about {max(0, nxt):.0f} s."))
        if now >= s["end"] + dt.timedelta(seconds=2):  # give the last reading a moment to be written
            self.finish_session()
        return True

    def finish_session(self, stopped=False):
        s = self.session
        end = min(dt.datetime.now().replace(microsecond=0), s["end"])
        s["end"] = end
        results = self.session_results()
        self.session = None
        path = os.path.join(LOG_DIR, "monitor_sessions.csv")
        try:
            new = not os.path.exists(path)
            with open(path, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["session_start", "session_end", "duration_s", "item", "readings",
                                "avg_w", "peak_w", "wh"])
                for label, _, avg, peak, wh, n in results:
                    w.writerow([s["start"].isoformat(), end.isoformat(), round((end - s["start"]).total_seconds()),
                                label, n, f"{avg:.2f}" if n else "", f"{peak:.2f}" if n else "", f"{wh:.3f}"])
            saved = f"Saved to {path}"
        except OSError as e:
            saved = f"Couldn't save: {e}"
        lines = [f"{label}: avg {avg:.1f} W, peak {peak:.1f} W, {fmt_wh(wh)}"
                 for label, _, avg, peak, wh, n in results if n]
        what = "stopped" if stopped else "finished"
        notify(f"Monitor Now {what} ({fmt_dur((end - s['start']).total_seconds())})",
               "\n".join(lines[:6]) or "No readings were logged. Is the logger running?")
        self.mon_model.clear()
        for label, now_w, avg, peak, wh, n in results:
            if n:
                self.mon_model.append([label, f"{now_w:.1f}", f"{avg:.1f}", f"{peak:.1f}", fmt_wh(wh), str(n)])
            else:
                self.mon_model.append([label, "—", "—", "—", "—", "no readings"])
        self.mon_status.set_markup(small(f"Session {what} at {end:%H:%M:%S}. {saved}"))
        if not stopped:
            self.mon_progress.set_fraction(1.0)
        self.mon_btn.set_label("Start")
        self.mon_btn.get_style_context().remove_class("destructive-action")
        self.mon_btn.get_style_context().add_class("suggested-action")
        for spin in (self.mon_h, self.mon_m, self.mon_s):
            spin.set_sensitive(True)

    # ------------------------------------------------------------------ refresh

    def refresh(self):
        hours = self.hours.get_value()
        self.until = dt.datetime.now()
        self.since = self.until - dt.timedelta(hours=hours)
        midnight = self.until.replace(hour=0, minute=0, second=0, microsecond=0)
        yday = midnight - dt.timedelta(days=1)
        all_rows = self.power.load(min(self.since, yday))
        self.rows = rows = [r for r in all_rows if r[0] >= self.since]
        self.today_rows = [r for r in all_rows if r[0] >= midnight]
        self.yday_rows = [r for r in all_rows if yday <= r[0] < midnight]
        self.proc_rows = self.procs.load(self.since)
        self.hw = {c["key"]: c for c in (read_json("hardware.json", {}) or {}).get("components", [])}
        self.cfg = {**CONFIG_DEFAULTS, **(read_json("config.json", {}) or {})}
        self.alerts = read_csv("alerts.csv")
        self.active_alert = read_json("alert_active.json")
        price = self.price.get_value()

        self.update_alert_banner()
        if not rows:
            self.now_lbl.set_markup("<span size='xx-large' weight='bold'>— W</span>")
            self.parts_lbl.set_text("")
            self.status_lbl.set_markup(f"<span foreground='{CRITICAL}'>No data in the last {hours:.0f} h. "
                                       "Is power-monitor.service running?</span>")
            self.units_lbl.set_markup("<span size='large' weight='bold'>0 units (kWh)</span>")
            self.stats_lbl.set_text("")
            self.model.clear()
            self.fill_breakdown(None, None, 0)
        else:
            last = rows[-1]
            now_v = last[2]
            age = (self.until - last[0]).total_seconds()
            self.now_lbl.set_markup(f"<span size='xx-large' weight='bold'>{now_v[TOTAL]:.0f} W</span>"
                                    "  now (estimated total at the wall)")
            self.parts_lbl.set_text(f"CPU {now_v[0]:.1f} W   ·   GPU {now_v[1]:.1f} W   ·   "
                                    f"rest of system {now_v[TOTAL] - now_v[0] - now_v[1]:.1f} W")
            if age > STALE_S:
                self.status_lbl.set_markup(f"<span foreground='{CRITICAL}'>Logger looks stopped: last sample "
                                           f"{fmt_age(age)} ago</span>")
            else:
                self.status_lbl.set_markup(f"<span foreground='#0ca30c'>●</span> Logging  ·  last sample "
                                           f"{fmt_age(age)} ago")
            secs = sum(r[1] for r in rows)
            joules = [sum(r[2][i] * r[1] for r in rows) for i in range(len(COLUMNS))]
            kwh = joules[TOTAL] / 3.6e6
            avg_w = joules[TOTAL] / secs
            peak = max(r[2][TOTAL] for r in rows)
            cost = f"   ·   cost {kwh * price:.2f}" if price else ""
            self.units_lbl.set_markup(f"<span size='large' weight='bold'>{kwh:.3f} units (kWh)</span>"
                                      f" in the last {hours:.0f} h{cost}")
            month_cost = f" (cost {avg_w * 0.72 * price:.0f})" if price else ""
            self.stats_lbl.set_text(f"avg {avg_w:.0f} W   ·   peak {peak:.0f} W   ·   logged {secs / 3600:.1f} h"
                                    f"   ·   at this average: {avg_w * 0.024:.2f} kWh/day, "
                                    f"{avg_w * 0.72:.0f} kWh/30 days{month_cost}")
            self.fill_breakdown(now_v, joules, secs)
            self.model.clear()
            for ts, _, v in reversed(rows[-TABLE_ROWS:]):
                self.model.append([ts.strftime("%d %b %H:%M:%S")] + [f"{x:.1f}" for x in v])

        self.fill_processes()
        self.fill_alerts()
        self.chart.render()
        self.analytics.render()
        if not self.session and len(self.mon_store) <= len(COMPONENTS) + 1:
            self.fill_monitor_items()
        return True

    def update_alert_banner(self):
        a = getattr(self, "active_alert", None)
        if a:
            top = ", ".join(f"{n} {w:.0f} W" for n, w in a.get("top", [])[:3]) or "no single process"
            text = (f"⚠ <b>Power alert</b>: {a['now_w']:.0f} W now, above {a['threshold_w']:.0f} W for "
                    f"{fmt_dur(a['duration_s'])} (peak {a['peak_w']:.0f} W). Top: {GLib.markup_escape_text(top)}")
            markup = f"<span foreground='{CRITICAL}'>{text}</span>"
            self.alert_lbl.set_markup(markup)
            self.al_banner.set_markup(markup)
            self.alert_lbl.show()
        else:
            self.alert_lbl.set_text("")
            self.alert_lbl.hide()
            self.al_banner.set_markup(small("No alert in progress."))

    def fill_breakdown(self, now_v, joules, secs):
        total_j = joules[TOTAL] if joules else 0
        fans = read_json("fans.json", {}) or {}
        for i, c in enumerate(COMPONENTS + ["total"]):
            name, hw_lbl, nums = self.cells[c]
            info = self.hw.get(c, {})
            source = info.get("source", "")
            if c == "total":
                name.set_markup("<b>Total</b>")
                hw_lbl.set_markup(small("Estimated power drawn from the wall socket"))
            else:
                tag = "" if source.startswith("Measured") else " <small>(est.)</small>"
                name.set_markup(GLib.markup_escape_text(LABELS[c]) + tag)
                text = info.get("hardware", "")
                if c == "fans" and fans.get("fans"):
                    lines = [f"{f['label']}: {f['rpm']} RPM, {f['w']:.1f} W" for f in fans["fans"] if f["rpm"]]
                    idle = [f["label"] for f in fans["fans"] if not f["rpm"]]
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

    def process_totals(self):
        """{name: [joules, peak W, cpu joules, gpu joules, max cpu %]} over the window."""
        agg = {}
        for ts, iv, name, cpu_pct, cpu_w, gpu_pct, gpu_w, w in self.proc_rows:
            a = agg.setdefault(name, [0.0, 0.0, 0.0, 0.0, 0.0])
            a[0] += w * iv
            a[1] = max(a[1], w)
            a[2] += cpu_w * iv
            a[3] += gpu_w * iv
            a[4] = max(a[4], cpu_pct)
        return agg

    def fill_processes(self):
        agg = self.process_totals()
        secs = sum(r[1] for r in self.rows) or 1
        total_j = sum(a[0] for a in agg.values()) or 1
        self.proc_model.clear()
        for name, (j, peak, cj, gj, maxcpu) in agg.items():
            wh = j / 3600
            self.proc_model.append([name, fmt_wh(wh), f"{j / secs:.2f}", f"{peak:.1f}", f"{cj / secs:.2f}",
                                    f"{gj / secs:.2f}", f"{maxcpu:.0f}", f"{j / total_j:.1%}",
                                    wh, j / secs, peak, cj / secs, gj / secs, maxcpu, j / total_j])

    def fill_alerts(self):
        self.al_model.clear()
        for r in reversed(self.alerts[-500:]):
            try:
                self.al_model.append([r["start"].replace("T", " "), fmt_dur(float(r["duration_s"])),
                                      f"{float(r['peak_w']):.0f}", f"{float(r['avg_w']):.0f}",
                                      fmt_wh(float(r["kwh"]) * 1000), r["threshold_w"], r["top_processes"]])
            except (KeyError, ValueError):
                pass

    # ------------------------------------------------------------------ drawing helpers

    def ink(self, cr, alpha=1.0):
        _, fg = self.theme()
        cr.set_source_rgba(*fg, alpha)

    def text(self, cr, x, y, s, size=11, alpha=0.9, bold=False, align="left"):
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(size)
        self.ink(cr, alpha)
        ext = cr.text_extents(s)
        if align == "right":
            x -= ext.x_advance
        elif align == "center":
            x -= ext.x_advance / 2
        cr.move_to(x, y)
        cr.show_text(s)
        return ext.x_advance

    def card(self, cr, x, y, w, h):
        mode, _ = self.theme()
        r = 8
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
        cr.set_source_rgb(*hex_rgb(SURFACE[mode]))
        cr.fill_preserve()
        self.ink(cr, 0.10)
        cr.set_line_width(1)
        cr.stroke()

    def bar(self, cr, x, y, w, h, rgb, round_top=True):
        """Bar anchored at the bottom (y + h) with a 4 px rounded data end."""
        if h <= 0 or w <= 0:
            return
        r = min(4, w / 2, h) if round_top else 0
        cr.new_sub_path()
        cr.move_to(x, y + h)
        cr.line_to(x, y + r)
        if r:
            cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
            cr.arc(x + w - r, y + r, r, 3 * math.pi / 2, 0)
        else:
            cr.line_to(x + w, y)
        cr.line_to(x + w, y + h)
        cr.close_path()
        cr.set_source_rgb(*rgb)
        cr.fill()

    def legend(self, cr, x, y, items, max_w):
        """items: [(label, rgb)] laid out left to right, wrapping. Returns the height used."""
        x0, cx, cy = x, x, y
        for label, rgb in items:
            cr.set_font_size(10.5)
            wlab = cr.text_extents(label).x_advance + 26
            if cx + wlab > x0 + max_w:
                cx, cy = x0, cy + 18
            cr.set_source_rgb(*rgb)
            cr.rectangle(cx, cy - 8, 10, 10)
            cr.fill()
            self.text(cr, cx + 14, cy + 1, label, 10.5, 0.8)
            cx += wlab
        return cy - y + 18

    def palette(self):
        mode, _ = self.theme()
        return [hex_rgb(h) for h in PALETTE[mode]]

    # ------------------------------------------------------------------ live chart tab

    def draw_chart(self, cr, w, hits):
        h = CHART_H
        L, R, T, B = 48, 10, 26, 20
        pw, ph = w - L - R, h - T - B
        mode, fg = self.theme()
        pal = self.palette()
        series = [("Total", fg, lambda v: v[TOTAL]), ("CPU", pal[0], lambda v: v[0]),
                  ("GPU", pal[1], lambda v: v[1]), ("Other parts", pal[2], lambda v: v[TOTAL] - v[0] - v[1])]
        if not self.rows or pw < 10:
            self.text(cr, L, T + ph / 2, "No data to chart", 11, 0.6)
            return h

        t0, span = self.since.timestamp(), (self.until - self.since).total_seconds()
        n = int(pw)
        sums = [[0.0] * len(series) for _ in range(n)]
        counts = [0] * n
        for ts, _, v in self.rows:
            x = min(n - 1, max(0, int((ts.timestamp() - t0) / span * n)))
            counts[x] += 1
            for k, (_, _, fn) in enumerate(series):
                sums[x][k] += fn(v)
        top = max((sums[x][0] / counts[x] for x in range(n) if counts[x]), default=1)
        threshold = float(self.cfg["alert_watts"]) if self.cfg.get("alert_enabled") else 0
        if threshold and threshold < top * 1.6:
            top = max(top, threshold)
        step = 10 ** math.floor(math.log10(max(top, 1)))
        ymax = math.ceil(top * 1.1 / step) * step

        def y_of(val):
            return T + ph - val / ymax * ph

        cr.set_line_width(1)
        for i in range(5):
            val = ymax * i / 4
            y = round(y_of(val)) + 0.5
            self.ink(cr, 0.10)
            cr.move_to(L, y)
            cr.line_to(L + pw, y)
            cr.stroke()
            self.text(cr, L - 6, y + 3, f"{val:.0f} W", 10, 0.6, align="right")
        self.text(cr, L, h - 4, self.since.strftime("%d %b %H:%M"), 10, 0.6)
        self.text(cr, L + pw, h - 4, "now", 10, 0.6, align="right")

        if threshold and threshold <= ymax:  # alert threshold, dashed, labelled
            y = round(y_of(threshold)) + 0.5
            cr.set_source_rgb(*hex_rgb(CRITICAL))
            cr.set_line_width(1)
            cr.set_dash([4, 3])
            cr.move_to(L, y)
            cr.line_to(L + pw, y)
            cr.stroke()
            cr.set_dash([])
            self.text(cr, L + pw - 4, y - 4, f"alert {threshold:.0f} W", 10, 0.75, align="right")

        max_gap_px = max(1.0, GAP_S / span * n)
        cr.set_line_width(2)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        for k, (_, rgb, _) in enumerate(series):
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
                    cr.line_to(px + 0.5, py)
                last_x = x
            cr.stroke()

        # hover: one strip per pixel column with data
        for x in range(n):
            if counts[x]:
                t = dt.datetime.fromtimestamp(t0 + (x + 0.5) / n * span)
                vals = "\n".join(f"{name}: {sums[x][k] / counts[x]:.1f} W" for k, (name, _, _) in enumerate(series))
                hits.append((L + x - 1, T, 3, ph, f"{t:%d %b %H:%M:%S}\n{vals}"))
        self.legend(cr, L, 12, [(name, rgb) for name, rgb, _ in series], pw)
        return h

    # ------------------------------------------------------------------ analytics dashboard

    def draw_analytics(self, cr, w, hits):
        pad = 12
        y = pad
        y = self.draw_kpis(cr, pad, y, w - 2 * pad, hits) + pad
        y = self.draw_daily(cr, pad, y, w - 2 * pad, hits) + pad
        y = self.draw_heatmap(cr, pad, y, w - 2 * pad, hits) + pad
        half = (w - 3 * pad) / 2
        if half > 300:
            y1 = self.draw_profile(cr, pad, y, half, hits)
            y2 = self.draw_share(cr, 2 * pad + half, y, half, hits)
            y = max(y1, y2) + pad
        else:
            y = self.draw_profile(cr, pad, y, w - 2 * pad, hits) + pad
            y = self.draw_share(cr, pad, y, w - 2 * pad, hits) + pad
        y = self.draw_top_procs(cr, pad, y, w - 2 * pad, hits) + pad
        return y

    def window_label(self):
        return f"last {self.hours.get_value():.0f} h"

    def draw_kpis(self, cr, x, y, w, hits):
        price = self.price.get_value()

        def kwh(rows):
            return sum(r[2][TOTAL] * r[1] for r in rows) / 3.6e6

        today, yday = kwh(self.today_rows), kwh(self.yday_rows)
        now = self.until
        yday_same = kwh([r for r in self.yday_rows if r[0] <= now - dt.timedelta(days=1)])
        daily = read_csv("daily_summary.csv")
        past = [float(d["est_total_kwh"]) for d in daily if d["date"] < now.date().isoformat()][-7:]
        week_avg = sum(past) / len(past) if past else None
        month = [d for d in daily if d["date"].startswith(now.strftime("%Y-%m")) and d["date"] < now.date().isoformat()]
        month_kwh = sum(float(d["est_total_kwh"]) for d in month) + today
        month_secs = sum(float(d["hours_logged"]) for d in month) * 3600 + sum(r[1] for r in self.today_rows)
        dim = ((now.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)).day
        proj = month_kwh / month_secs * 3600 * 24 * dim if month_secs else 0

        rows = self.rows
        totals = sorted(r[2][TOTAL] for r in rows)
        idle = totals[int(len(totals) * 0.05)] if totals else None
        peak_row = max(rows, key=lambda r: r[2][TOTAL]) if rows else None
        thr = float(self.cfg["alert_watts"])
        secs = sum(r[1] for r in rows) or 1
        above = sum(r[1] for r in rows if r[2][TOTAL] >= thr) / secs if rows else 0
        win_alerts = [a for a in self.alerts if a.get("start", "") >= self.since.isoformat()]
        alert_secs = sum(float(a["duration_s"]) for a in win_alerts)

        def cost(k):
            return f"  ·  cost {k * price:.2f}" if price else ""

        delta = (f"{(today - yday_same) / yday_same:+.0%} vs same time yesterday" if yday_same > 0.001
                 else "today so far")
        tiles = [
            ("Today", f"{today:.2f} kWh", delta + cost(today)),
            ("Yesterday", f"{yday:.2f} kWh", ("full day" if self.yday_rows else "no data") + cost(yday)),
            ("7-day average", f"{week_avg:.2f} kWh" if week_avg is not None else "—",
             f"per day, {len(past)} day(s) logged" if past else "needs a full day of data"),
            (now.strftime("%B"), f"{month_kwh:.1f} kWh", f"so far  ·  projected {proj:.0f} kWh" + cost(proj)),
            ("Idle baseline", f"{idle:.0f} W" if idle is not None else "—",
             f"lowest 5% of readings, {self.window_label()}"),
            ("Peak", f"{peak_row[2][TOTAL]:.0f} W" if peak_row else "—",
             f"at {peak_row[0]:%d %b %H:%M}" if peak_row else ""),
            ("Above alert limit", f"{above:.1%}", f"of the time at or above {thr:.0f} W, {self.window_label()}"),
            ("Alerts", f"{len(win_alerts)}", f"{fmt_dur(alert_secs)} in total, {self.window_label()}"),
        ]
        cols = 4 if w >= 640 else 2
        gap = 10
        tw = (w - gap * (cols - 1)) / cols
        th = 74
        for i, (title, value, sub) in enumerate(tiles):
            cx = x + (i % cols) * (tw + gap)
            cy = y + (i // cols) * (th + gap)
            self.card(cr, cx, cy, tw, th)
            self.text(cr, cx + 12, cy + 20, title, 10.5, 0.65)
            self.text(cr, cx + 12, cy + 46, value, 20, 0.95, bold=True)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(9.5)
            s = sub
            while s and cr.text_extents(s + "…").x_advance > tw - 20:
                s = s[:-1]
            self.text(cr, cx + 12, cy + 64, sub if s == sub else s.rstrip() + "…", 9.5, 0.6)
            hits.append((cx, cy, tw, th, f"{title}: {value}\n{sub}"))
        rows_n = math.ceil(len(tiles) / cols)
        return y + rows_n * th + (rows_n - 1) * gap

    def draw_daily(self, cr, x, y, w, hits):
        daily = read_csv("daily_summary.csv")[-30:]
        h = 260
        self.card(cr, x, y, w, h)
        self.text(cr, x + 14, y + 22, "Energy per day", 12.5, 0.95, bold=True)
        self.text(cr, x + 14, y + 38, "kWh by component, last 30 days logged (today updates hourly)", 10, 0.6)
        pal = self.palette()
        lh = self.legend(cr, x + 14, y + 58, [(name, pal[i]) for i, (name, _) in enumerate(STACK)], w - 28)
        if not daily:
            self.text(cr, x + 14, y + h / 2 + 20, "No daily summaries yet", 11, 0.6)
            return y + h
        L, T, B = x + 48, y + 58 + lh, y + h - 24
        pw, ph = w - 48 - 14, B - T
        vals = [[sum(float(d.get(f"{c}_kwh") or 0) for c in comps) for _, comps in STACK] for d in daily]
        top = max(sum(p) for p in vals) or 1
        step = 10 ** math.floor(math.log10(top))
        ymax = math.ceil(top * 1.1 / step) * step
        for i in range(4):
            v = ymax * i / 3
            yy = round(B - v / ymax * ph) + 0.5
            self.ink(cr, 0.10)
            cr.set_line_width(1)
            cr.move_to(L, yy)
            cr.line_to(L + pw, yy)
            cr.stroke()
            self.text(cr, L - 6, yy + 3, f"{v:.2f}" if ymax < 1 else f"{v:.1f}", 9.5, 0.6, align="right")
        slot = pw / max(len(daily), 7)
        bw = max(3, min(28, slot * 0.7))
        mode, _ = self.theme()
        surface = hex_rgb(SURFACE[mode])
        for i, (d, parts) in enumerate(zip(daily, vals)):
            bx = L + i * slot + (slot - bw) / 2
            base = B
            nonzero = [k for k, p in enumerate(parts) if p > 0]
            for k, p in enumerate(parts):
                hh = p / ymax * ph
                if hh <= 0:
                    continue
                self.bar(cr, bx, base - hh, bw, hh, pal[k], round_top=(k == nonzero[-1]))
                if k != nonzero[-1]:
                    cr.set_source_rgb(*surface)  # 2 px surface gap between stacked segments
                    cr.rectangle(bx, base - hh - 1, bw, 2)
                    cr.fill()
                base -= hh
            total = sum(parts)
            tip = (f"{d['date']}  ·  {total:.2f} kWh  ·  avg {float(d['avg_w']):.0f} W\n" +
                   "\n".join(f"{name}: {p:.3f} kWh" for (name, _), p in zip(STACK, parts) if p > 0))
            hits.append((L + i * slot, T, slot, ph, tip))
            if len(daily) <= 10 or i % max(1, len(daily) // 8) == 0 or i == len(daily) - 1:
                self.text(cr, bx + bw / 2, B + 14, d["date"][5:], 9, 0.6, align="center")
        return y + h

    def draw_heatmap(self, cr, x, y, w, hits):
        hourly = read_csv("hourly.csv")
        days = sorted({r["date"] for r in hourly})[-14:]
        cell = {(r["date"], int(r["hour"])): r for r in hourly if r["date"] in days}
        rows_n = max(1, len(days))
        L = x + 64
        cw = (w - 64 - 14) / 24
        chh = 18
        h = 70 + rows_n * chh + 40
        self.card(cr, x, y, w, h)
        self.text(cr, x + 14, y + 22, "When is power used?", 12.5, 0.95, bold=True)
        self.text(cr, x + 14, y + 38, "Average W for every hour, last 14 days (darker = more power)", 10, 0.6)
        if not cell:
            self.text(cr, x + 14, y + 70, "Hourly data appears after the first full hour of logging", 11, 0.6)
            return y + h
        T = y + 58
        vals = [float(r["avg_w"]) for r in cell.values()]
        lo, hi = min(vals), max(vals)
        mode, _ = self.theme()
        ramp = SEQ_BLUE if mode == "light" else list(reversed(SEQ_BLUE))

        def color(v):
            t = 0 if hi <= lo else (v - lo) / (hi - lo)
            return hex_rgb(ramp[min(len(ramp) - 1, int(t * (len(ramp) - 1) + 0.5))])

        for hr in range(0, 24, 3):
            self.text(cr, L + hr * cw + cw / 2, T - 4, f"{hr:02d}", 9, 0.6, align="center")
        for i, day in enumerate(days):
            cy = T + i * chh
            label = dt.date.fromisoformat(day).strftime("%a %d")
            self.text(cr, L - 8, cy + chh - 5, label, 9.5, 0.65, align="right")
            for hr in range(24):
                r = cell.get((day, hr))
                if not r:
                    continue
                v = float(r["avg_w"])
                cr.set_source_rgb(*color(v))
                cr.rectangle(L + hr * cw + 1, cy + 1, cw - 2, chh - 2)
                cr.fill()
                hits.append((L + hr * cw, cy, cw, chh,
                             f"{label}, {hr:02d}:00–{hr:02d}:59\navg {v:.0f} W  ·  peak {float(r['peak_w']):.0f} W\n"
                             f"{float(r['kwh']) * 1000:.0f} Wh  ·  {float(r['minutes_logged']):.0f} min logged"))
        ly = T + rows_n * chh + 16  # gradient legend
        lw = min(220, w / 3)
        for k in range(len(ramp)):
            cr.set_source_rgb(*hex_rgb(ramp[k]))
            cr.rectangle(L + k * lw / len(ramp), ly, lw / len(ramp) + 0.5, 8)
            cr.fill()
        self.text(cr, L - 6, ly + 8, f"{lo:.0f} W", 9.5, 0.6, align="right")
        self.text(cr, L + lw + 6, ly + 8, f"{hi:.0f} W", 9.5, 0.6)
        return y + h

    def draw_profile(self, cr, x, y, w, hits):
        hourly = read_csv("hourly.csv")
        days = sorted({r["date"] for r in hourly})[-14:]
        by_hour = {}
        for r in hourly:
            if r["date"] in days:
                a = by_hour.setdefault(int(r["hour"]), [0.0, 0.0])
                m = float(r["minutes_logged"])
                a[0] += float(r["avg_w"]) * m
                a[1] += m
        h = 220
        self.card(cr, x, y, w, h)
        self.text(cr, x + 14, y + 22, "Typical day", 12.5, 0.95, bold=True)
        self.text(cr, x + 14, y + 38, "Average W by hour of day, last 14 days", 10, 0.6)
        avg = {hr: a[0] / a[1] for hr, a in by_hour.items() if a[1]}
        if not avg:
            self.text(cr, x + 14, y + 80, "Not enough data yet", 11, 0.6)
            return y + h
        L, T, B = x + 44, y + 56, y + h - 24
        pw, ph = w - 44 - 14, B - T
        top = max(avg.values())
        step = 10 ** math.floor(math.log10(max(top, 1)))
        ymax = math.ceil(top * 1.1 / step) * step
        for i in range(3):
            v = ymax * i / 2
            yy = round(B - v / ymax * ph) + 0.5
            self.ink(cr, 0.10)
            cr.set_line_width(1)
            cr.move_to(L, yy)
            cr.line_to(L + pw, yy)
            cr.stroke()
            self.text(cr, L - 6, yy + 3, f"{v:.0f}", 9.5, 0.6, align="right")
        slot = pw / 24
        bw = max(2, slot - 2)
        pal = self.palette()
        busiest = max(avg, key=avg.get)
        for hr in range(24):
            if hr % 6 == 0:
                self.text(cr, L + hr * slot + slot / 2, B + 14, f"{hr:02d}", 9, 0.6, align="center")
            if hr not in avg:
                continue
            hh = avg[hr] / ymax * ph
            self.bar(cr, L + hr * slot + (slot - bw) / 2, B - hh, bw, hh, pal[0])
            hits.append((L + hr * slot, T, slot, ph, f"{hr:02d}:00–{hr:02d}:59\naverage {avg[hr]:.0f} W"))
        bx = L + busiest * slot + slot / 2
        self.text(cr, bx, B - avg[busiest] / ymax * ph - 5, f"{avg[busiest]:.0f} W", 9.5, 0.8, align="center")
        return y + h

    def draw_share(self, cr, x, y, w, hits):
        h = 220
        self.card(cr, x, y, w, h)
        self.text(cr, x + 14, y + 22, "Where the energy went", 12.5, 0.95, bold=True)
        self.text(cr, x + 14, y + 38, f"Share of kWh by component, {self.window_label()}", 10, 0.6)
        if not self.rows:
            self.text(cr, x + 14, y + 80, "No data yet", 11, 0.6)
            return y + h
        pal = self.palette()
        joules = [sum(r[2][COMPONENTS.index(c)] * r[1] for r in self.rows for c in comps) for _, comps in STACK]
        total = sum(joules) or 1
        bx, by, bw, bh = x + 14, y + 52, w - 28, 26
        cx = bx
        for k, j in enumerate(joules):
            sw = j / total * bw
            if sw <= 0:
                continue
            cr.set_source_rgb(*pal[k])
            cr.rectangle(cx, by, max(0, sw - 2), bh)  # 2 px surface gap
            cr.fill()
            hits.append((cx, by, sw, bh, f"{STACK[k][0]}: {j / total:.1%}  ·  {fmt_wh(j / 3600)}"))
            cx += sw
        ly = by + bh + 26  # ranked list, direct labels
        order = sorted(range(len(STACK)), key=lambda k: -joules[k])
        col_w = (w - 28) / 2
        for n, k in enumerate(order):
            cxx = bx + (n // 4) * col_w
            cyy = ly + (n % 4) * 22
            cr.set_source_rgb(*pal[k])
            cr.rectangle(cxx, cyy - 9, 10, 10)
            cr.fill()
            self.text(cr, cxx + 16, cyy, STACK[k][0], 10.5, 0.85)
            self.text(cr, cxx + col_w - 14, cyy, f"{joules[k] / total:.0%}  ·  {fmt_wh(joules[k] / 3600)}", 10.5, 0.7,
                      align="right")
            hits.append((cxx, cyy - 13, col_w, 20, f"{STACK[k][0]}: {fmt_wh(joules[k] / 3600)}"))
        return y + h

    def draw_top_procs(self, cr, x, y, w, hits):
        agg = sorted(self.process_totals().items(), key=lambda kv: -kv[1][0])[:10]
        h = 70 + max(1, len(agg)) * 24 + 10
        self.card(cr, x, y, w, h)
        self.text(cr, x + 14, y + 22, "Top processes", 12.5, 0.95, bold=True)
        self.text(cr, x + 14, y + 38, f"Energy caused above idle, {self.window_label()}", 10, 0.6)
        if not agg:
            self.text(cr, x + 14, y + 70, "No process data yet", 11, 0.6)
            return y + h
        pal = self.palette()
        self.legend(cr, x + w - 210, y + 22, [("mostly CPU", pal[0]), ("mostly GPU", pal[1])], 200)
        secs = sum(r[1] for r in self.rows) or 1
        name_w = min(240, w * 0.32)
        L = x + 14 + name_w
        pw = w - 14 - name_w - 150
        top = agg[0][1][0] or 1
        for i, (name, (j, peak, cj, gj, maxcpu)) in enumerate(agg):
            cy = y + 56 + i * 24
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(10.5)
            label = name
            while label and cr.text_extents(label + "…").x_advance > name_w - 12:
                label = label[:-1]
            self.text(cr, x + 14, cy + 14, name if label == name else label + "…", 10.5, 0.85)
            bw = max(2, j / top * pw)
            self.bar_h(cr, L, cy + 4, bw, 14, pal[1] if gj > cj else pal[0])
            self.text(cr, L + bw + 6, cy + 15, f"{fmt_wh(j / 3600)}  ·  {j / secs:.1f} W avg", 10, 0.7)
            hits.append((x, cy, w, 22, f"{name}\n{fmt_wh(j / 3600)} above idle  ·  avg {j / secs:.2f} W  ·  "
                                       f"peak {peak:.1f} W\nCPU {fmt_wh(cj / 3600)}, GPU {fmt_wh(gj / 3600)}, "
                                       f"max CPU {maxcpu:.0f}%"))
        return y + h

    def bar_h(self, cr, x, y, w, h, rgb):
        """Horizontal bar anchored on the left with a 4 px rounded data end."""
        r = min(4, h / 2, w)
        cr.new_sub_path()
        cr.move_to(x, y)
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.line_to(x, y + h)
        cr.close_path()
        cr.set_source_rgb(*rgb)
        cr.fill()


if __name__ == "__main__":
    win = PowerWindow()
    win.show_all()
    win.update_alert_banner()  # hides the banner again if no alert is active
    win.tabs.grab_focus()  # don't start with the cursor in the hours box
    Gtk.main()
