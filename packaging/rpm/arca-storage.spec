Name:           arca-storage
Version:        0.1.0
Release:        1%{?dist}
Summary:        Arca Storage (CLI/API) with embedded venv
License:        Apache-2.0
URL:            https://github.com/akam1o/arca-storage
Source0:        %{name}.tar.gz
Source1:        %{name}-wheelhouse.tar.gz

# systemd-rpm-macros isn't always installed in minimal build containers.
%{!?_unitdir:%global _unitdir %{_prefix}/lib/systemd/system}

BuildRequires:  python3
BuildRequires:  python3-pip

Requires:       python3

%description
Software-defined storage control-plane with Pacemaker/DRBD/NFS-Ganesha integration.

%prep
%autosetup -n arca-storage

%build
:

%install
rm -rf %{buildroot}

# Embedded venv
install -d %{buildroot}/opt/arca-storage
python3 -m venv --copies %{buildroot}/opt/arca-storage/venv
tar -C %{_builddir}/%{name} -xzf %{SOURCE1}
%{buildroot}/opt/arca-storage/venv/bin/pip install --no-index --find-links %{_builddir}/%{name}/packaging/wheelhouse arca-storage

# RPM forbids references to the buildroot path in packaged files.
# venv-generated scripts embed the absolute venv path, so rewrite it to the
# final install prefix (/opt/arca-storage/venv).
LC_ALL=C grep -rlI "%{buildroot}" %{buildroot}/opt/arca-storage/venv | while read -r f; do
  sed -i "s|%{buildroot}||g" "$f"
done

# Wrappers
install -d %{buildroot}%{_bindir}
install -m 0755 packaging/wrappers/arca %{buildroot}%{_bindir}/arca
install -m 0755 packaging/wrappers/arca-storage-api %{buildroot}%{_bindir}/arca-storage-api

# Configs
install -d %{buildroot}%{_sysconfdir}/arca-storage
install -m 0644 arca_storage/arca_storage/resources/config/storage-bootstrap.conf %{buildroot}%{_sysconfdir}/arca-storage/storage-bootstrap.conf
install -m 0644 arca_storage/arca_storage/resources/config/storage-runtime.conf %{buildroot}%{_sysconfdir}/arca-storage/storage-runtime.conf

# systemd units
install -d %{buildroot}%{_unitdir}
install -m 0644 arca_storage/arca_storage/resources/systemd/arca-storage-api.service %{buildroot}%{_unitdir}/arca-storage-api.service
install -m 0644 arca_storage/arca_storage/resources/systemd/nfs-ganesha@.service %{buildroot}%{_unitdir}/nfs-ganesha@.service

# Pacemaker RA
install -d %{buildroot}%{_prefix}/lib/ocf/resource.d/local
install -m 0755 arca_storage/arca_storage/resources/pacemaker/NetnsVlan %{buildroot}%{_prefix}/lib/ocf/resource.d/local/NetnsVlan

%files
%license LICENSE
%{_bindir}/arca
%{_bindir}/arca-storage-api
/opt/arca-storage/venv
%config(noreplace) %{_sysconfdir}/arca-storage/storage-bootstrap.conf
%config(noreplace) %{_sysconfdir}/arca-storage/storage-runtime.conf
%{_unitdir}/arca-storage-api.service
%{_unitdir}/nfs-ganesha@.service
%{_prefix}/lib/ocf/resource.d/local/NetnsVlan

%changelog
* Wed Dec 31 2025 Arca Project <arca-project@ark-networks.net> - 0.1.0-1
- Initial package.
