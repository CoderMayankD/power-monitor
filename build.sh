#!/bin/bash
# Build shareable packages into dist/ (committed to the repo) and copy them to the Desktop:
# PowerMonitor-Linux.tar.gz and PowerMonitor-Windows.zip
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$SRC/dist}"
DESKTOP="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
mkdir -p "$OUT"
rm -f "$OUT/PowerMonitor-Linux.tar.gz" "$OUT/PowerMonitor-Windows.zip"
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
if [ -d "$DESKTOP" ] && [ "$(realpath "$OUT")" != "$(realpath "$DESKTOP")" ]; then
    cp "$OUT"/PowerMonitor-{Linux.tar.gz,Windows.zip} "$DESKTOP/"
fi
ls -la "$OUT"/PowerMonitor-*
