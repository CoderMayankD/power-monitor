POWER MONITOR (Linux)
=====================

Tracks how much electricity your PC uses, all the time, and shows it in a small window.

INSTALL
  1. Extract this folder.
  2. Open a terminal in it and run:     sudo ./install.sh
  Needs: systemd, python3. The installer adds the GTK packages for the window if they're missing.

WHAT YOU GET
  - A background service (power-monitor.service) that takes a reading every 10 seconds,
    starts at boot and restarts itself if it ever stops.
  - "Power Monitor" window on your Desktop and app menu (also opens at login):
      current watts, units (kWh) used in the last N hours, cost, a breakdown for every
      component (CPU, GPU, RAM, SSDs, motherboard, fans, network, PSU loss), a chart and the raw log.
      Tabs: Breakdown, Monitor Now, Processes, Alerts, Analytics, Chart, Log.
  - Monitor Now: tick any components and/or programs, set a duration (h m s, 10 s minimum) and press
    Start. Live now/avg/peak watts and energy for each, a notification at the end, and the results are
    saved to ~/power-logs/monitor_sessions.csv.
  - Per-program power: every reading records which programs caused the power above idle (from their
    share of CPU time and GPU use). See the Processes tab or "power-report".
  - Alerts: when the total stays at or above a limit (default 250 W for 30 s) you get a desktop
    notification naming the programs responsible; each alert is logged with its duration, peak, energy
    and top programs. Change the limit in the Alerts tab.
  - Analytics: today / yesterday / 7-day / month tiles with projections and cost, energy per day by
    component, an hour-by-hour heatmap, a typical-day profile, where the energy went, top programs.
    Hover any bar, cell or tile for details.
  - CSV files in ~/power-logs/: one per day (all components), procs-<day>.csv (programs),
    daily_summary.csv, hourly.csv, alerts.csv, monitor_sessions.csv, plus hardware.json and config.json.
  - Terminal report:   power-report today | month | all | 2026-10   [--price 8]

HOW ACCURATE IS IT?
  Measured:  CPU (Intel/AMD RAPL sensor) and GPU (NVIDIA via nvidia-smi, AMD via driver sensor).
  Estimated: RAM, SSDs, motherboard, fans, network, USB and power-supply loss, from the detected
             hardware and its live activity. Software can't measure these directly.
  Fans:      no fan reports watts, so each fan's power is estimated from its live speed (RPM).
             The installer loads the motherboard's fan sensor driver if it can; if none is found
             it falls back to a fixed 5 W.
  For exact numbers at the wall socket, use a plug-in power meter.
  You can tune the estimates at the top of /opt/power-monitor/power_monitor.py
  (then run: sudo systemctl restart power-monitor).

UNINSTALL
  sudo /opt/power-monitor/uninstall.sh       (your logs are kept)
