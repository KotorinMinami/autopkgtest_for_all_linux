%global debug_package %{nil}

Name:           autopkgtest
Version:        5.30
Release:        1
Summary:        automatic as-installed testing for Debian packages
License:        GPLv2
URL:            https://salsa.debian.org/ci-team/autopkgtest
Source0:        https://salsa.debian.org/ci-team/autopkgtest/-/archive/debian/%{version}/autopkgtest-debian-%{version}.tar.gz

BuildRequires:  python3-debian make python3-docutils
Requires:       fakeroot procps-ng python3 python3-pycodestyle python3-pyflakes python3-debian python3-mock
Provides:       autopkgtest

%description
automatic as-installed testing for Debian packages

%prep
%autosetup -n %{name}-debian-%{version} -p1

%build
make all

%install
%make_install


%files
%license debian/copyright
%{_bindir}/autopkgtest
%{_bindir}/autopkgtest-build-docker
%{_bindir}/autopkgtest-build-lxc
%{_bindir}/autopkgtest-build-lxd
%{_bindir}/autopkgtest-build-podman
%{_bindir}/autopkgtest-build-qemu
%{_bindir}/autopkgtest-buildvm-ubuntu-cloud
%{_bindir}/autopkgtest-virt-chroot
%{_bindir}/autopkgtest-virt-docker
%{_bindir}/autopkgtest-virt-lxc
%{_bindir}/autopkgtest-virt-lxd
%{_bindir}/autopkgtest-virt-null
%{_bindir}/autopkgtest-virt-podman
%{_bindir}/autopkgtest-virt-qemu
%{_bindir}/autopkgtest-virt-schroot
%{_bindir}/autopkgtest-virt-ssh
%{_bindir}/autopkgtest-virt-unshare
%{_datarootdir}/doc/autopkgtest/*
%{_datarootdir}/autopkgtest/*
%{_mandir}/man1/*

%changelog
* Tue Jul 16 2024 weilinfox <caiweilin@iscas.ac.cn> - 5.30-1
- Package init

