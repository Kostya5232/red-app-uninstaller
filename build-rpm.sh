#!/usr/bin/env bash
set -euo pipefail

NAME="red-app-uninstaller"
VERSION="0.1.3"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TOPDIR="${HOME}/rpmbuild"
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

if ! command -v rpmbuild >/dev/null 2>&1; then
    echo "Не найден rpmbuild. Установите: sudo dnf install rpm-build" >&2
    exit 1
fi

mkdir -p "${TOPDIR}"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
mkdir -p "${STAGE}/${NAME}-${VERSION}"

cp "${SCRIPT_DIR}/red_uninstaller.py" "${STAGE}/${NAME}-${VERSION}/"
cp "${SCRIPT_DIR}/red-app-uninstaller.desktop" "${STAGE}/${NAME}-${VERSION}/"
cp "${SCRIPT_DIR}/README.md" "${STAGE}/${NAME}-${VERSION}/"
cp "${SCRIPT_DIR}/LICENSE" "${STAGE}/${NAME}-${VERSION}/"
mkdir -p "${STAGE}/${NAME}-${VERSION}/icons"
cp "${SCRIPT_DIR}/icons/red-app-uninstaller.svg" "${STAGE}/${NAME}-${VERSION}/icons/"

tar -C "${STAGE}" -czf \
    "${TOPDIR}/SOURCES/${NAME}-${VERSION}.tar.gz" \
    "${NAME}-${VERSION}"

cp "${SCRIPT_DIR}/packaging/red-app-uninstaller.spec" "${TOPDIR}/SPECS/"
rpmbuild -ba "${TOPDIR}/SPECS/red-app-uninstaller.spec"

echo
echo "Готово:"
find "${TOPDIR}/RPMS" -type f -name "${NAME}-*.rpm" -print
