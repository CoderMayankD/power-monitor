#!/bin/bash
# Build shareable packages onto the Desktop: PowerMonitor-Linux.tar.gz and PowerMonitor-Windows.zip
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/PowerMonitor-Linux" "$WORK/PowerMonitor-Windows"
cp "$SRC"/linux/{power_monitor.py,power_monitor_ui.py,install.sh,uninstall.sh,README.txt} "$WORK/PowerMonitor-Linux/"
chmod 755 "$WORK"/PowerMonitor-Linux/*.{sh,py}
tar -C "$WORK" -czf "$OUT/PowerMonitor-Linux.tar.gz" PowerMonitor-Linux

# Windows PowerShell 5.1 reads BOM-less scripts as ANSI, so add a UTF-8 BOM and CRLF line endings.
for f in "$SRC"/windows/*; do
    name=$(basename "$f"); dest="$WORK/PowerMonitor-Windows/$name"
    if [[ $name == *.ps1 ]]; then
        { printf '\xEF\xBB\xBF'; sed 's/\r$//; s/$/\r/' "$f"; } > "$dest"
    else
        sed 's/\r$//; s/$/\r/' "$f" > "$dest"
    fi
done
(cd "$WORK" && zip -qr "$OUT/PowerMonitor-Windows.zip" PowerMonitor-Windows)
ls -la "$OUT"/PowerMonitor-*
