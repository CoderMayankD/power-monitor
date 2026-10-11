# Power Monitor

Tracks how much electricity a PC uses, all the time, and shows it in a small desktop window:
current watts, units (kWh) used in the last N hours, cost, and a per-component breakdown
(CPU, GPU, RAM, SSDs, motherboard, fans, network, PSU loss) with a chart and the raw log.
Readings are taken every 10 seconds and saved as daily CSV files.

| Folder | Platform | Logger | Window |
|---|---|---|---|
| `linux/` | Linux with systemd | `power_monitor.py` as a root systemd service | GTK 3 (`power_monitor_ui.py`) |
| `windows/` | Windows 10/11 | `PowerMonitor.ps1` as a SYSTEM scheduled task | WinForms (`PowerMonitorUI.ps1`) |

## Install

- **Linux:** `cd linux && sudo ./install.sh`
- **Windows:** double-click `windows\Install.bat`

See `linux/README.txt` and `windows/README.txt` for details and uninstalling.

## Build shareable packages

```
./build.sh [output-dir]     # default: your Desktop
```

Creates `PowerMonitor-Linux.tar.gz` and `PowerMonitor-Windows.zip`. The Windows scripts get a UTF-8 BOM
and CRLF line endings, which Windows PowerShell 5.1 needs.

## Accuracy

- **Measured:** CPU (RAPL on Linux, Energy Meter counter on Windows when available) and GPU
  (nvidia-smi; AMD driver sensor on Linux).
- **Estimated:** RAM, SSDs, motherboard, fans (from fan RPM when a sensor driver is available),
  network, USB and power-supply loss, from the detected hardware and its live activity.
  The estimates can be tuned at the top of `power_monitor.py` / `PowerMonitor.ps1`.

For exact wall-socket numbers, use a plug-in power meter.
