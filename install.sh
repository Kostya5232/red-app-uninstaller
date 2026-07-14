#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/red-app-uninstaller"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
        exec sudo -E "$0" "$@"
    fi
    echo "Запустите установщик с правами root." >&2
    exit 1
fi

echo "Проверка зависимостей…"
if ! python3 -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' >/dev/null 2>&1; then
    dnf -y install python3-gobject gtk3
fi

if ! command -v pkexec >/dev/null 2>&1; then
    dnf -y install polkit
fi

install -d -m 0755 "${APP_DIR}"
install -m 0755 "${SCRIPT_DIR}/red_uninstaller.py" "${APP_DIR}/red_uninstaller.py"
install -Dm0644 \
    "${SCRIPT_DIR}/red-app-uninstaller.desktop" \
    "/usr/share/applications/red-app-uninstaller.desktop"
install -Dm0644 \
    "${SCRIPT_DIR}/icons/red-app-uninstaller.svg" \
    "/usr/share/icons/hicolor/scalable/apps/red-app-uninstaller.svg"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f /usr/share/icons/hicolor || true
fi

echo
echo "Готово. Откройте «Удаление программ» в системном меню."
