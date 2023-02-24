# This file is part of autopkgtest
# autopkgtest is a tool for testing Debian binary packages
#
# autopkgtest is Copyright (C) 2006 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.
#
# See the file CREDITS for a full list of credits information (often
# installed as /usr/share/doc/autopkgtest/CREDITS).

prefix =	/usr
share =		$(DESTDIR)$(prefix)/share
bindir =	$(DESTDIR)$(prefix)/bin
man1dir =	$(share)/man/man1
pkgname =	autopkgtest
docdir =	$(share)/doc/$(pkgname)
datadir =	$(share)/$(pkgname)
pythondir = 	$(datadir)/lib

INSTALL =	install
INSTALL_DIRS =	$(INSTALL) -d
INSTALL_PROG =	$(INSTALL) -m 0755
INSTALL_DATA =	$(INSTALL) -m 0644

virts =		chroot \
		docker \
		lxc \
		lxd \
		null \
		qemu \
		schroot \
		ssh \
		unshare \
		$(NULL)

programs =	tools/autopkgtest-buildvm-ubuntu-cloud \
		tools/autopkgtest-build-docker \
		tools/autopkgtest-build-lxc \
		tools/autopkgtest-build-lxd \
		tools/autopkgtest-build-qemu \
		runner/autopkgtest \
		$(NULL)

pythonfiles =	lib/VirtSubproc.py \
		lib/adtlog.py \
		lib/autopkgtest_args.py \
		lib/autopkgtest_qemu.py \
		lib/adt_testbed.py \
		lib/adt_binaries.py \
		lib/testdesc.py \
		$(NULL)

rstfiles =	$(wildcard doc/*.rst)
htmlfiles =	$(patsubst %.rst,%.html,$(rstfiles))

%.html: %.rst
	rst2html -v $< > $@

all: $(htmlfiles)

install:
	$(INSTALL_DIRS) \
		$(bindir) \
		$(docdir) \
		$(man1dir) \
		$(pythondir) \
		$(datadir)/lib/in-testbed \
		$(datadir)/setup-commands \
		$(datadir)/ssh-setup \
		$(NULL)
	set -e; for f in $(programs); do \
		$(INSTALL_PROG) $$f $(bindir); \
		test ! -f $$f.1 || $(INSTALL_DATA) $$f.1 $(man1dir); \
		done
	set -e; for f in $(virts); do \
		$(INSTALL_PROG) virt/autopkgtest-virt-$$f $(bindir); \
		$(INSTALL_DATA) virt/autopkgtest-virt-$${f}.1 $(man1dir); \
		done
	$(INSTALL_DATA) $(pythonfiles) $(pythondir)
	$(INSTALL_DATA) CREDITS $(docdir)
	$(INSTALL_DATA) $(rstfiles) $(htmlfiles) $(docdir)
	$(INSTALL_PROG) lib/in-testbed/*.sh $(datadir)/lib/in-testbed/
	$(INSTALL_PROG) lib/unshare-helper $(datadir)/lib/
	$(INSTALL_PROG) setup-commands/*[!~] $(datadir)/setup-commands
	$(INSTALL_PROG) ssh-setup/[a-z]*[!~] $(datadir)/ssh-setup
	$(INSTALL_DATA) ssh-setup/SKELETON $(datadir)/ssh-setup
	ln -fns autopkgtest-virt-docker $(bindir)/autopkgtest-virt-podman
	ln -fns autopkgtest-virt-docker.1 $(man1dir)/autopkgtest-virt-podman.1
	ln -fns autopkgtest-build-docker $(bindir)/autopkgtest-build-podman
	ln -fns autopkgtest-build-docker.1 $(man1dir)/autopkgtest-build-podman.1

clean:
	rm -f */*.pyc
	rm -f $(htmlfiles)
