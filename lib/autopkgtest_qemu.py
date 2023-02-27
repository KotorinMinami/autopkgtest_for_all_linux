#!/usr/bin/python3
#
# This is not a stable API; for use within autopkgtest only.
#
# Part of autopkgtest.
# autopkgtest is a tool for testing Debian binary packages.
#
# Copyright 2006-2016 Canonical Ltd.
# Copyright 2016-2020 Simon McVittie
# Copyright 2017 Martin Pitt
# Copyright 2017 Jiri Palecek
# Copyright 2017-2018 Collabora Ltd.
# Copyright 2018 Thadeu Lima de Souza Cascardo
# Copyright 2019 Michael Biebl
# Copyright 2019 Raphaël Hertzog
#
# autopkgtest-virt-qemu was developed by
# Martin Pitt <martin.pitt@ubuntu.com>
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

import errno
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import (
    List,
    Optional,
    Sequence,
    Union,
)

import VirtSubproc
import adtlog


def find_free_port(start: int) -> int:
    '''Find an unused port in the range [start, start+50)'''

    for p in range(start, start + 50):
        adtlog.debug('find_free_port: trying %i' % p)
        try:
            lockfile = '/tmp/autopkgtest-virt-qemu.port.%i' % p
            f = None
            try:
                f = open(lockfile, 'x')
                os.unlink(lockfile)
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (IOError, OSError):
                adtlog.debug('find_free_port: %i is locked' % p)
                continue
            finally:
                if f:
                    f.close()

            s = socket.create_connection(('127.0.0.1', p))
            # if that works, the port is taken
            s.close()
            continue
        except socket.error as e:
            if e.errno == errno.ECONNREFUSED:
                adtlog.debug('find_free_port: %i is free' % p)
                return p
            else:
                pass

    adtlog.debug('find_free_port: all ports are taken')
    return 0


def get_cpuflag() -> Sequence[str]:
    '''Return QEMU cpu option list suitable for host CPU'''

    try:
        with open('/proc/cpuinfo', 'r') as f:
            for line in f:
                if line.startswith('flags'):
                    words = line.split()
                    if 'vmx' in words:
                        adtlog.debug(
                            'Detected KVM capable Intel host CPU, '
                            'enabling nested KVM'
                        )
                        return ['-cpu', 'kvm64,+vmx,+lahf_lm']
                    elif 'svm' in words:  # AMD kvm
                        adtlog.debug(
                            'Detected KVM capable AMD host CPU, '
                            'enabling nested KVM'
                        )
                        # FIXME: this should really be the one below
                        # for more reproducible testbeds, but nothing
                        # except -cpu host works
                        # return ['-cpu', 'kvm64,+svm,+lahf_lm']
                        return ['-cpu', 'host']
    except IOError as e:
        adtlog.warning(
            'Cannot read /proc/cpuinfo to detect CPU flags: %s' % e
        )
        # fetching CPU flags isn't critical (only used to enable
        # nested KVM), so don't fail here

    return []


class QemuImage:
    def __init__(
        self,
        file: str,
        format: Optional[str] = None,
        readonly: bool = False,
    ) -> None:
        self.file = file
        self.overlay = None     # type: Optional[str]
        self.readonly = readonly

        if format is not None:
            self.format = format
        else:
            info = json.loads(
                VirtSubproc.check_exec([
                    'qemu-img', 'info',
                    '--output=json',
                    self.file,
                ], outp=True, timeout=5)
            )

            if 'format' not in info:
                VirtSubproc.bomb('Unable to determine format of %s' % self.file)

            self.format = str(info['format'])

    def __str__(self) -> str:
        bits = []   # type: List[str]

        if self.overlay is None:
            bits.append('file={}'.format(self.file))
            bits.append('format={}'.format(self.format))
        else:
            bits.append('file={}'.format(self.overlay))
            bits.append('format=qcow2')
            bits.append('cache=unsafe')

        bits.append('if=virtio')
        bits.append('discard=unmap')

        if self.readonly:
            bits.append('readonly')

        return ','.join(bits)


class Qemu:
    def __init__(
        self,
        images: Sequence[Union[QemuImage, str]],
        *,
        boot: str = 'auto',
        cpus: int = 1,
        dpkg_architecture: Optional[str] = None,
        overlay: bool = False,
        overlay_dir: Optional[str] = None,
        qemu_architecture: Optional[str] = None,
        qemu_command: Optional[str] = None,
        qemu_options: Sequence[str] = (),
        ram_size: int = 1024,
        workdir: Optional[str] = None
    ) -> None:
        """
        Constructor.

        images: Disk images for the VM. The first image is assumed to be
            the bootable, writable root filesystem, and we actually boot a
            snapshot. The remaining images are assumed to be read-only.

        boot: auto, bios, efi, ieee1275 or none
        cpus: Number of vCPUs
        dpkg_architecture: Architecture, from dpkg's vocabulary
        overlay: If true, use a temporary overlay for first image
        overlay_dir: Store writable overlays here (default: workdir)
        qemu_architecture: Architecture, from qemu's vocabulary
        qemu_command: qemu executable
        qemu_options: Space-separated options for qemu
        ram_size: Amount of RAM in MiB
        workdir: Directory for temporary files (default: a random
            subdirectory of $TMPDIR)
        """

        self.consoles = set(['hvc0', 'hvc1'])
        self.cpus = cpus
        self.images = []    # type: List[QemuImage]
        self.overlay_dir = overlay_dir
        self.ram_size = ram_size
        self.ssh_port = find_free_port(10022)

        if qemu_architecture is not None:
            self.qemu_architecture = qemu_architecture      # type: str
        elif dpkg_architecture is not None:
            self.qemu_architecture = self.qemu_arch_for_dpkg_arch(
                dpkg_architecture
            )
        else:
            self.qemu_architecture = ''

        if not self.qemu_architecture and shutil.which('dpkg') is not None:
            dpkg_architecture = subprocess.check_output(
                ['dpkg', '--print-architecture'],
                universal_newlines=True
            ).strip()
            self.qemu_architecture = self.qemu_arch_for_dpkg_arch(
                dpkg_architecture
            )

        if not self.qemu_architecture and qemu_command is not None:
            guess = self.qemu_arch_for_command(qemu_command)

            if guess is not None:
                self.qemu_architecture = guess

        if not self.qemu_architecture:
            self.qemu_architecture = self.qemu_arch_for_uname()

        if boot == 'auto':
            if self.qemu_architecture in ('i386', 'x86_64'):
                # Just for documentation purposes, we don't do anything
                # differently for BIOS boot
                boot = 'bios'
            elif self.qemu_architecture in ('arm', 'aarch64'):
                boot = 'efi'
            elif self.qemu_architecture == 'ppc64le':
                # Just for documentation purposes, we don't do anything
                # differently for IEEE 1275 (OpenFirmware)
                boot = 'ieee1275'
            else:
                adtlog.debug(
                    'autopkgtest does not know how to boot this machine, '
                    'hopefully nothing special needs to be done?'
                )
                boot = 'none'

        if qemu_command is not None:
            self.qemu_command = qemu_command        # type: str
        elif (
            self.qemu_architecture == 'arm' and
            os.uname()[4] == 'aarch64'
        ):
            # aarch64 host systems can't kvm-accelerate qemu-system-arm,
            # so automatically switch to qemu-system-aarch64 in 32-bit mode
            # (emulating a 32-bit ARMv8a)
            self.qemu_architecture = 'aarch64'
            self.qemu_command = 'qemu-system-aarch64'

            if '-cpu' not in qemu_options:
                qemu_options = [
                    '-cpu', 'host,aarch64=off'
                ] + list(qemu_options)
        else:
            self.qemu_command = self.qemu_command_for_arch(
                self.qemu_architecture
            )

        adtlog.debug('qemu architecture: %s' % self.qemu_architecture)
        adtlog.debug('qemu command: %s' % self.qemu_command)

        if workdir is None:
            workdir = tempfile.mkdtemp(prefix='autopkgtest-qemu.')

        self.workdir = workdir      # type: Optional[str]
        os.chmod(workdir, 0o755)
        self.shareddir = os.path.join(workdir, 'shared')
        os.mkdir(self.shareddir)

        for i, image in enumerate(images):
            if isinstance(image, QemuImage):
                self.images.append(image)
            else:
                assert isinstance(image, str)

                self.images.append(
                    QemuImage(
                        file=image,
                        format=None,
                        readonly=(i != 0),
                    )
                )

        if overlay:
            self.images[0].overlay = self.prepare_overlay(self.images[0])

        if self.ssh_port:
            adtlog.debug(
                'Forwarding local port %i to VM ssh port 22' % self.ssh_port
            )
            nic_opt = ',hostfwd=tcp:127.0.0.1:%i-:22' % self.ssh_port
        else:
            nic_opt = ''

        # start QEMU
        argv = [
            self.qemu_command,
        ]

        if '-machine' not in qemu_options:
            # Some architectures do not have a default machine model
            if self.qemu_architecture in ('aarch64', 'arm'):
                argv.extend(['-machine', 'virt'])
            elif (
                self.qemu_architecture in ('i386', 'x86_64') and
                boot == 'efi'
            ):
                argv.extend(['-machine', 'q35'])

        # Some architectures can only be run with certain CPUs
        if '-cpu' not in qemu_options:
            if self.qemu_architecture == 'aarch64':
                if os.uname()[4] == 'aarch64':
                    # Specifying a particular CPU doesn't seem to be
                    # possible when running natively with kvm
                    argv.extend(['-cpu', 'host'])
                else:
                    # -cpu host is obviously not possible on a foreign
                    # CPU, -cpu max is unusably slow, but this works!
                    argv.extend(['-cpu', 'cortex-a53'])

        argv.extend([
            '-m', str(ram_size),
            '-smp', str(cpus),
            '-nographic',
            '-object', 'rng-random,filename=/dev/urandom,id=rng0',
            '-device', 'virtio-rng-pci,rng=rng0,id=rng-device0',
            '-monitor',
            'unix:%s,server=on,wait=off' % self.get_socket_path('monitor'),
            '-virtfs',
            (
                'local,id=autopkgtest,path=%s,security_model=none,'
                'mount_tag=autopkgtest'
            ) % self.shareddir,
        ])

        if self.qemu_architecture == 'riscv64':
            argv.extend([
                '-device', 'virtio-net-device,netdev=usernet',
                '-netdev', 'user,id=usernet' + nic_opt,
            ])
        else:
            argv.extend([
                '-net', 'nic,model=virtio',
                '-net', 'user' + nic_opt,
            ])

        for hvc in ('hvc0', 'hvc1'):
            if hvc == 'hvc0' and self.qemu_architecture == 'ppc64le':
                # The first -serial argument shows up as /dev/hvc0 in
                # the VM on ppc64le, so identify it as such
                argv.extend([
                    '-serial',
                    'unix:%s,server=on,wait=off' % self.get_socket_path('hvc0'),
                ])
            else:
                if hvc == 'hvc0' or self.qemu_architecture == 'ppc64le':
                    argv.extend([
                        '-device', 'virtio-serial',
                    ])
                argv.extend([
                    '-chardev',
                    'socket,path=%s,server=on,wait=off,id=%s' % (
                        self.get_socket_path(hvc), hvc,
                    ),
                    '-device', 'virtconsole,chardev=%s' % hvc,
                ])

        if self.qemu_architecture in ('x86_64', 'i386'):
            self.consoles.add('ttyS0')
            self.consoles.add('ttyS1')
            argv.extend([
                '-serial',
                'unix:%s,server=on,wait=off' % self.get_socket_path('ttyS0'),
                '-serial',
                'unix:%s,server=on,wait=off' % self.get_socket_path('ttyS1'),
            ])
        elif self.qemu_architecture == 'ppc64le':
            # There are no emulated hardware serial ports in this qemu
            # implementation, only hypervisor virtual consoles
            pass
        else:
            # On ARM this ends up as ttyAMA0 rather than ttyS0, but that's
            # close enough.
            self.consoles.add('ttyS0')
            argv.extend([
                '-serial',
                'unix:%s,server=on,wait=off' % self.get_socket_path('ttyS0'),
            ])

        for i, image in enumerate(self.images):
            argv.append('-drive')
            argv.append('index=%d,%s' % (i, image))

        if boot == 'efi':
            if self.qemu_architecture == 'x86_64':
                code = '/usr/share/OVMF/OVMF_CODE.fd'
                data = '/usr/share/OVMF/OVMF_VARS.fd'
            elif self.qemu_architecture == 'i386':
                code = '/usr/share/OVMF/OVMF32_CODE_4M.secboot.fd'
                data = '/usr/share/OVMF/OVMF32_VARS_4M.fd'
            elif (
                self.qemu_architecture == 'arm' or (
                    self.qemu_architecture == 'aarch64' and
                    'aarch64=off' in str(qemu_options)
                )
            ):
                code = '/usr/share/AAVMF/AAVMF32_CODE.fd'
                data = '/usr/share/AAVMF/AAVMF32_VARS.fd'
            elif self.qemu_architecture == 'aarch64':
                code = '/usr/share/AAVMF/AAVMF_CODE.fd'
                data = '/usr/share/AAVMF/AAVMF_VARS.fd'
            else:
                VirtSubproc.bomb('Unknown architecture for EFI boot')

            shutil.copy(data, '%s/efivars.fd' % workdir)
            argv.append('-drive')
            argv.append('if=pflash,format=raw,unit=0,read-only=on,file=' + code)
            argv.append('-drive')
            argv.append(
                'if=pflash,format=raw,unit=1,file=%s/efivars.fd' % workdir
            )
        else:
            adtlog.debug(
                'Assuming nothing special needs to be done to set up '
                'firmware to boot this machine (boot method: %s)' % boot
            )

        if os.path.exists('/dev/kvm'):
            if self.kvm_compatible(self.qemu_architecture):
                argv.append('-enable-kvm')

            # Enable nested KVM by default on x86_64
            if (
                os.uname()[4] == 'x86_64' and
                self.qemu_architecture == 'x86_64' and
                '-cpu' not in qemu_options
            ):
                argv += get_cpuflag()

        # pass through option to qemu
        if qemu_options:
            argv.extend(qemu_options)

        adtlog.debug('full qemu command-line: %s' % argv)

        self.subprocess = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=subprocess.STDOUT,
        )   # type: Optional[subprocess.Popen[bytes]]

    @staticmethod
    def dpkg_arch_for_qemu_arch(qemu_architecture: str) -> str:
        return {
            'aarch64': 'arm64',
            # alpha
            'arm': 'armhf',     # or armel or arm
            'avr': 'avr32',
            # cris not in dpkg-architecture -L
            # hppa
            # i386
            # m68k
            # microblaze not in dpkg-architecture -L
            # microblazeel not in dpkg-architecture -L
            # mips
            # mips64
            # mips64el
            # mipsel
            # moxie not in dpkg-architecture -L
            # nios2
            # or1k
            'ppc': 'powerpc',
            # ppc64
            'ppc64le': 'ppc64el',
            # riscv32 not in dpkg-architecture -L
            # riscv64
            # rx not in dpkg-architecture -L
            # s390x
            # sh4
            # sh4eb
            # sparc
            # sparc64
            # tricore not in dpkg-architecture -L
            'x86_64': 'amd64',          # or x32
            'x86_64-microvm': 'amd64',  # or x32
            # xtensa not in dpkg-architecture -L
            # xtensaeb not in dpkg-architecture -L
        }.get(qemu_architecture, qemu_architecture)

    @staticmethod
    def qemu_arch_for_command(qemu_command: str) -> Optional[str]:
        known = [
            'aarch64',
            'alpha',
            'arm',
            'avr',
            'cris',
            'hppa',
            'i386',
            'm68k',
            'microblaze',
            'microblazeel',
            'mips',
            'mips64',
            'mips64el',
            'mipsel',
            'moxie',
            'nios2',
            'or1k',
            'ppc',
            'ppc64le',
            'riscv32',
            'riscv64',
            'rx',
            's390x',
            'sh4',
            'sh4eb',
            'sparc',
            'sparc64',
            'tricore',
            'x86_64',
            'x86_64-microvm',
            'xtensa',
            'xtensaeb',
        ]

        base = os.path.basename(qemu_command)

        for k in known:
            if base == 'qemu-system-' + k:
                return k

        for k in known:
            if ('qemu-system-' + k) in base:
                return k

        return None

    @staticmethod
    def qemu_arch_for_dpkg_arch(dpkg_architecture: str) -> str:
        return {
            # alpha
            'amd64': 'x86_64',
            # arm
            'arm64': 'aarch64',
            'arm64ilp32': 'aarch64',
            # armeb?
            'armel': 'arm',
            'armhf': 'arm',
            'avr32': 'avr',
            # hppa
            # i386
            # ia64?
            # m32r?
            # m68k
            # mips
            # mips64
            # mips64el
            # mipsel
            # mipsn32?
            # mipsn32el?
            # mipsn32r6?
            # mipsn32r6el?
            # mipsr6?
            # mipsr6el?
            # nios2
            # or1k
            'powerpc': 'ppc',
            'powerpcel': 'ppc64le',     # ?
            # powerpcspe?
            # ppc64
            'ppc64el': 'ppc64le',
            # riscv64
            's390': 's390x',            # ?
            # s390x
            # sh3?
            # sh3eb?
            # sh4
            # sh4eb
            # sparc
            # sparc64
            # tilegx?
            'x32': 'x86_64',
        }.get(dpkg_architecture, dpkg_architecture)

    @staticmethod
    def qemu_arch_for_uname(uname_m: Optional[str] = None) -> str:
        uname_to_qemu_arch = {'i[3456]86$': 'i386', '^arm': 'arm'}

        if uname_m is None:
            uname_m = os.uname()[4]

        for pattern, arch in uname_to_qemu_arch.items():
            if re.match(pattern, uname_m):
                return arch
        else:
            # usually the qemu architecture is the same as Linux uname -m
            return uname_m

    @staticmethod
    def qemu_command_for_arch(qemu_architecture: str) -> str:
        return 'qemu-system-' + qemu_architecture

    @staticmethod
    def kvm_compatible(
        qemu_architecture: str,
        uname_m: Optional[str] = None
    ) -> bool:
        if uname_m is None:
            uname_m = os.uname()[4]

        if re.match('^i[3456]86$', uname_m):
            uname_m = 'i386'

        return qemu_architecture in {
            'x86_64': ['x86_64', 'i386'],
            'i386': ['i386'],
            'aarch64': ['aarch64'],
            # According to https://wiki.alpinelinux.org/wiki/S390x/Installation
            's390x': ['s390x'],
            'ppc64le': ['ppc64le'],
        }.get(uname_m, [])

    def get_socket_path(self, name: str) -> str:
        assert self.workdir is not None
        return os.path.join(self.workdir, name)

    @property
    def monitor_socket(self) -> socket.socket:
        return VirtSubproc.get_unix_socket(self.get_socket_path('monitor'))

    def get_console_socket(self) -> socket.socket:
        for name in ('ttyS0', 'hvc0'):
            if name in self.consoles:
                path = self.get_socket_path(name)
                return VirtSubproc.get_unix_socket(path)

        VirtSubproc.bomb('No console socket available')
        raise AssertionError            # not reached

    def prepare_overlay(
        self,
        image: QemuImage,
    ) -> str:
        '''Generate a temporary overlay image'''

        # generate a temporary overlay
        if self.overlay_dir is not None:
            overlay = os.path.join(
                self.overlay_dir,
                os.path.basename(image.file) + '.overlay-%s' % time.time()
            )
        else:
            workdir = self.workdir
            assert workdir is not None
            overlay = os.path.join(workdir, 'overlay.img')

        adtlog.debug('Creating temporary overlay image in %s' % overlay)
        VirtSubproc.check_exec(
            [
                'qemu-img', 'create',
                '-f', 'qcow2',
                '-F', image.format,
                '-b', os.path.abspath(image.file),
                overlay,
            ],
            outp=True,
            timeout=300,
        )
        return overlay

    def cleanup(self) -> Optional[int]:
        ret = None

        if self.subprocess is not None:
            self.subprocess.terminate()
            ret = self.subprocess.wait()
            self.subprocess = None

        if self.workdir is not None:
            shutil.rmtree(self.workdir)
            self.workdir = None

        return ret
