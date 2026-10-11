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
  - Daily CSV files in ~/power-logs/ (one per day) plus daily_summary.csv and hardware.json.
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
