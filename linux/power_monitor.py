#!/usr/bin/env python3
"""Power consumption logger and reporter.

  power-monitor run     sample every component forever, write daily CSVs (needs root for the CPU sensor)
  power-report [today|month|YYYY-MM|all] [--price P]   summarise the logs (no root needed)

Files in LOG_DIR:
  YYYY-MM-DD.csv       timestamp,interval_s,<one *_w column per component>,est_total_w   (one row per sample)
  daily_summary.csv    date,hours_logged,avg_w,peak_w,<one *_kwh column per component>,est_total_kwh
  hardware.json        detected hardware and how each component's power is obtained
  fans.json            latest speed and estimated watts of every fan (when fan sensors are readable)
  procs-YYYY-MM-DD.csv per sample, the processes causing the most power above idle (CPU + GPU share)
  hourly.csv           per hour: average/peak W and kWh
  alerts.csv           every time total power stayed above the alert threshold, with the top processes
  alert_active.json    present while an alert is in progress
  config.json          alert settings (edited from the window): alert_enabled, alert_watts, alert_seconds, notify

CPU and GPU are measured. Everything else has no power sensor on this machine, so it is estimated
from the detected hardware and its live activity (CPU load, disk busy time, network link state).
"""
import argparse
import calendar
import csv
import datetime as dt
import glob
import io
import json
import os
import pwd
import re
import subprocess
import sys
import time
from collections import deque

LOG_DIR = os.environ.get("POWER_LOG_DIR", os.path.expanduser("~/power-logs"))
INTERVAL_S = 10
RAPL = "/sys/class/powercap/intel-rapl:0"

# ---- Estimates for parts without a power sensor. Edit to match your hardware. ----
CPU_EST_W = (10.0, 90.0)          # only used when no RAPL sensor is readable: idle W, extra W at 100% load
RAM_W_PER_DIMM = (2.5, 2.5)        # DDR5 DIMM: idle W, extra W at 100% CPU load
NVME_W = {                         # model substring: idle W, extra W at 100% disk busy
    "S70": (1.5, 6.5),
    "S60": (1.0, 3.5),
}
NVME_W_DEFAULT = (1.5, 4.5)
SATA_W = (0.5, 2.5)
BOARD_W = 9.0                      # Z790 chipset (~6 W) + audio, LEDs, Super I/O
VRM_LOSS = 0.10                    # CPU voltage regulator loss, as a fraction of CPU power
FAN_MAX_W = 2.0                    # one 120 mm fan at FAN_MAX_RPM; motor power scales with (rpm / max rpm)^3
FAN_MIN_W = 0.3                    # fan electronics, drawn whenever it spins
FAN_MAX_RPM = 2000
FANS_W = 5.0                       # fallback when no fan speed sensors are readable (~3 case fans + CPU cooler)
GPU_HWMON = ("amdgpu", "nouveau", "radeon", "i915", "xe")  # their fans are already in the measured GPU power
NET_W = {"ethernet": (0.2, 0.8), "wifi": (0.2, 1.5)}  # link down W, link up W
OTHER_W = 3.0                      # USB controllers and devices, misc
PSU_RATED_W = 750                  # assumed 80+ Gold PSU
PSU_CURVE = [(0.0, 0.70), (0.05, 0.80), (0.10, 0.85), (0.20, 0.89), (0.50, 0.91), (1.0, 0.88)]

PROC_TOP = 10                      # processes logged per sample
PROC_MIN_W = 0.1                   # ...if they cause at least this many watts above idle
IDLE_WINDOW = 2160                 # samples (6 h) used to find the idle floor of CPU and GPU power
INTERPRETERS = {"python", "node", "java", "bash", "sh", "dash", "zsh", "perl", "ruby", "php", "dotnet", "mono"}
CONFIG_DEFAULTS = {"alert_enabled": True, "alert_watts": 250.0, "alert_seconds": 30, "notify": True}

COMPONENTS = ["cpu", "gpu", "ram", "ssd", "board", "fans", "network", "other", "psu_loss"]
LABELS = {"cpu": "CPU", "gpu": "GPU", "ram": "RAM", "ssd": "SSDs", "board": "Motherboard",
          "fans": "Fans / cooling", "network": "Network", "other": "USB / other", "psu_loss": "PSU loss"}
SAMPLE_HEADER = ["timestamp", "interval_s"] + [f"{c}_w" for c in COMPONENTS] + ["est_total_w"]
PROC_HEADER = ["timestamp", "interval_s", "process", "cpu_pct", "cpu_w", "gpu_pct", "gpu_w", "total_w"]
ALERT_HEADER = ["start", "end", "duration_s", "threshold_w", "peak_w", "avg_w", "kwh", "top_processes"]
HOURLY_HEADER = ["date", "hour", "minutes_logged", "avg_w", "peak_w", "kwh"]
SUMMARY_HEADER = (["date", "hours_logged", "avg_w", "peak_w"]
                  + [f"{c}_kwh" for c in COMPONENTS] + ["est_total_kwh"])


def read_int(path):
    with open(path) as f:
        return int(f.read().strip())


def read_text(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def run_cmd(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def clean_name(name):
    name = re.sub(r"\s*\(rev \w+\)|\bCorporation\b|,? Co\., Ltd\.?|,? Inc\.?", "", name)
    return re.sub(r"\s+", " ", name).strip()


def psu_efficiency(load_w):
    frac = load_w / PSU_RATED_W
    for (x0, y0), (x1, y1) in zip(PSU_CURVE, PSU_CURVE[1:]):
        if frac <= x1:
            return y0 + (y1 - y0) * (frac - x0) / (x1 - x0)
    return PSU_CURVE[-1][1]


# ---------------------------------------------------------------- hardware detection

def detect_dimms():
    props = dict(line.split("=", 1) for line in
                 run_cmd(["udevadm", "info", "-q", "property", "-p", "/sys/devices/virtual/dmi/id"]).splitlines()
                 if "=" in line)
    dimms = []
    for i in range(int(props.get("MEMORY_ARRAY_NUM_DEVICES", 0) or 0)):
        size = int(props.get(f"MEMORY_DEVICE_{i}_SIZE", 0) or 0)
        if size:
            dimms.append({"size_gb": size // 2**30, "type": props.get(f"MEMORY_DEVICE_{i}_TYPE", ""),
                          "speed": props.get(f"MEMORY_DEVICE_{i}_CONFIGURED_SPEED_MTS", ""),
                          "part": props.get(f"MEMORY_DEVICE_{i}_PART_NUMBER", "").strip()})
    return dimms


def detect_disks():
    disks = []
    for path in sorted(glob.glob("/sys/block/*")):
        name = os.path.basename(path)
        if name.startswith(("loop", "ram", "zram", "dm-", "sr", "md")) or not os.path.exists(f"{path}/device"):
            continue
        model = read_text(f"{path}/device/model") or name
        size_gb = int(read_text(f"{path}/size", "0")) * 512 / 1e9
        if name.startswith("nvme"):
            w = next((v for k, v in NVME_W.items() if k in model), NVME_W_DEFAULT)
        else:
            w = SATA_W
        disks.append({"dev": name, "model": model, "size_gb": round(size_gb), "watts": w})
    return disks


def detect_nics():
    nics = []
    for path in sorted(glob.glob("/sys/class/net/*")):
        if not os.path.exists(f"{path}/device"):  # skip lo, docker, bridges, veth
            continue
        kind = "wifi" if os.path.exists(f"{path}/wireless") else "ethernet"
        slot = os.path.basename(os.path.realpath(f"{path}/device"))
        desc = clean_name(run_cmd(["lspci", "-s", slot]).split(": ", 1)[-1]) or kind
        nics.append({"iface": os.path.basename(path), "kind": kind, "model": desc})
    return nics


def detect_fans():
    """Fan speed sensors from the motherboard / laptop driver (needs e.g. nct6683, nct6775 or it87 loaded)."""
    fans = []
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        chip = read_text(f"{hw}/name")
        if chip in GPU_HWMON:
            continue
        for f in sorted(glob.glob(f"{hw}/fan*_input"), key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)))):
            n = re.sub(r"\D", "", os.path.basename(f))
            label = read_text(f"{hw}/fan{n}_label") or f"fan{n}"
            fans.append({"label": label, "chip": chip, "path": f})
    return fans


def fan_watts(rpm):
    return FAN_MIN_W + (FAN_MAX_W - FAN_MIN_W) * min(1.5, rpm / FAN_MAX_RPM) ** 3 if rpm > 0 else 0.0


def gpu_names():
    out = []
    for line in run_cmd(["lspci"]).splitlines():
        if re.search(r"VGA compatible|3D controller|Display controller", line):
            out.append(clean_name(line.split(": ", 1)[-1]))
    return ", ".join(out)


def cpu_name():
    for line in read_text("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return "CPU"


# ---------------------------------------------------------------- live readings

def hwmon_gpu_power_files():
    """AMD (and some Intel Arc) cards expose board power in hwmon, readable without root."""
    files = []
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        for name in ("power1_average", "power1_input"):
            found = glob.glob(f"{card}/device/hwmon/hwmon*/{name}")
            if found:
                files.append(found[0])
                break
    return files


def gpu_watts(hwmon_files):
    total = 0.0
    for x in run_cmd(["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"]).split():
        try:
            total += float(x)
        except ValueError:
            pass
    for f in hwmon_files:
        try:
            total += read_int(f) / 1e6
        except (OSError, ValueError):
            pass
    return total


def cpu_busy_ticks():
    f = [int(x) for x in read_text("/proc/stat").splitlines()[0].split()[1:]]
    idle = f[3] + f[4]
    return sum(f) - idle, sum(f)


def disk_busy_ms():
    out = {}
    for line in read_text("/proc/diskstats").splitlines():
        p = line.split()
        if len(p) > 12:
            out[p[2]] = int(p[12])
    return out


def gpu_process_util():
    """{pid: GPU SM utilisation %} from nvidia-smi (empty when no NVIDIA GPU)."""
    util = {}
    for line in run_cmd(["nvidia-smi", "pmon", "-c", "1", "-s", "u"]).splitlines():
        p = line.split()
        if line.startswith("#") or len(p) < 4 or not p[1].isdigit():
            continue
        try:
            util[int(p[1])] = util.get(int(p[1]), 0.0) + float(p[3])
        except ValueError:
            pass  # "-" means no activity
    return util


class ProcTracker:
    """CPU time used by each process (grouped by program name) between calls, from /proc."""

    def __init__(self):
        self.clk = os.sysconf("SC_CLK_TCK")
        self.names = {}  # (pid, starttime) -> name
        self.prev = {}
        self.sample()

    def name_of(self, pid, start, comm):
        key = (pid, start)
        name = self.names.get(key)
        if name is None:
            try:
                name = os.path.basename(os.readlink(f"/proc/{pid}/exe")).replace(" (deleted)", "")
            except OSError:
                name = comm.split("/")[0]  # kernel threads like kworker/3:1
            if re.fullmatch(r"[\d.]+", name):  # binaries named after their version, e.g. 2.1.291
                name = comm
            if re.sub(r"[\d.]+$", "", name) in INTERPRETERS:  # python3.12 -> "python3.12 train.py"
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        args = f.read().split(b"\0")[1:]
                    script = next((a.decode(errors="replace") for a in args if a and not a.startswith(b"-")), "")
                    if script:
                        name = f"{name} {os.path.basename(script)[:40]}"
                except OSError:
                    pass
            self.names[key] = name
        return name

    def sample(self):
        """Returns ({name: CPU ticks used since last call}, {pid: name})."""
        cur, used, pid_names = {}, {}, {}
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/stat") as f:
                    st = f.read()
                r = st.rfind(")")
                fields = st[r + 2:].split()
                ticks, start = int(fields[11]) + int(fields[12]), int(fields[19])
            except (OSError, IndexError, ValueError):
                continue
            pid = int(d)
            cur[pid] = (start, ticks)
            prev = self.prev.get(pid)
            delta = ticks - prev[1] if prev and prev[0] == start else (ticks if self.prev else 0)
            name = self.name_of(pid, start, st[st.find("(") + 1:r])
            pid_names[pid] = name
            if delta > 0:
                used[name] = used.get(name, 0) + delta
        self.prev = cur
        if len(self.names) > 4 * len(cur) + 1000:
            self.names = {k: v for k, v in self.names.items() if cur.get(k[0], (None,))[0] == k[1]}
        return used, pid_names


class Sampler:
    def __init__(self):
        self.dimms = detect_dimms() or [{"size_gb": 0, "type": "", "speed": "", "part": ""}] * 2
        self.disks = detect_disks()
        self.nics = detect_nics()
        self.fans = detect_fans()
        self.fan_live = []
        self.procs = ProcTracker()
        self.has_nvidia = bool(run_cmd(["nvidia-smi", "-L"]).strip())
        self.cpu_hist = deque(maxlen=IDLE_WINDOW)
        self.gpu_hist = deque(maxlen=IDLE_WINDOW)
        self.gpu_hwmon = hwmon_gpu_power_files()
        try:
            self.max_range = read_int(f"{RAPL}/max_energy_range_uj")
            self.prev_e = self.read_cpu_energy()
            self.rapl = True
        except (OSError, ValueError):
            self.max_range, self.prev_e, self.rapl = 0, 0, False
        self.prev_t = time.monotonic()
        self.prev_cpu = cpu_busy_ticks()
        self.prev_disk = disk_busy_ms()

    def read_cpu_energy(self):
        return read_int(f"{RAPL}/energy_uj")

    def hardware(self):
        ram_gb = sum(d["size_gb"] for d in self.dimms)
        ram_desc = f"{ram_gb} GB " + ", ".join(
            f"{d['type']}-{d['speed']} {d['part']}".strip() for d in self.dimms)
        lo, hi = RAM_W_PER_DIMM
        ssd_idle = sum(d["watts"][0] for d in self.disks)
        net_desc = ", ".join(f"{n['model']} ({n['iface']})" for n in self.nics) or "none"
        board = clean_name(f"{read_text('/sys/class/dmi/id/board_vendor')} {read_text('/sys/class/dmi/id/board_name')}")
        info = {
            "cpu": (cpu_name(), "Measured: RAPL package sensor" if self.rapl else
                    f"Estimated from CPU load (no readable power sensor): {CPU_EST_W[0]} W idle"),
            "gpu": (gpu_names() or "GPU",
                    "Measured: driver power sensor (whole card)" if self.gpu_measured() else
                    "No GPU power sensor found (integrated GPU power is included in CPU)"),
            "ram": (f"{ram_desc} ({len(self.dimms)} DIMM)",
                    f"Estimated: {lo} W/DIMM idle, up to +{hi} W/DIMM with CPU load"),
            "ssd": (", ".join(f"{d['model']} {d['size_gb']} GB" for d in self.disks),
                    f"Estimated from disk busy time: {ssd_idle:.1f} W idle total"),
            "board": (board.strip(), f"Estimated: {BOARD_W} W chipset/misc + {VRM_LOSS:.0%} of CPU power (VRM loss)"),
            "fans": ((f"{len(self.fans)} fan headers on {self.fans[0]['chip']}" if self.fans else
                      "Not visible (no fan sensor driver loaded)"),
                     (f"Estimated from each fan's speed: {FAN_MAX_W} W at {FAN_MAX_RPM} RPM, scaling with speed cubed"
                      if self.fans else f"Fixed estimate: {FANS_W} W")),
            "network": (net_desc, "Estimated from link state (up/down)"),
            "other": ("USB controllers and devices, audio", f"Fixed estimate: {OTHER_W} W"),
            "psu_loss": (f"Power supply (assumed {PSU_RATED_W} W 80+ Gold)",
                         "Estimated from load using a typical efficiency curve"),
        }
        return {"updated": dt.datetime.now().isoformat(timespec="seconds"),
                "components": [{"key": k, "label": LABELS[k], "hardware": info[k][0], "source": info[k][1]}
                               for k in COMPONENTS]}

    def gpu_measured(self):
        return bool(self.gpu_hwmon) or bool(run_cmd(["nvidia-smi", "-L"]).strip())

    def sample(self):
        e = self.read_cpu_energy() if self.rapl else 0
        t = time.monotonic()
        cpu_t = cpu_busy_ticks()
        disk = disk_busy_ms()
        elapsed = t - self.prev_t

        delta_e = e - self.prev_e if e >= self.prev_e else e + self.max_range - self.prev_e
        busy, total = cpu_t[0] - self.prev_cpu[0], cpu_t[1] - self.prev_cpu[1]
        cpu_util = busy / total if total > 0 else 0.0

        cpu_w = delta_e / 1e6 / elapsed if self.rapl else CPU_EST_W[0] + CPU_EST_W[1] * cpu_util
        w = {"cpu": cpu_w, "gpu": gpu_watts(self.gpu_hwmon)}
        lo, hi = RAM_W_PER_DIMM
        w["ram"] = len(self.dimms) * (lo + hi * cpu_util)
        w["ssd"] = 0.0
        for d in self.disks:
            util = min(1.0, max(0, disk.get(d["dev"], 0) - self.prev_disk.get(d["dev"], 0)) / (elapsed * 1000))
            w["ssd"] += d["watts"][0] + d["watts"][1] * util
        w["board"] = BOARD_W + VRM_LOSS * w["cpu"]
        if self.fans:
            self.fan_live = []
            for fan in self.fans:
                try:
                    rpm = read_int(fan["path"])
                except (OSError, ValueError):
                    rpm = 0
                self.fan_live.append({"label": fan["label"], "rpm": rpm, "w": round(fan_watts(rpm), 2)})
            w["fans"] = sum(f["w"] for f in self.fan_live)
        else:
            w["fans"] = FANS_W
        w["network"] = sum(NET_W[n["kind"]][read_text(f"/sys/class/net/{n['iface']}/operstate") == "up"]
                           for n in self.nics)
        w["other"] = OTHER_W
        dc = sum(w.values())
        w["psu_loss"] = dc / psu_efficiency(dc) - dc

        self.prev_e, self.prev_t, self.prev_cpu, self.prev_disk = e, t, cpu_t, disk
        return elapsed, w, self.attribute(w, elapsed, busy)

    def attribute(self, w, elapsed, busy_ticks):
        """Split the CPU and GPU power above their idle floor between the processes using them."""
        used, pid_names = self.procs.sample()
        self.cpu_hist.append(w["cpu"])
        self.gpu_hist.append(w["gpu"])
        cpu_extra = max(0.0, w["cpu"] - min(self.cpu_hist))
        gpu_extra = max(0.0, w["gpu"] - min(self.gpu_hist))
        out = {}  # name -> [cpu_pct, cpu_w, gpu_pct, gpu_w]
        denom = max(busy_ticks, sum(used.values()), 1)
        for name, ticks in used.items():
            out[name] = [ticks / (elapsed * self.procs.clk) * 100, cpu_extra * ticks / denom, 0.0, 0.0]
        if self.has_nvidia:
            by_name = {}
            for pid, u in gpu_process_util().items():
                n = pid_names.get(pid, f"pid {pid}")
                by_name[n] = by_name.get(n, 0.0) + u
            total_u = sum(by_name.values())
            if total_u > 0:
                for n, u in by_name.items():
                    e = out.setdefault(n, [0.0, 0.0, 0.0, 0.0])
                    e[2], e[3] = u, gpu_extra * u / total_u
            elif gpu_extra >= 1:
                out["GPU (unattributed)"] = [0.0, 0.0, 0.0, gpu_extra]
        top = sorted(out.items(), key=lambda kv: -(kv[1][1] + kv[1][3]))[:PROC_TOP]
        return [{"name": n, "cpu_pct": v[0], "cpu_w": v[1], "gpu_pct": v[2], "gpu_w": v[3], "w": v[1] + v[3]}
                for n, v in top if v[1] + v[3] >= PROC_MIN_W]


# ---------------------------------------------------------------- logging

def chown_like_dir(path):
    st = os.stat(LOG_DIR)
    try:
        os.chown(path, st.st_uid, st.st_gid)
    except PermissionError:
        pass


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        f.write(text)
    os.replace(tmp, path)
    chown_like_dir(path)


_summary_cache = {}  # path -> ((mtime, size), summary); past days never change, so they're parsed once


def cached_summary(path):
    st = os.stat(path)
    key = (st.st_mtime, st.st_size)
    hit = _summary_cache.get(path)
    if not hit or hit[0] != key:
        hit = _summary_cache[path] = (key, summarise_day(path))
    return hit[1]


def write_summary():
    lines = [",".join(SUMMARY_HEADER)]
    hourly = [",".join(HOURLY_HEADER)]
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "????-??-??.csv"))):
        s = cached_summary(path)
        if not s:
            continue
        lines.append(",".join([s["date"], f"{s['hours']:.2f}", f"{s['avg_w']:.1f}", f"{s['peak_w']:.1f}"]
                              + [f"{s['kwh'][c]:.4f}" for c in COMPONENTS] + [f"{s['total_kwh']:.4f}"]))
        for hour, (secs, joules, peak) in sorted(s["hourly"].items()):
            hourly.append(f"{s['date']},{hour},{secs / 60:.1f},{joules / secs:.1f},{peak:.1f},{joules / 3.6e6:.4f}")
    write_atomic(os.path.join(LOG_DIR, "daily_summary.csv"), "\n".join(lines) + "\n")
    write_atomic(os.path.join(LOG_DIR, "hourly.csv"), "\n".join(hourly) + "\n")


_config = {"mtime": None, "value": dict(CONFIG_DEFAULTS)}


def load_config():
    """config.json in LOG_DIR, re-read whenever the window saves it."""
    path = os.path.join(LOG_DIR, "config.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _config["value"]
    if mtime != _config["mtime"]:
        try:
            with open(path) as f:
                _config["value"] = {**CONFIG_DEFAULTS, **json.load(f)}
        except (OSError, ValueError):
            pass
        _config["mtime"] = mtime
    return _config["value"]


def notify(title, body, urgent=False):
    """Desktop notification for the person who owns LOG_DIR (the logger itself runs as root)."""
    uid = os.stat(LOG_DIR).st_uid
    cmd = ["notify-send", "-a", "Power Monitor", "-i", "dialog-warning" if urgent else "dialog-information",
           "-u", "critical" if urgent else "normal", title, body]
    if os.geteuid() == 0 and uid != 0:
        cmd = (["runuser", "-u", pwd.getpwuid(uid).pw_name, "--", "env",
                f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus"] + cmd)
    try:
        subprocess.run(cmd, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def fmt_duration(secs):
    secs = int(secs)
    h, m, s = secs // 3600, secs % 3600 // 60, secs % 60
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s" if m else f"{s}s"


class AlertTracker:
    """Raises an alert when total power stays at or above the threshold for alert_seconds,
    and logs it to alerts.csv (with the processes behind it) once power stays below for 2 samples."""

    def __init__(self):
        self.active_path = os.path.join(LOG_DIR, "alert_active.json")
        self.active = False
        self.reset()
        try:
            os.remove(self.active_path)  # left over from a restart mid-alert
        except OSError:
            pass

    def reset(self):
        self.start, self.secs, self.joules, self.peak, self.below = None, 0.0, 0.0, 0.0, 0
        self.proc_j = {}

    def add(self, interval, total, procs):
        self.secs += interval
        self.joules += total * interval
        self.peak = max(self.peak, total)
        for p in procs:
            self.proc_j[p["name"]] = self.proc_j.get(p["name"], 0.0) + p["w"] * interval

    def top(self, n=3):
        ranked = sorted(self.proc_j.items(), key=lambda kv: -kv[1])[:n]
        return [(name, j / self.secs) for name, j in ranked if self.secs and j / self.secs >= 0.5]

    def top_text(self, n=3):
        return "; ".join(f"{name} {w:.0f} W" for name, w in self.top(n)) or "no single process stood out"

    def update(self, now, interval, total, procs, cfg):
        threshold = float(cfg["alert_watts"])
        enabled = cfg.get("alert_enabled", True) and threshold > 0
        above = enabled and total >= threshold
        if not self.active:
            if not above:
                self.reset()
                return
            if self.start is None:
                self.start = now - dt.timedelta(seconds=interval)
            self.add(interval, total, procs)
            if self.secs >= float(cfg["alert_seconds"]):
                self.active = True
                if cfg.get("notify", True):
                    notify(f"Power alert: {total:.0f} W",
                           f"Above {threshold:.0f} W for {fmt_duration(self.secs)}.\nTop: {self.top_text()}", True)
                self.write_active(threshold, total)
            return
        self.add(interval, total, procs)
        self.below = 0 if above else self.below + 1
        if self.below >= 2 or not enabled:
            self.finish(now, threshold, cfg)
        else:
            self.write_active(threshold, total)

    def write_active(self, threshold, now_w):
        write_atomic(self.active_path, json.dumps({
            "since": self.start.isoformat(timespec="seconds"), "threshold_w": threshold, "now_w": round(now_w, 1),
            "peak_w": round(self.peak, 1), "avg_w": round(self.joules / self.secs, 1),
            "duration_s": round(self.secs), "top": [[n, round(w, 1)] for n, w in self.top(5)]}))

    def finish(self, now, threshold, cfg):
        path = os.path.join(LOG_DIR, "alerts.csv")
        new = not os.path.exists(path)
        avg = self.joules / self.secs
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(ALERT_HEADER)
            w.writerow([self.start.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
                        f"{self.secs:.0f}", f"{threshold:.0f}", f"{self.peak:.1f}", f"{avg:.1f}",
                        f"{self.joules / 3.6e6:.4f}", self.top_text(5)])
        if new:
            chown_like_dir(path)
        if cfg.get("notify", True):
            notify("Power alert ended",
                   f"{fmt_duration(self.secs)} at {avg:.0f} W average (peak {self.peak:.0f} W), "
                   f"{self.joules / 3600:.0f} Wh.\nTop: {self.top_text()}")
        try:
            os.remove(self.active_path)
        except OSError:
            pass
        self.active = False
        self.reset()


class DayFile:
    """Append-only CSV that rolls over to a new file each day."""

    def __init__(self, prefix, header, migrate=False):
        self.prefix, self.header, self.migrate = prefix, header, migrate
        self.day = self.f = self.writer = None

    def writerows(self, day, rows):
        if day != self.day:
            if self.f:
                self.f.close()
            path = os.path.join(LOG_DIR, f"{self.prefix}{day}.csv")
            new = not os.path.exists(path)
            if not new and self.migrate:
                migrate_day_file(path)
            self.f = open(path, "a", newline="")
            self.writer = csv.writer(self.f)
            if new:
                self.writer.writerow(self.header)
                chown_like_dir(path)
            self.day = day
        self.writer.writerows(rows)
        self.f.flush()


def migrate_day_file(path):
    """Rewrite, in place, a day file started by an older version so every row matches SAMPLE_HEADER.
    Columns the old version didn't log are left blank (read as 0)."""
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows or rows[0] == SAMPLE_HEADER:
        return
    old = rows[0]
    out = [SAMPLE_HEADER]
    for r in rows[1:]:
        if len(r) == len(SAMPLE_HEADER):
            out.append(r)
        elif len(r) == len(old):
            d = dict(zip(old, r))
            out.append([d.get(c, "") for c in SAMPLE_HEADER])
    buf = io.StringIO()
    csv.writer(buf).writerows(out)
    with open(path, "r+", newline="") as f:  # same inode, so an open appender keeps working
        f.write(buf.getvalue())
        f.truncate()


def run():
    os.makedirs(LOG_DIR, exist_ok=True)
    sampler = Sampler()
    write_atomic(os.path.join(LOG_DIR, "hardware.json"), json.dumps(sampler.hardware(), indent=2))
    if not os.path.exists(os.path.join(LOG_DIR, "config.json")):
        write_atomic(os.path.join(LOG_DIR, "config.json"), json.dumps(CONFIG_DEFAULTS, indent=2))
    write_summary()
    power_log = DayFile("", SAMPLE_HEADER, migrate=True)
    proc_log = DayFile("procs-", PROC_HEADER)
    alerts = AlertTracker()
    current_hour = dt.datetime.now().strftime("%Y-%m-%dT%H")

    while True:
        time.sleep(INTERVAL_S - (time.monotonic() - sampler.prev_t) % INTERVAL_S)
        elapsed, w, procs = sampler.sample()
        now = dt.datetime.now()
        day = now.date().isoformat()
        stamp = now.isoformat(timespec="seconds")
        total = sum(w.values())

        power_log.writerows(day, [[stamp, f"{elapsed:.2f}"] + [f"{w[c]:.2f}" for c in COMPONENTS] + [f"{total:.2f}"]])
        proc_log.writerows(day, [[stamp, f"{elapsed:.2f}", p["name"], f"{p['cpu_pct']:.1f}", f"{p['cpu_w']:.2f}",
                                  f"{p['gpu_pct']:.0f}", f"{p['gpu_w']:.2f}", f"{p['w']:.2f}"] for p in procs])
        alerts.update(now, elapsed, total, procs, load_config())
        if now.strftime("%Y-%m-%dT%H") != current_hour:
            current_hour = now.strftime("%Y-%m-%dT%H")
            write_summary()
        if sampler.fan_live:
            write_atomic(os.path.join(LOG_DIR, "fans.json"),
                         json.dumps({"updated": now.isoformat(timespec="seconds"), "fans": sampler.fan_live}))


# ---------------------------------------------------------------- reporting

def summarise_day(path):
    secs = tot_j = peak = 0.0
    comp_j = dict.fromkeys(COMPONENTS, 0.0)
    hourly = {}  # "HH" -> [secs, joules, peak]
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                i = float(r["interval_s"])
                t = float(r["est_total_w"])
            except (KeyError, ValueError, TypeError):
                continue
            secs += i
            tot_j += t * i
            peak = max(peak, t)
            h = hourly.setdefault(r["timestamp"][11:13], [0.0, 0.0, 0.0])
            h[0] += i
            h[1] += t * i
            h[2] = max(h[2], t)
            for c in COMPONENTS:
                try:
                    comp_j[c] += float(r.get(f"{c}_w") or 0) * i
                except ValueError:
                    pass
    if secs == 0:
        return None
    return {"date": os.path.basename(path)[:-4], "hours": secs / 3600, "avg_w": tot_j / secs, "peak_w": peak,
            "kwh": {c: j / 3.6e6 for c, j in comp_j.items()}, "total_kwh": tot_j / 3.6e6, "hourly": hourly}


def report(scope, price):
    today = dt.date.today()
    if scope == "today":
        pattern = f"{today.isoformat()}.csv"
    elif scope == "month":
        pattern = f"{today:%Y-%m}-??.csv"
    elif scope == "all":
        pattern = "????-??-??.csv"
    else:
        pattern = f"{scope}-??.csv" if len(scope) == 7 else f"{scope}.csv"

    days = [s for p in sorted(glob.glob(os.path.join(LOG_DIR, pattern))) if (s := summarise_day(p))]
    if not days:
        print(f"No data for '{scope}' in {LOG_DIR}")
        return 1

    cost = lambda k: f"  cost {k * price:9.2f}" if price else ""
    print(f"{'date':10}  {'hours':>5}  {'avg W':>6}  {'peak W':>6}  {'total kWh':>9}")
    for d in days:
        print(f"{d['date']:10}  {d['hours']:5.1f}  {d['avg_w']:6.1f}  {d['peak_w']:6.1f}  "
              f"{d['total_kwh']:9.3f}{cost(d['total_kwh'])}")

    total = sum(d["total_kwh"] for d in days)
    hours = sum(d["hours"] for d in days)
    avg_w = total * 1000 / hours
    try:
        with open(os.path.join(LOG_DIR, "hardware.json")) as f:
            sources = {c["key"]: c["source"] for c in json.load(f)["components"]}
    except (OSError, ValueError, KeyError):
        sources = {}
    print(f"\nBy component ({hours:.1f} h logged):")
    for c in COMPONENTS:
        k = sum(d["kwh"][c] for d in days)
        print(f"  {LABELS[c]:15} {k:8.3f} kWh  {k * 1000 / hours:6.1f} W avg  {k / total:5.1%}"
              f"  {'measured' if sources.get(c, '').startswith('Measured') else 'estimated'}")
    print(f"\nTotal: {total:.3f} kWh (avg {avg_w:.1f} W){cost(total)}")
    if scope == "month":
        dim = calendar.monthrange(today.year, today.month)[1]
        proj = avg_w * 24 * dim / 1000
        print(f"Projected for {today:%B} ({dim} days, 24/7 at {avg_w:.1f} W): {proj:.1f} kWh{cost(proj)}")

    dates = {d["date"] for d in days}
    proc_j, proc_peak = {}, {}
    for date in sorted(dates):
        try:
            with open(os.path.join(LOG_DIR, f"procs-{date}.csv"), newline="") as f:
                for r in csv.DictReader(f):
                    try:
                        w_, i = float(r["total_w"]), float(r["interval_s"])
                    except (KeyError, ValueError):
                        continue
                    proc_j[r["process"]] = proc_j.get(r["process"], 0.0) + w_ * i
                    proc_peak[r["process"]] = max(proc_peak.get(r["process"], 0.0), w_)
        except OSError:
            pass
    if proc_j:
        print("\nTop processes (power above idle they caused):")
        for name, j in sorted(proc_j.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {name[:28]:28} {j / 3.6e6:8.3f} kWh  {j / 3600 / hours:6.1f} W avg  peak {proc_peak[name]:6.1f} W")

    try:
        with open(os.path.join(LOG_DIR, "alerts.csv"), newline="") as f:
            hits = [r for r in csv.DictReader(f) if r["start"][:10] in dates]
    except OSError:
        hits = []
    if hits:
        print(f"\nAlerts ({len(hits)}):")
        for r in hits:
            print(f"  {r['start'].replace('T', ' ')}  {fmt_duration(float(r['duration_s'])):>8}  "
                  f"peak {float(r['peak_w']):5.0f} W  avg {float(r['avg_w']):5.0f} W  >= {r['threshold_w']} W  "
                  f"| {r['top_processes']}")
    return 0


def main():
    argv = sys.argv[1:]
    if os.path.basename(sys.argv[0]) == "power-report":
        argv = ["report"] + argv
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("summary")
    rp = sub.add_parser("report")
    rp.add_argument("scope", nargs="?", default="today", help="today | month | all | YYYY-MM | YYYY-MM-DD")
    rp.add_argument("--price", type=float, default=float(os.environ.get("POWER_PRICE", 0)),
                    help="price per kWh, for cost columns")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        run()
    elif a.cmd == "summary":
        write_summary()
    else:
        sys.exit(report(a.scope, a.price))


if __name__ == "__main__":
    main()
