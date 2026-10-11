#!/bin/bash
# Install Power Monitor: a background logger (systemd service) plus a desktop window.
# Run from your normal user account:   sudo ./install.sh
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Please run with sudo:  sudo ./install.sh"; exit 1; }
USER_NAME="${SUDO_USER:-}"
if [ -z "$USER_NAME" ] || [ "$USER_NAME" = root ]; then
    echo "Run this with sudo from your normal user account (not as root directly)."; exit 1
fi
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
USER_GROUP=$(id -gn "$USER_NAME")
SRC="$(cd "$(dirname "$0")" && pwd)"
PREFIX=/opt/power-monitor
LOG_DIR="$USER_HOME/power-logs"

command -v python3 >/dev/null || { echo "python3 is required. Install it and run this again."; exit 1; }
command -v systemctl >/dev/null || { echo "This installer needs systemd."; exit 1; }

# The window needs GTK 3 bindings for Python.
if ! python3 -c 'import gi, cairo; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' 2>/dev/null; then
    echo "Installing GTK bindings for Python..."
    if command -v apt-get >/dev/null; then apt-get install -y python3-gi python3-cairo gir1.2-gtk-3.0
    elif command -v dnf >/dev/null; then dnf install -y python3-gobject python3-cairo gtk3
    elif command -v pacman >/dev/null; then pacman -S --needed --noconfirm python-gobject python-cairo gtk3
    elif command -v zypper >/dev/null; then zypper install -y python3-gobject python3-cairo typelib-1_0-Gtk-3_0
    else echo "WARNING: install PyGObject + pycairo + GTK 3 with your package manager, or the window won't open."
    fi
fi

echo "Installing to $PREFIX ..."
install -d "$PREFIX"
install -m 755 "$SRC/power_monitor.py" "$PREFIX/power_monitor.py"
install -m 755 "$SRC/power_monitor_ui.py" "$PREFIX/power_monitor_ui.py"
install -m 755 "$SRC/uninstall.sh" "$PREFIX/uninstall.sh"
ln -sf "$PREFIX/power_monitor.py" /usr/local/bin/power-monitor
ln -sf "$PREFIX/power_monitor.py" /usr/local/bin/power-report
install -d -o "$USER_NAME" -g "$USER_GROUP" -m 755 "$LOG_DIR"

# Fan speed sensors live on the motherboard's Super I/O chip. Many distros don't load its driver,
# so try the common ones (MSI: nct6683, ASUS/ASRock: nct6775, Gigabyte: it87) and keep one that shows fans.
board_fans() {
    local n=0 hw
    for hw in /sys/class/hwmon/hwmon*; do
        case "$(cat "$hw/name" 2>/dev/null)" in amdgpu|nouveau|radeon|i915|xe) continue ;; esac
        n=$((n + $(ls "$hw"/fan*_input 2>/dev/null | wc -l)))
    done
    echo "$n"
}
if [ "$(board_fans)" = 0 ]; then
    attempts=("nct6683" "nct6775" "it87")
    if grep -qi "micro-star" /sys/class/dmi/id/board_vendor 2>/dev/null; then
        attempts+=("nct6683 force=1")   # newer MSI boards aren't on the driver's list yet; reading is safe
    fi
    for attempt in "${attempts[@]}"; do
        mod=${attempt%% *}
        if grep -q "^$mod " /proc/modules; then continue; fi   # already loaded and shows no fans
        modprobe $attempt 2>/dev/null || continue
        if [ "$(board_fans)" -gt 0 ]; then
            echo "$mod" > /etc/modules-load.d/power-monitor.conf
            if [ "$attempt" != "$mod" ]; then echo "options $attempt" > /etc/modprobe.d/power-monitor.conf; fi
            echo "Fan sensors enabled ($attempt): $(board_fans) fan headers"
            break
        fi
        modprobe -r "$mod" 2>/dev/null || true
    done
    if [ "$(board_fans)" = 0 ]; then echo "No fan speed sensors found; fan power will be a fixed estimate."; fi
fi

cat > /etc/systemd/system/power-monitor.service <<EOF
[Unit]
Description=Power Monitor: log power consumption to daily CSV files
After=local-fs.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $PREFIX/power_monitor.py run
Environment=POWER_LOG_DIR=$LOG_DIR
Restart=always
RestartSec=5
Nice=10

[Install]
WantedBy=multi-user.target
EOF

DESKTOP_ENTRY="[Desktop Entry]
Type=Application
Name=Power Monitor
Comment=Current power draw and units consumed
Exec=/usr/bin/python3 $PREFIX/power_monitor_ui.py
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;"

as_user() { sudo -u "$USER_NAME" "$@"; }
DESKTOP_DIR=$(as_user xdg-user-dir DESKTOP 2>/dev/null || echo "$USER_HOME/Desktop")
for target in "$USER_HOME/.local/share/applications" "$USER_HOME/.config/autostart" "$DESKTOP_DIR"; do
    as_user mkdir -p "$target"
    printf '%s\n' "$DESKTOP_ENTRY" | as_user tee "$target/power-monitor-ui.desktop" >/dev/null
    as_user chmod 755 "$target/power-monitor-ui.desktop"
done
# GNOME needs desktop icons marked trusted; this needs the user's session, so it may silently fail.
USER_BUS="unix:path=/run/user/$(id -u "$USER_NAME")/bus"
as_user env DBUS_SESSION_BUS_ADDRESS="$USER_BUS" gio set "$DESKTOP_DIR/power-monitor-ui.desktop" metadata::trusted true 2>/dev/null || true

systemctl daemon-reload
systemctl enable power-monitor.service >/dev/null
systemctl restart power-monitor.service

echo "Waiting for the first readings..."
sleep 12
if systemctl is-active --quiet power-monitor.service; then
    echo
    echo "Power Monitor is running and starts automatically at every boot."
    echo "  Logs:    $LOG_DIR"
    echo "  Window:  'Power Monitor' on your Desktop / app menu (also opens at login)"
    echo "  Report:  power-report today | month | all   [--price PRICE_PER_KWH]"
    echo
    tail -n 2 "$LOG_DIR/$(date +%F).csv" 2>/dev/null || true
else
    echo "The service failed to start. Details:"
    journalctl -u power-monitor.service -n 20 --no-pager
    exit 1
fi
