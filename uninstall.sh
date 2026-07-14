#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
        exec sudo -E "$0" "$@"
    fi
    echo "Запустите сценарий с правами root." >&2
    exit 1
fi

rm -rf /opt/red-app-uninstaller
rm -f /usr/share/applications/red-app-uninstaller.desktop
rm -f /usr/share/icons/hicolor/scalable/apps/red-app-uninstaller.svg

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f /usr/share/icons/hicolor || true
fi

echo "RED App Uninstaller удалён."
