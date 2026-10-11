POWER MONITOR (Windows 10 / 11)
===============================

Tracks how much electricity your PC uses, all the time, and shows it in a small window.

INSTALL
  1. Extract (unzip) this folder.
  2. Double-click Install.bat and click "Yes" when Windows asks for administrator permission.
  If Windows SmartScreen warns about the file, click "More info" > "Run anyway".

WHAT YOU GET
  - A background logger (Task Scheduler task "PowerMonitor") that takes a reading every 10 seconds,
    starts at boot and restarts itself if it ever stops.
  - "Power Monitor" shortcut on the Desktop and Start menu (also opens at login):
      current watts, units (kWh) used in the last N hours, cost, a breakdown for every
      component (CPU, GPU, RAM, SSDs, motherboard, fans, network, PSU loss), a chart and the raw log.
  - Daily CSV files in C:\ProgramData\PowerMonitor\logs (one per day), plus daily_summary.csv
    and hardware.json. They open in Excel.

HOW ACCURATE IS IT?
  Measured:  CPU, if Windows exposes its "Energy Meter" counter (many Intel/AMD PCs do);
             otherwise it is estimated from CPU load. NVIDIA GPUs are measured with nvidia-smi.
  Estimated: RAM, SSDs, motherboard, fans, network, USB and power-supply loss, from the detected
             hardware and its live activity. Software can't measure these directly.
  The Breakdown tab marks every estimated part with "(est.)". Hover a row to see how it's worked out.
  For exact numbers at the wall socket, use a plug-in power meter.
  You can tune the estimates at the top of C:\Program Files\PowerMonitor\PowerMonitor.ps1.

UNINSTALL
  Double-click Uninstall.bat in C:\Program Files\PowerMonitor (your logs are kept).
