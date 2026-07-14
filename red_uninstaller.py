#!/usr/bin/env python3
"""
RED App Uninstaller

A Windows-like graphical application uninstaller for RPM/DNF-based RED OS.
It presents applications using their .desktop names and icons, while safely
mapping each launcher back to its RPM or Flatpak package.
"""

from __future__ import annotations

import configparser
import dataclasses
import locale
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable, Optional

try:
    import gi

    # Require every GTK-related namespace before importing anything from
    # gi.repository. Otherwise PyGObject may load the newest available Gdk
    # (for example Gdk 4.0) before Gtk 3.0, causing a namespace conflict.
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("GdkPixbuf", "2.0")
    gi.require_version("Pango", "1.0")
    gi.require_version("Gio", "2.0")
    gi.require_version("GLib", "2.0")

    from gi.repository import Gtk
    from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Pango
except (ImportError, ValueError) as exc:
    print(
        "Не удалось загрузить GTK 3 / PyGObject.\n"
        "Установите зависимости:\n"
        "  sudo dnf install python3-gobject gtk3\n\n"
        f"Техническая информация: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(2)


APP_ID = "ru.redos.RedAppUninstaller"
APP_TITLE = "Удаление программ"
APP_VERSION = "0.1.3"

DESKTOP_DIRS = (
    Path.home() / ".local/share/applications",
    Path.home() / ".local/share/flatpak/exports/share/applications",
    Path("/usr/local/share/applications"),
    Path("/usr/share/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
)


def user_desktop_dirs() -> tuple[Path, ...]:
    """Return likely desktop folders without executing shell configuration."""
    home = Path.home()
    candidates: list[Path] = [
        home / "Desktop",
        home / "Рабочий стол",
    ]

    config_path = home / ".config/user-dirs.dirs"
    try:
        config_text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        config_text = ""

    match = re.search(
        r'^XDG_DESKTOP_DIR=(?:"([^"]*)"|\'([^\']*)\')',
        config_text,
        re.MULTILINE,
    )
    if match:
        raw = next((value for value in match.groups() if value is not None), "")
        raw = raw.replace("${HOME}", str(home)).replace("$HOME", str(home))
        if raw:
            candidates.insert(0, Path(raw).expanduser())

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        absolute = candidate.expanduser().absolute()
        key = str(absolute)
        if key not in seen:
            seen.add(key)
            unique.append(absolute)
    return tuple(unique)

PROTECTED_RPM_PACKAGES = {
    "basesystem",
    "bash",
    "coreutils",
    "dnf",
    "filesystem",
    "glibc",
    "kernel",
    "kernel-core",
    "kernel-modules",
    "polkit",
    "red-os-release",
    "redos-release",
    "rpm",
    "setup",
    "sudo",
    "systemd",
}

FIELD_CODE_RE = re.compile(r"(?<!%)%[fFuUdDnNickvm]")
MULTISPACE_RE = re.compile(r"\s+")


@dataclasses.dataclass(slots=True)
class DesktopEntry:
    path: Path
    name: str
    generic_name: str
    comment: str
    icon: str
    exec_line: str
    categories: tuple[str, ...]
    flatpak_id: str
    no_display: bool
    hidden: bool


@dataclasses.dataclass(slots=True)
class PackageInfo:
    backend: str
    package_id: str
    version: str
    size_bytes: int
    installed_at: int
    summary: str
    scope: str = "system"


@dataclasses.dataclass(slots=True)
class AppItem:
    backend: str
    package_id: str
    name: str
    generic_name: str
    comment: str
    icon: str
    version: str
    size_bytes: int
    installed_at: int
    summary: str
    scope: str
    aliases: tuple[str, ...]
    desktop_paths: tuple[Path, ...]
    categories: tuple[str, ...]
    shortcut_paths: tuple[Path, ...] = ()
    protected: bool = False

    @property
    def search_blob(self) -> str:
        return " ".join(
            [
                self.name,
                self.generic_name,
                self.comment,
                self.summary,
                self.package_id,
                self.version,
                " ".join(self.aliases),
                " ".join(self.categories),
                self.backend,
            ]
        ).casefold()


def run_command(
    args: list[str],
    *,
    timeout: int = 60,
    env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=merged_env,
        check=False,
    )


def current_locale_candidates() -> tuple[str, ...]:
    language = locale.getlocale()[0] or os.environ.get("LANG", "ru_RU").split(".")[0]
    values: list[str] = []
    for candidate in (language, language.split("_")[0], "ru_RU", "ru"):
        if candidate and candidate not in values:
            values.append(candidate)
    return tuple(values)


def localized_value(section: configparser.SectionProxy, key: str) -> str:
    for lang in current_locale_candidates():
        value = section.get(f"{key}[{lang}]", "").strip()
        if value:
            return value
    return section.get(key, "").strip()


def parse_bool(value: str) -> bool:
    return value.strip().casefold() in {"1", "true", "yes"}


def parse_desktop_file(path: Path) -> Optional[DesktopEntry]:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error):
        return None

    if not parser.has_section("Desktop Entry"):
        return None

    section = parser["Desktop Entry"]
    if section.get("Type", "Application").strip() != "Application":
        return None

    name = localized_value(section, "Name")
    if not name:
        return None

    return DesktopEntry(
        path=path,
        name=name,
        generic_name=localized_value(section, "GenericName"),
        comment=localized_value(section, "Comment"),
        icon=section.get("Icon", "").strip(),
        exec_line=section.get("Exec", "").strip(),
        categories=tuple(
            value for value in section.get("Categories", "").split(";") if value
        ),
        flatpak_id=section.get("X-Flatpak", "").strip(),
        no_display=parse_bool(section.get("NoDisplay", "false")),
        hidden=parse_bool(section.get("Hidden", "false")),
    )


def discover_desktop_entries(include_hidden: bool = False) -> list[DesktopEntry]:
    entries: list[DesktopEntry] = []
    seen: set[str] = set()

    for directory in DESKTOP_DIRS:
        if not directory.is_dir():
            continue
        try:
            paths = sorted(directory.glob("*.desktop"))
        except OSError:
            continue

        for path in paths:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            entry = parse_desktop_file(path)
            if not entry or entry.hidden:
                continue
            if entry.no_display and not include_hidden:
                continue
            entries.append(entry)

    return entries



def discover_user_shortcuts() -> list[DesktopEntry]:
    """Read application shortcuts placed directly on the user's desktop."""
    shortcuts: list[DesktopEntry] = []
    seen: set[str] = set()

    for directory in user_desktop_dirs():
        if not directory.is_dir():
            continue
        try:
            paths = sorted(directory.glob("*.desktop"))
        except OSError:
            continue

        for path in paths:
            key = str(path.absolute())
            if key in seen:
                continue
            seen.add(key)
            entry = parse_desktop_file(path)
            if entry and not entry.hidden:
                shortcuts.append(entry)

    return shortcuts


def remove_user_shortcuts(
    shortcut_paths: Iterable[Path],
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """
    Delete only known .desktop shortcut files located directly in the current
    user's desktop folders. Symlinks are unlinked, never followed for deletion.
    """
    allowed_parents = {str(path.absolute()) for path in user_desktop_dirs()}
    removed: list[Path] = []
    errors: list[str] = []

    for raw_path in shortcut_paths:
        path = raw_path.expanduser().absolute()
        if path.suffix.casefold() != ".desktop":
            errors.append(f"Пропущен файл с неподходящим расширением: {path}")
            continue
        if str(path.parent) not in allowed_parents:
            errors.append(f"Пропущен файл вне рабочего стола: {path}")
            continue

        try:
            path.unlink(missing_ok=True)
            removed.append(path)
        except OSError as exc:
            errors.append(f"{path}: {exc}")

    return tuple(removed), tuple(errors)


def parse_exec_executable(exec_line: str) -> Optional[Path]:
    if not exec_line:
        return None

    cleaned = FIELD_CODE_RE.sub("", exec_line).replace("%%", "%").strip()
    try:
        parts = shlex.split(cleaned)
    except ValueError:
        return None

    if not parts:
        return None

    # Skip common environment wrappers.
    if Path(parts[0]).name == "env":
        index = 1
        while index < len(parts) and "=" in parts[index] and not parts[index].startswith("/"):
            index += 1
        if index >= len(parts):
            return None
        command = parts[index]
    else:
        command = parts[0]

    if command in {"sh", "bash"} and len(parts) >= 3 and parts[1] in {"-c", "-lc"}:
        try:
            nested = shlex.split(parts[2])
        except ValueError:
            nested = []
        if nested:
            command = nested[0]

    if os.path.isabs(command):
        return Path(command)

    resolved = shutil.which(command)
    return Path(resolved) if resolved else None


def rpm_info_for_file(path: Path) -> Optional[PackageInfo]:
    if not shutil.which("rpm"):
        return None

    fmt = r"%{NAME}\t%{VERSION}-%{RELEASE}\t%{SIZE}\t%{INSTALLTIME}\t%{SUMMARY}\n"
    result = run_command(["rpm", "-qf", "--qf", fmt, str(path)], timeout=20)
    if result.returncode != 0:
        return None

    line = result.stdout.strip().splitlines()
    if not line:
        return None

    fields = line[-1].split("\t", 4)
    if len(fields) != 5:
        return None

    name, version, size, installed_at, summary = fields
    try:
        size_bytes = int(size)
    except ValueError:
        size_bytes = 0
    try:
        install_timestamp = int(installed_at)
    except ValueError:
        install_timestamp = 0

    return PackageInfo(
        backend="rpm",
        package_id=name,
        version=version,
        size_bytes=size_bytes,
        installed_at=install_timestamp,
        summary=summary,
        scope="system",
    )


def resolve_rpm_entry(entry: DesktopEntry) -> Optional[PackageInfo]:
    package = rpm_info_for_file(entry.path)
    if package:
        return package

    executable = parse_exec_executable(entry.exec_line)
    if executable and executable.exists():
        return rpm_info_for_file(executable)
    return None


def flatpak_inventory() -> dict[str, PackageInfo]:
    if not shutil.which("flatpak"):
        return {}

    commands = [
        [
            "flatpak",
            "list",
            "--app",
            "--columns=application,version,installation,size,description",
        ],
        ["flatpak", "list", "--app", "--columns=application,version,installation,size"],
        ["flatpak", "list", "--app", "--columns=application,version,installation"],
    ]

    result: Optional[subprocess.CompletedProcess[str]] = None
    for command in commands:
        candidate = run_command(command, timeout=45)
        if candidate.returncode == 0:
            result = candidate
            break

    if result is None:
        return {}

    inventory: dict[str, PackageInfo] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        app_id = fields[0].strip()
        if not app_id:
            continue
        version = fields[1].strip()
        scope = fields[2].strip() or "system"
        size = parse_flatpak_size(fields[3]) if len(fields) > 3 else 0
        summary = fields[4].strip() if len(fields) > 4 else ""
        inventory[app_id] = PackageInfo(
            backend="flatpak",
            package_id=app_id,
            version=version,
            size_bytes=size,
            installed_at=0,
            summary=summary,
            scope=scope,
        )
    return inventory


def parse_flatpak_size(value: str) -> int:
    value = value.strip().replace(",", ".")
    match = re.match(r"^([\d.]+)\s*([KMGT]?B|[KMGT]?iB)?$", value, re.IGNORECASE)
    if not match:
        return 0
    number = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    factors = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "KIB": 1024,
        "MIB": 1024**2,
        "GIB": 1024**3,
        "TIB": 1024**4,
    }
    return int(number * factors.get(unit, 1))


def flatpak_id_for_entry(entry: DesktopEntry, inventory: dict[str, PackageInfo]) -> str:
    if entry.flatpak_id and entry.flatpak_id in inventory:
        return entry.flatpak_id

    if "flatpak/exports/share/applications" in str(entry.path):
        stem = entry.path.stem
        if stem in inventory:
            return stem
        # Some exported desktop IDs append suffixes.
        matches = [app_id for app_id in inventory if stem.startswith(app_id)]
        if matches:
            return max(matches, key=len)

    return ""


def choose_primary(entries: list[DesktopEntry]) -> DesktopEntry:
    def score(entry: DesktopEntry) -> tuple[int, int, int, str]:
        categories = {item.casefold() for item in entry.categories}
        utility_penalty = int(bool(categories & {"settings", "system", "utility"}))
        generic_bonus = int(bool(entry.generic_name))
        comment_bonus = int(bool(entry.comment))
        return (utility_penalty, -generic_bonus, -comment_bonus, entry.name.casefold())

    return min(entries, key=score)


def build_catalog(include_hidden: bool = False) -> list[AppItem]:
    entries = discover_desktop_entries(include_hidden=include_hidden)
    flatpaks = flatpak_inventory()

    resolved: list[tuple[DesktopEntry, PackageInfo]] = []
    unresolved_rpm: list[DesktopEntry] = []

    for entry in entries:
        flatpak_id = flatpak_id_for_entry(entry, flatpaks)
        if flatpak_id:
            resolved.append((entry, flatpaks[flatpak_id]))
        else:
            unresolved_rpm.append(entry)

    max_workers = min(8, max(2, (os.cpu_count() or 2)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(resolve_rpm_entry, entry): entry for entry in unresolved_rpm
        }
        for future in as_completed(future_map):
            entry = future_map[future]
            try:
                package = future.result()
            except Exception:
                package = None
            if package:
                resolved.append((entry, package))

    groups: dict[tuple[str, str], list[tuple[DesktopEntry, PackageInfo]]] = {}
    for entry, package in resolved:
        groups.setdefault((package.backend, package.package_id), []).append((entry, package))

    items: list[AppItem] = []
    for (backend, package_id), members in groups.items():
        desktop_entries = [entry for entry, _ in members]
        package = members[0][1]
        primary = choose_primary(desktop_entries)
        aliases = tuple(
            sorted(
                {entry.name for entry in desktop_entries if entry.name != primary.name},
                key=str.casefold,
            )
        )
        categories = tuple(
            sorted({cat for entry in desktop_entries for cat in entry.categories})
        )
        item = AppItem(
            backend=backend,
            package_id=package_id,
            name=primary.name,
            generic_name=primary.generic_name,
            comment=primary.comment,
            icon=primary.icon,
            version=package.version,
            size_bytes=package.size_bytes,
            installed_at=package.installed_at,
            summary=package.summary,
            scope=package.scope,
            aliases=aliases,
            desktop_paths=tuple(entry.path for entry in desktop_entries),
            categories=categories,
            protected=backend == "rpm" and package_id in PROTECTED_RPM_PACKAGES,
        )
        items.append(item)

    items_by_package = {
        (item.backend, item.package_id): item
        for item in items
    }
    shortcuts_by_package: dict[tuple[str, str], list[Path]] = {}

    for shortcut in discover_user_shortcuts():
        package: Optional[PackageInfo] = None
        flatpak_id = flatpak_id_for_entry(shortcut, flatpaks)
        if flatpak_id:
            package = flatpaks[flatpak_id]
        else:
            package = resolve_rpm_entry(shortcut)

        if package and (package.backend, package.package_id) in items_by_package:
            shortcuts_by_package.setdefault(
                (package.backend, package.package_id), []
            ).append(shortcut.path)
            continue

        # Safe fallback for copied/symlinked launchers: use an identical
        # .desktop filename only when it belongs to a known application.
        for item in items:
            source_names = {path.name for path in item.desktop_paths}
            if shortcut.path.name in source_names:
                shortcuts_by_package.setdefault(
                    (item.backend, item.package_id), []
                ).append(shortcut.path)
                break

    for key, paths in shortcuts_by_package.items():
        item = items_by_package[key]
        item.shortcut_paths = tuple(
            sorted({path.absolute() for path in paths}, key=lambda path: str(path))
        )

    return sorted(items, key=lambda item: item.name.casefold())


def human_size(size: int) -> str:
    if size <= 0:
        return "размер неизвестен"
    value = float(size)
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "Б":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}".replace(".0 ", " ")
        value /= 1024
    return f"{size} Б"


def human_date(timestamp: int) -> str:
    if timestamp <= 0:
        return "неизвестно"
    try:
        return time.strftime("%d.%m.%Y", time.localtime(timestamp))
    except (OverflowError, OSError, ValueError):
        return "неизвестно"


def escape_markup(value: str) -> str:
    return GLib.markup_escape_text(value or "")


def load_icon(icon_name: str, size: int = 48) -> GdkPixbuf.Pixbuf:
    theme = Gtk.IconTheme.get_default()
    fallback = "application-x-executable"

    try:
        if icon_name:
            path = Path(icon_name)
            if path.is_absolute() and path.exists():
                return GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(path), size, size, True
                )
            return theme.load_icon(icon_name, size, Gtk.IconLookupFlags.FORCE_SIZE)
    except GLib.Error:
        pass

    try:
        return theme.load_icon(fallback, size, Gtk.IconLookupFlags.FORCE_SIZE)
    except GLib.Error:
        # Last-resort transparent pixbuf, so a broken icon never breaks the app.
        pixbuf = GdkPixbuf.Pixbuf.new(
            GdkPixbuf.Colorspace.RGB, True, 8, size, size
        )
        pixbuf.fill(0x00000000)
        return pixbuf


class AppRow(Gtk.ListBoxRow):
    def __init__(
        self,
        item: AppItem,
        on_details: Callable[[AppItem], None],
        on_remove: Callable[[AppItem, "AppRow"], None],
    ) -> None:
        super().__init__()
        self.item = item

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        outer.set_border_width(10)
        self.add(outer)

        image = Gtk.Image.new_from_pixbuf(load_icon(item.icon, 48))
        image.set_valign(Gtk.Align.START)
        outer.pack_start(image, False, False, 0)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        text_box.set_hexpand(True)
        outer.pack_start(text_box, True, True, 0)

        name_label = Gtk.Label()
        name_label.set_xalign(0)
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        name_label.set_markup(f"<b>{escape_markup(item.name)}</b>")
        text_box.pack_start(name_label, False, False, 0)

        subtitle_parts = []
        if item.generic_name and item.generic_name.casefold() != item.name.casefold():
            subtitle_parts.append(item.generic_name)
        subtitle_parts.append(
            f"{'RPM' if item.backend == 'rpm' else 'Flatpak'} · {item.version or 'версия неизвестна'}"
        )
        subtitle = Gtk.Label(label=" — ".join(subtitle_parts))
        subtitle.set_xalign(0)
        subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        subtitle.get_style_context().add_class("dim-label")
        text_box.pack_start(subtitle, False, False, 0)

        description_text = item.comment or item.summary
        if item.aliases:
            alias_preview = ", ".join(item.aliases[:3])
            if len(item.aliases) > 3:
                alias_preview += f" и ещё {len(item.aliases) - 3}"
            description_text = (
                f"{description_text} · Ярлыки: {alias_preview}"
                if description_text
                else f"Ярлыки: {alias_preview}"
            )

        if description_text:
            description = Gtk.Label(label=description_text)
            description.set_xalign(0)
            description.set_ellipsize(Pango.EllipsizeMode.END)
            description.set_max_width_chars(80)
            description.get_style_context().add_class("dim-label")
            text_box.pack_start(description, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_valign(Gtk.Align.CENTER)
        outer.pack_end(actions, False, False, 0)

        details_button = Gtk.Button(label="Сведения")
        details_button.connect("clicked", lambda _button: on_details(item))
        actions.pack_start(details_button, False, False, 0)

        self.remove_button = Gtk.Button(label="Удалить")
        self.remove_button.get_style_context().add_class("destructive-action")
        self.remove_button.set_sensitive(not item.protected)
        if item.protected:
            self.remove_button.set_tooltip_text(
                "Удаление этого системного пакета заблокировано для защиты системы"
            )
        self.remove_button.connect(
            "clicked", lambda _button: on_remove(item, self)
        )
        actions.pack_start(self.remove_button, False, False, 0)


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(application=application, title=APP_TITLE)
        self.set_default_size(920, 650)
        self.set_size_request(720, 480)

        self.catalog: list[AppItem] = []
        self.rows: list[AppRow] = []
        self.scan_generation = 0

        self._build_ui()
        self._apply_css()
        self.refresh_catalog()

    def _build_ui(self) -> None:
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.props.title = APP_TITLE
        header.props.subtitle = "Приложения с настоящими названиями и значками"
        self.set_titlebar(header)

        refresh_button = Gtk.Button.new_from_icon_name(
            "view-refresh-symbolic", Gtk.IconSize.BUTTON
        )
        refresh_button.set_tooltip_text("Обновить список")
        refresh_button.connect("clicked", lambda _button: self.refresh_catalog())
        header.pack_end(refresh_button)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(main_box)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        toolbar.set_border_width(12)
        main_box.pack_start(toolbar, False, False, 0)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Поиск по названию программы…")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("search-changed", self._on_filter_changed)
        toolbar.pack_start(self.search_entry, True, True, 0)

        self.backend_combo = Gtk.ComboBoxText()
        self.backend_combo.append("all", "Все источники")
        self.backend_combo.append("rpm", "RPM / DNF")
        self.backend_combo.append("flatpak", "Flatpak")
        self.backend_combo.set_active_id("all")
        self.backend_combo.connect("changed", self._on_filter_changed)
        toolbar.pack_start(self.backend_combo, False, False, 0)

        self.hidden_switch = Gtk.Switch()
        self.hidden_switch.set_tooltip_text("Показывать служебные ярлыки NoDisplay")
        self.hidden_switch.connect("notify::active", self._on_hidden_toggled)
        hidden_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hidden_label = Gtk.Label(label="Служебные")
        hidden_box.pack_start(hidden_label, False, False, 0)
        hidden_box.pack_start(self.hidden_switch, False, False, 0)
        toolbar.pack_start(hidden_box, False, False, 0)

        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        main_box.pack_start(separator, False, False, 0)

        overlay = Gtk.Overlay()
        main_box.pack_start(overlay, True, True, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        overlay.add(scrolled)

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.set_header_func(self._update_row_header)
        scrolled.add(self.list_box)

        self.status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.status_box.set_halign(Gtk.Align.CENTER)
        self.status_box.set_valign(Gtk.Align.CENTER)
        overlay.add_overlay(self.status_box)

        self.spinner = Gtk.Spinner()
        self.status_box.pack_start(self.spinner, False, False, 0)

        self.status_label = Gtk.Label(label="Чтение списка приложений…")
        self.status_box.pack_start(self.status_label, False, False, 0)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_border_width(9)
        main_box.pack_end(footer, False, False, 0)

        self.count_label = Gtk.Label(label="")
        self.count_label.set_xalign(0)
        self.count_label.get_style_context().add_class("dim-label")
        footer.pack_start(self.count_label, True, True, 0)

        note = Gtk.Label(label="Удаление выполняется штатным DNF или Flatpak")
        note.set_xalign(1)
        note.get_style_context().add_class("dim-label")
        footer.pack_end(note, False, False, 0)

    def _apply_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(
            b"""
            list row { padding: 2px; }
            list row:hover { background: alpha(@theme_selected_bg_color, 0.08); }
            .dim-label { opacity: 0.72; }
            """
        )
        screen = Gdk.Screen.get_default()
        if screen:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    @staticmethod
    def _update_row_header(row: Gtk.ListBoxRow, before: Optional[Gtk.ListBoxRow]) -> None:
        if before is None:
            row.set_header(None)
        elif row.get_header() is None:
            row.set_header(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

    def _set_loading(self, loading: bool, message: str = "") -> None:
        self.status_box.set_visible(loading or not self.rows)
        if loading:
            self.spinner.start()
            self.status_label.set_text(message or "Чтение списка приложений…")
        else:
            self.spinner.stop()
            if not self.rows:
                self.status_label.set_text(
                    message or "Подходящие установленные приложения не найдены"
                )
            else:
                self.status_box.hide()

    def refresh_catalog(self) -> None:
        self.scan_generation += 1
        generation = self.scan_generation
        self._set_loading(True, "Сопоставление ярлыков с установленными пакетами…")

        include_hidden = self.hidden_switch.get_active()

        def worker() -> None:
            try:
                catalog = build_catalog(include_hidden=include_hidden)
                error = ""
            except Exception as exc:
                catalog = []
                error = str(exc)
            GLib.idle_add(self._catalog_ready, generation, catalog, error)

        threading.Thread(target=worker, daemon=True).start()

    def _catalog_ready(
        self, generation: int, catalog: list[AppItem], error: str
    ) -> bool:
        if generation != self.scan_generation:
            return False

        if error:
            self._show_error("Не удалось прочитать список приложений", error)

        self.catalog = catalog
        self._rebuild_rows()
        return False

    def _clear_rows(self) -> None:
        for child in self.list_box.get_children():
            self.list_box.remove(child)
        self.rows.clear()

    def _rebuild_rows(self) -> None:
        self._clear_rows()
        query = MULTISPACE_RE.sub(" ", self.search_entry.get_text()).strip().casefold()
        backend = self.backend_combo.get_active_id() or "all"

        visible_items = [
            item
            for item in self.catalog
            if (backend == "all" or item.backend == backend)
            and (not query or query in item.search_blob)
        ]

        for item in visible_items:
            row = AppRow(item, self.show_details, self.request_remove)
            self.list_box.add(row)
            self.rows.append(row)

        self.list_box.show_all()
        self.count_label.set_text(
            f"Показано: {len(visible_items)} · найдено приложений: {len(self.catalog)}"
        )
        self._set_loading(False)

    def _on_filter_changed(self, *_args: object) -> None:
        if self.catalog:
            self._rebuild_rows()

    def _on_hidden_toggled(self, *_args: object) -> None:
        self.refresh_catalog()

    def show_details(self, item: AppItem) -> None:
        backend = "RPM / DNF" if item.backend == "rpm" else "Flatpak"
        aliases = "\n".join(f"• {alias}" for alias in item.aliases) or "нет"
        launchers = "\n".join(str(path) for path in item.desktop_paths)
        desktop_shortcuts = (
            "\n".join(str(path) for path in item.shortcut_paths)
            if item.shortcut_paths
            else "не найдены"
        )
        body = (
            f"<b>{escape_markup(item.name)}</b>\n\n"
            f"<b>Пакет:</b> {escape_markup(item.package_id)}\n"
            f"<b>Источник:</b> {backend}"
            + (f" ({escape_markup(item.scope)})" if item.backend == "flatpak" else "")
            + f"\n<b>Версия:</b> {escape_markup(item.version or 'неизвестно')}\n"
            f"<b>Размер пакета:</b> {escape_markup(human_size(item.size_bytes))}\n"
            f"<b>Установлено:</b> {escape_markup(human_date(item.installed_at))}\n\n"
            f"<b>Описание:</b>\n{escape_markup(item.comment or item.summary or 'нет описания')}\n\n"
            f"<b>Другие ярлыки из этого пакета:</b>\n{escape_markup(aliases)}\n\n"
            f"<b>Системные файлы ярлыков:</b>\n{escape_markup(launchers)}\n\n"
            f"<b>Ярлыки на рабочем столе:</b>\n{escape_markup(desktop_shortcuts)}"
        )

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.CLOSE,
            text=item.name,
        )
        dialog.format_secondary_markup(body)
        dialog.run()
        dialog.destroy()

    def request_remove(self, item: AppItem, row: AppRow) -> None:
        if item.protected:
            self._show_error(
                "Удаление заблокировано",
                f"Пакет {item.package_id} относится к критическим системным компонентам.",
            )
            return

        row.remove_button.set_sensitive(False)
        row.remove_button.set_label("Проверка…")

        def worker() -> None:
            preview = self._build_removal_preview(item)
            GLib.idle_add(self._preview_ready, item, row, preview)

        threading.Thread(target=worker, daemon=True).start()

    def _build_removal_preview(self, item: AppItem) -> str:
        if item.backend == "rpm":
            if not shutil.which("dnf"):
                return "ОШИБКА: команда dnf не найдена."
            result = run_command(
                [
                    "dnf",
                    "--color=never",
                    "remove",
                    "--assumeno",
                    item.package_id,
                ],
                timeout=180,
            )
            output = "\n".join(
                part.strip() for part in (result.stdout, result.stderr) if part.strip()
            )
            return output or "DNF не вернул сведения о транзакции."

        if item.backend == "flatpak":
            result = run_command(["flatpak", "info", item.package_id], timeout=60)
            output = "\n".join(
                part.strip() for part in (result.stdout, result.stderr) if part.strip()
            )
            return output or f"Будет удалено Flatpak-приложение {item.package_id}."

        return "Неизвестный способ установки."

    def _preview_ready(self, item: AppItem, row: AppRow, preview: str) -> bool:
        row.remove_button.set_label("Удалить")
        row.remove_button.set_sensitive(True)

        if preview.startswith("ОШИБКА:"):
            self._show_error("Невозможно подготовить удаление", preview)
            return False

        dialog = Gtk.Dialog(
            title=f"Удалить «{item.name}»?",
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
        )
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        remove_button = dialog.add_button("Удалить", Gtk.ResponseType.OK)
        remove_button.get_style_context().add_class("destructive-action")
        dialog.set_default_size(760, 520)

        content = dialog.get_content_area()
        content.set_spacing(10)
        content.set_border_width(14)

        title = Gtk.Label()
        title.set_xalign(0)
        title.set_markup(
            f"<big><b>Будет удалено: {escape_markup(item.name)}</b></big>\n"
            f"Пакет: <tt>{escape_markup(item.package_id)}</tt>"
        )
        content.pack_start(title, False, False, 0)

        warning = Gtk.Label(
            label=(
                "Проверьте расчёт операции ниже. Вместе с приложением DNF может "
                "удалить зависимые пакеты. Для продолжения потребуется пароль администратора."
                if item.backend == "rpm"
                else "Будет удалено приложение Flatpak и принадлежащие ему данные пакета."
            )
        )
        warning.set_xalign(0)
        warning.set_line_wrap(True)
        content.pack_start(warning, False, False, 0)

        shortcut_check = Gtk.CheckButton()
        if item.shortcut_paths:
            shortcut_check.set_label(
                f"Удалить ярлыки с рабочего стола ({len(item.shortcut_paths)})"
            )
            shortcut_check.set_active(True)
            shortcut_check.set_tooltip_text(
                "\n".join(str(path) for path in item.shortcut_paths)
            )
        else:
            shortcut_check.set_label("Ярлыки этой программы на рабочем столе не найдены")
            shortcut_check.set_sensitive(False)
        content.pack_start(shortcut_check, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        content.pack_start(scrolled, True, True, 0)

        text_view = Gtk.TextView()
        text_view.set_editable(False)
        text_view.set_cursor_visible(False)
        text_view.set_monospace(True)
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_view.get_buffer().set_text(preview)
        scrolled.add(text_view)

        dialog.show_all()
        response = dialog.run()
        dialog.destroy()

        remove_shortcuts = shortcut_check.get_active()
        if response == Gtk.ResponseType.OK:
            self._perform_remove(item, row, remove_shortcuts)

        return False

    def _perform_remove(
        self,
        item: AppItem,
        row: AppRow,
        remove_shortcuts: bool,
    ) -> None:
        row.remove_button.set_sensitive(False)
        row.remove_button.set_label("Удаление…")

        def worker() -> None:
            if item.backend == "rpm":
                if not shutil.which("pkexec"):
                    result = subprocess.CompletedProcess(
                        args=[],
                        returncode=127,
                        stdout="",
                        stderr="Команда pkexec не найдена. Установите пакет polkit.",
                    )
                else:
                    result = run_command(
                        ["pkexec", "dnf", "-y", "remove", item.package_id],
                        timeout=1800,
                    )
            else:
                command = ["flatpak", "uninstall", "--assumeyes", item.package_id]
                if item.scope == "system":
                    command.insert(2, "--system")
                elif item.scope == "user":
                    command.insert(2, "--user")
                result = run_command(command, timeout=1800)

            removed_shortcuts: tuple[Path, ...] = ()
            shortcut_errors: tuple[str, ...] = ()
            if result.returncode == 0 and remove_shortcuts:
                removed_shortcuts, shortcut_errors = remove_user_shortcuts(
                    item.shortcut_paths
                )

            GLib.idle_add(
                self._remove_finished,
                item,
                row,
                result,
                removed_shortcuts,
                shortcut_errors,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _remove_finished(
        self,
        item: AppItem,
        row: AppRow,
        result: subprocess.CompletedProcess[str],
        removed_shortcuts: tuple[Path, ...],
        shortcut_errors: tuple[str, ...],
    ) -> bool:
        if result.returncode == 0:
            dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text=f"«{item.name}» удалено",
            )
            message_parts = [
                f"Пакет {item.package_id} успешно удалён."
            ]
            if removed_shortcuts:
                message_parts.append(
                    f"Удалено ярлыков с рабочего стола: {len(removed_shortcuts)}."
                )
            if shortcut_errors:
                message_parts.append(
                    "Не удалось удалить некоторые ярлыки:\n"
                    + "\n".join(shortcut_errors)
                )
            message_parts.append("Список приложений будет обновлён.")
            dialog.format_secondary_text("\n\n".join(message_parts))
            dialog.run()
            dialog.destroy()
            self.refresh_catalog()
            return False

        row.remove_button.set_label("Удалить")
        row.remove_button.set_sensitive(True)
        details = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        if result.returncode in {126, 127} and not details:
            details = "Авторизация была отменена или системная команда недоступна."
        self._show_error(
            f"Не удалось удалить «{item.name}»",
            details or f"Команда завершилась с кодом {result.returncode}.",
        )
        return False

    def _show_error(self, title: str, details: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(details[:6000])
        dialog.run()
        dialog.destroy()


class RedUninstallerApplication(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = MainWindow(self)
            # GTK 3 widgets are hidden by default. Without show_all(), only the
            # empty top-level window is painted, which looks like a white screen.
            window.show_all()
        window.present()


def self_test() -> int:
    sample = """[Desktop Entry]
Type=Application
Name=Example
Name[ru]=Пример
Comment=Test application
Icon=example
Exec=env FOO=bar /usr/bin/example %U
Categories=Utility;
"""
    temp = Path("/tmp/red-app-uninstaller-self-test.desktop")
    temp.write_text(sample, encoding="utf-8")
    try:
        entry = parse_desktop_file(temp)
        assert entry is not None
        assert entry.name in {"Пример", "Example"}
        assert entry.icon == "example"
        executable = parse_exec_executable(entry.exec_line)
        assert executable == Path("/usr/bin/example")
        assert human_size(1024 * 1024) == "1 МБ"
        print("Self-test: OK")
        return 0
    finally:
        temp.unlink(missing_ok=True)


def main() -> int:
    if "--version" in sys.argv:
        print(APP_VERSION)
        return 0
    if "--self-test" in sys.argv:
        return self_test()

    app = RedUninstallerApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
