# Сборка:
#   sudo dnf install rpm-build
#   ./build-rpm.sh
#
# Готовый RPM появится в ~/rpmbuild/RPMS/noarch/.

Name:           red-app-uninstaller
Version:        0.1.3
Release:        1%{?dist}
Summary:        Windows-like graphical application uninstaller for RED OS
License:        MIT
BuildArch:      noarch
Source0:        %{name}-%{version}.tar.gz

Requires:       python3
Requires:       python3-gobject
Requires:       gtk3
Requires:       rpm
Requires:       dnf
Requires:       polkit

%description
Shows installed desktop applications using localized .desktop names and icons,
maps them to RPM or Flatpak packages, previews DNF transactions, and removes
software through the native package managers.

%prep
%setup -q

%build

%install
install -Dm0755 red_uninstaller.py %{buildroot}%{_libexecdir}/%{name}/red_uninstaller.py
install -Dm0644 red-app-uninstaller.desktop %{buildroot}%{_datadir}/applications/red-app-uninstaller.desktop
install -Dm0644 icons/red-app-uninstaller.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/red-app-uninstaller.svg
sed -i "s|Exec=/opt/red-app-uninstaller/red_uninstaller.py|Exec=%{_libexecdir}/%{name}/red_uninstaller.py|" \
    %{buildroot}%{_datadir}/applications/red-app-uninstaller.desktop

%files
%license LICENSE
%doc README.md
%{_libexecdir}/%{name}/red_uninstaller.py
%{_datadir}/applications/red-app-uninstaller.desktop
%{_datadir}/icons/hicolor/scalable/apps/red-app-uninstaller.svg

%changelog
* Tue Jul 14 2026 RED App Uninstaller contributors <noreply@example.invalid> - 0.1.3-1
- Initial MVP
