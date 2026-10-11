#!/bin/bash
# Remove Power Monitor. Your logs in ~/power-logs are kept.
# Run:  sudo /opt/power-monitor/uninstall.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Please run with sudo."; exit 1; }
USER_HOME=$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)

systemctl disable --now power-monitor.service 2>/dev/null || true
rm -f /etc/systemd/system/power-monitor.service
systemctl daemon-reload
pkill -f power_monitor_ui.py 2>/dev/null || true
rm -f /usr/local/bin/power-monitor /usr/local/bin/power-report
DESKTOP_DIR=$(sudo -u "${SUDO_USER:-root}" xdg-user-dir DESKTOP 2>/dev/null || echo "$USER_HOME/Desktop")
rm -f "$USER_HOME/.local/share/applications/power-monitor-ui.desktop" \
      "$USER_HOME/.config/autostart/power-monitor-ui.desktop" \
      "$DESKTOP_DIR/power-monitor-ui.desktop"
rm -f /etc/modules-load.d/power-monitor.conf /etc/modprobe.d/power-monitor.conf
rm -rf /opt/power-monitor
echo "Power Monitor removed. Logs kept in $USER_HOME/power-logs"
