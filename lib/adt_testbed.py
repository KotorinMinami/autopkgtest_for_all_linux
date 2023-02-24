# adt_testbed.py is part of autopkgtest
# autopkgtest is a tool for testing Debian binary packages
#
# autopkgtest is Copyright (C) 2006-2015 Canonical Ltd.
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

import os
import sys
import errno
import time
import traceback
import re
import shlex
import signal
import subprocess
import tempfile
import shutil
import urllib.parse
from typing import Set

import adtlog
import VirtSubproc
from testdesc import Unsupported


timeouts = {'short': 100, 'copy': 300, 'install': 3000, 'test': 10000,
            'build': 100000}


# When running installed, this is /usr/share/autopkgtest.
# When running uninstalled, this is the source tree.
# Either way, it has a setup-commands subdirectory.
PKGDATADIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


class Testbed:
    def __init__(self, vserver_argv, output_dir, user,
                 setup_commands=[], setup_commands_boot=[], add_apt_pockets=[],
                 copy_files=[], pin_packages=[], add_apt_sources=[],
                 add_apt_releases=[], apt_default_release=None,
                 enable_apt_fallback=True, shell_fail=False, needs_internet='run'):
        self.sp = None
        self.lastsend = None
        self.scratch = None
        self.modified = False
        self._need_reset_apt = False
        self.stop_sent = False
        self.dpkg_arch = None
        self.exec_cmd = None
        self.output_dir = output_dir
        self.shared_downtmp = None  # testbed's downtmp on the host, if supported
        self.vserver_argv = vserver_argv
        self.user = user
        self.setup_commands = setup_commands
        self.setup_commands_boot = setup_commands_boot
        self.add_apt_pockets = add_apt_pockets
        self.add_apt_sources = add_apt_sources
        self.add_apt_releases = add_apt_releases
        self.pin_packages = pin_packages
        self.default_release = apt_default_release
        self.copy_files = copy_files
        self.initial_kernel_version = None
        # tests might install a different kernel; [(testname, reboot_marker, kver)]
        self.test_kernel_versions = []
        # used for tracking kernel version changes
        self.last_test_name = ''
        self.last_reboot_marker = ''
        self.eatmydata_prefix = []
        self.apt_pin_for_releases = []
        self.enable_apt_fallback = enable_apt_fallback
        self.needs_internet = needs_internet
        self.shell_fail = shell_fail
        self.nproc = None
        self.cpu_model = None
        self.cpu_flags = None
        self._created_user = False
        self._tried_debug = False
        # False if not tried, True if successful, error message if not
        self._tried_provide_sudo = False

        try:
            self.devnull = subprocess.DEVNULL
        except AttributeError:
            self.devnull = open(os.devnull, 'rb')

        adtlog.debug('testbed init')

    @property
    def su_user(self):
        return self.user or 'root'

    def start(self):
        # log date at least once; to ease finding it
        adtlog.info('starting date and time: %s' % time.strftime('%Y-%m-%d %H:%M:%S%z'))

        # are we running from a checkout?
        root_dir = PKGDATADIR
        if os.path.exists(os.path.join(root_dir, '.git')):
            try:
                head = subprocess.check_output(['git', 'show', '--no-patch', '--oneline'],
                                               cwd=root_dir)
                head = head.decode('UTF-8').strip()
            except Exception as e:
                adtlog.debug(str(e))
                head = 'cannot determine current HEAD'
            adtlog.info('git checkout: %s' % head)
        else:
            adtlog.info('version @version@')

        # log command line invocation for the log
        adtlog.info('host %s; command line: %s' % (
            os.uname()[1], ' '.join([shlex.quote(w) for w in sys.argv])))

        self.sp = subprocess.Popen(self.vserver_argv,
                                   stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,
                                   universal_newlines=True)
        self.expect('ok', 0)

    def stop(self):
        adtlog.debug('testbed stop')
        if self.stop_sent:
            # avoid endless loop
            return
        self.stop_sent = True

        self.close()
        if self.sp is None:
            return
        ec = self.sp.returncode
        if ec is None:
            self.sp.stdout.close()
            self.send('quit')
            self.sp.stdin.close()
            ec = self.sp.wait()
        if ec:
            self.debug_fail()
            self.bomb('testbed gave exit status %d after quit' % ec)
        self.sp = None

    def open(self):
        adtlog.debug('testbed open, scratch=%s' % self.scratch)
        if self.scratch is not None:
            return
        pl = self.command('open', (), 1)
        self._opened(pl)

    def post_boot_setup(self):
        '''Setup after (re)booting the test bed'''

        # provide autopkgtest-reboot command, if reboot is supported; /run is
        # usually "noexec" and /[s]bin might be readonly, so create in /tmp
        if 'reboot' in self.caps and 'root-on-testbed' in self.caps:
            adtlog.debug('testbed supports reboot, creating /tmp/autopkgtest-reboot')
            reboot = Path(
                self,
                os.path.join(PKGDATADIR, 'lib', 'in-testbed', 'reboot.sh'),
                '%s/autopkgtest-reboot' % self.scratch,
                False,
            )
            reboot.copydown(mode='0755')
            # TODO: Because we're now creating a symlink, it would be OK
            # to put the symlink in /run again, even though it's noexec,
            # because the actual content is elsewhere
            self.check_exec(
                ['ln', '-fns', reboot.tb, '/tmp/autopkgtest-reboot'],
            )
            # Intentionally not check_exec, and intentionally silencing stderr:
            # /[s]bin might be read-only.
            self.execute(
                ['ln', '-fns', reboot.tb, '/sbin/autopkgtest-reboot'],
                stderr=subprocess.PIPE,
            )

            reboot_prepare = Path(
                self,
                os.path.join(PKGDATADIR, 'lib', 'in-testbed',
                             'reboot-prepare.sh'),
                '%s/autopkgtest-reboot-prepare' % self.scratch,
                False,
            )
            reboot_prepare.copydown(mode='0755')
            self.check_exec(
                [
                    'ln', '-fns', reboot_prepare.tb,
                    '/tmp/autopkgtest-reboot-prepare',
                ],
            )

        # record running kernel version
        kver = self.check_exec(['uname', '-srv'], True).strip()
        if not self.initial_kernel_version:
            assert not self.last_test_name
            self.initial_kernel_version = kver
            adtlog.info('testbed running kernel: ' + self.initial_kernel_version)
        else:
            if kver != self.initial_kernel_version:
                self.test_kernel_versions.append((self.last_test_name, self.last_reboot_marker, kver))
                adtlog.info('testbed running kernel changed: %s (current test: %s%s)' %
                            (kver, self.last_test_name,
                             self.last_reboot_marker and (', last reboot marker: ' + self.last_reboot_marker) or ''))

        # get CPU info
        if self.nproc is None:
            cpu_info = self.check_exec(['sh', '-c', 'nproc; cat /proc/cpuinfo 2>/dev/null || true'],
                                       stdout=True).strip()
            self.nproc = cpu_info.split('\n', 1)[0]
            m = re.search(r'^(model.*name|cpu)\s*:\s*(.*)$', cpu_info, re.MULTILINE | re.IGNORECASE)
            if m:
                self.cpu_model = m.group(2)
            m = re.search(r'^(flags|features)\s*:\s*(.*)$', cpu_info, re.MULTILINE | re.IGNORECASE)
            if m:
                self.cpu_flags = m.group(2)

        xenv = ['AUTOPKGTEST_IS_SETUP_BOOT_COMMAND=1']
        if self.user:
            xenv.append('AUTOPKGTEST_NORMAL_USER=' + self.user)
            xenv.append('ADT_NORMAL_USER=' + self.user)

        for c in self.setup_commands_boot:
            rc = self.execute(['sh', '-ec', c], xenv=xenv, kind='install')[0]
            if rc:
                # setup scripts should exit with 100 if it's the package's
                # fault, otherwise it's considered a transient testbed failure
                if self.shell_fail:
                    self.run_shell()
                if rc == 100:
                    self.badpkg('testbed boot setup commands failed with status 100')
                else:
                    self.bomb('testbed boot setup commands failed with status %i' % rc)

    def _opened(self, pl):
        self._tried_provide_sudo = False
        self.scratch = pl[0]
        self.deps_installed = []
        self.apt_pin_for_releases = []
        self.exec_cmd = list(map(urllib.parse.unquote, self.command('print-execute-command', (), 1)[0].split(',')))
        self.caps = self.command('capabilities', (), None)
        if self.needs_internet in ['try', 'run']:
            self.caps.append('has_internet')
        adtlog.debug('testbed capabilities: %s' % self.caps)
        for c in self.caps:
            if c.startswith('downtmp-host='):
                self.shared_downtmp = c.split('=', 1)[1]

        Path(
            self, os.path.join(PKGDATADIR, 'lib', 'in-testbed', 'wrapper.sh'),
            '%s/wrapper.sh' % self.scratch,
            False,
        ).copydown(mode='0755')

        # provide a default for --user
        if self.user is None and 'root-on-testbed' in self.caps:
            self.user = ''
            for c in self.caps:
                if c.startswith('suggested-normal-user='):
                    self.user = c.split('=', 1)[1]

            if 'revert-full-system' in self.caps and not self.user:
                with open(
                    os.path.join(PKGDATADIR, 'setup-commands', 'create-normal-user'),
                    encoding='UTF-8',
                ) as reader:
                    self.check_exec(['sh', '-euc', reader.read()])
                    self._created_user = True
                    self.user = self.check_exec(
                        ['cat', '/run/autopkgtest-normal-user'],
                        stdout=True,
                    ).strip()
        elif self._created_user:
            # Re-create the same user after reset. We only do this if
            # we created it before the reset, in which case the capabilities
            # should be the same as above and the same username should still
            # be available.
            assert 'revert-full-system' in self.caps, self.caps
            assert 'root-on-testbed' in self.caps, self.caps
            with open(
                os.path.join(PKGDATADIR, 'setup-commands', 'create-normal-user'),
                encoding='UTF-8',
            ) as reader:
                self.check_exec(['sh', '-euc', reader.read(), 'sh', self.user])

        self.run_setup_commands()

        # determine testbed architecture
        self.dpkg_arch = self.check_exec(['dpkg', '--print-architecture'], True).strip()
        adtlog.info('testbed dpkg architecture: ' + self.dpkg_arch)

        # do we have eatmydata?
        (code, out, err) = self.execute(
            ['sh', '-ec', 'command -v eatmydata'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if code == 0:
            adtlog.debug('testbed has eatmydata')
            self.eatmydata_prefix = [out.strip()]

        # record package versions of pristine testbed
        if self.output_dir and self.execute(
            ['sh', '-ec', 'command -v dpkg-query'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )[0] == 0:
            pkglist = TempPath(self, 'testbed-packages', autoclean=False)
            self.check_exec(['sh', '-ec', "dpkg-query --show -f '${Package}\\t${Version}\\n' > %s" % pkglist.tb])
            pkglist.copyup()

        self.post_boot_setup()

    def close(self):
        adtlog.debug('testbed close, scratch=%s' % self.scratch)
        if self.scratch is None:
            return
        self.scratch = None
        if self.sp is None:
            return
        self.command('close')
        self.shared_downtmp = None

    def reboot(self, prepare_only=False):
        '''Reboot the testbed'''

        self.command('reboot', prepare_only and ('prepare-only', ) or ())
        self.post_boot_setup()

    def run_setup_commands(self):
        '''Run --setup-commmands and --copy'''

        requirements = [
            self.setup_commands,
            self.add_apt_pockets,
            self.copy_files,
            self.add_apt_sources,
            self.add_apt_releases,
            self.pin_packages,
            self.default_release,
        ]
        if not any(requirements):
            return

        adtlog.info('@@@@@@@@@@@@@@@@@@@@ test bed setup')
        for (host, tb) in self.copy_files:
            adtlog.debug('Copying file %s to testbed %s' % (host, tb))
            Path(self, host, tb, os.path.isdir(host)).copydown()

        # Make sure default release is set regardless of other options if so
        # required by --apt-default-release. Typically this would be used in
        # combination with --add-apt-pocket or --pin-packages and it wouldn't
        # be needed, but let's not assume there is no other use case.
        if self.default_release:
            self._set_default_release()

        for source in self.add_apt_sources:
            self._add_apt_source(source)

        # create apt sources for --apt-pocket
        for pocket in self.add_apt_pockets:
            pocket = pocket.split('=', 1)[0]  # strip off package list
            script = '''
            sed -rn 's/^(deb|deb-src) +(\\[.*\\] *)?((http|https|file):[^ ]*) +([^ -]+) +(.*)$/\\1 \\2\\3 \\5-%s \\6/p' \\
                /etc/apt/sources.list `ls /etc/apt/sources.list.d/*.list 2>/dev/null|| true` > \\
                /etc/apt/sources.list.d/%s.list
            for retry in 1 2 3; do
                apt-get --no-list-cleanup \\
                        -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/%s.list \\
                        -o Dir::Etc::sourceparts=/dev/null \\
                        update 2>&1 && break || \\
                        if [ $retry = 3 ] ; then
                            return 1
                        else
                            sleep 15
                        fi
            done
            ''' % (pocket, pocket, pocket)
            self.check_exec(['sh', '-ec', script])

        # create apt pinning for --apt-pocket
        for pocket in self.add_apt_pockets:
            # do we have a package list?
            try:
                (pocket, pkglist) = pocket.split('=', 1)
            except ValueError:
                pkglist = ''

            # set priority for the specified pocket.
            # this is useful to override NotAutomatic: yes.
            self._create_apt_pinning_for_pocket(self._get_default_release() + '-' + pocket)

            # set priorities for individual packages if specified
            if pkglist:
                self._create_apt_pinning_for_packages(self._get_default_release() + '-' + pocket, pkglist,
                                                      [self._get_default_release() + '-updates'])

        if self.add_apt_releases:
            get_mirror_and_components = '''
            sed -rn 's/^(deb|deb-src) +(\\[.*\\] *)?((http|https|file):[^ ]*) +([^ ]+) +(.*)$/\\2\\3 \\6/p' \\
                /etc/apt/sources.list `ls /etc/apt/sources.list.d/*.list 2>/dev/null|| true` | head -1
            '''
            mirror_and_components = self.check_exec(['sh', '-ec', get_mirror_and_components], stdout=True).split()
            mirror = mirror_and_components[0]
            components = " ".join(mirror_and_components[1:])
            for release in self.add_apt_releases:
                sources = ["%s %s %s %s" % (t, mirror, release, components) for t in ['deb', 'deb-src']]
                self._add_apt_source(
                    "\n".join(sources),
                    filename='autopkgtest-add-apt-release-%s.list' % release,
                )

        # create apt pinning for --pin-packages
        for package_set in self.pin_packages:
            (release, pkglist) = package_set.split('=', 1)
            self._create_apt_pinning_for_packages(release, pkglist)

        # record the mtimes of dirs affecting the boot
        boot_dirs = '/boot /etc/init /etc/init.d /etc/systemd/system /lib/systemd/system'
        self.check_exec(['bash', '-ec',
                         r'for d in %s; do [ ! -d $d ] || touch -r $d %s/${d//\//_}.stamp; done' % (
                             boot_dirs, self.scratch)])

        xenv = ['AUTOPKGTEST_IS_SETUP_COMMAND=1']
        if self.user:
            xenv.append('AUTOPKGTEST_NORMAL_USER=' + self.user)
            xenv.append('ADT_NORMAL_USER=' + self.user)

        for c in self.setup_commands:
            rc = self.execute(['sh', '-ec', c], xenv=xenv, kind='install')[0]
            if rc:
                # setup scripts should exit with 100 if it's the package's
                # fault, otherwise it's considered a transient testbed failure
                if self.shell_fail:
                    self.run_shell()
                if rc == 100:
                    self.badpkg('testbed setup commands failed with status 100')
                else:
                    self.bomb('testbed setup commands failed with status %i' % rc)

        # if the setup commands affected the boot, then reboot
        if self.setup_commands and 'reboot' in self.caps:
            boot_affected = self.execute(
                ['bash', '-ec', r'[ ! -e /run/autopkgtest_no_reboot.stamp ] || exit 0;'
                 r'for d in %s; do s=%s/${d//\//_}.stamp;'
                 r'  [ ! -d $d ] || [ `stat -c %%Y $d` = `stat -c %%Y $s` ]; done' % (
                     boot_dirs, self.scratch)])[0]
            if boot_affected:
                adtlog.info('rebooting testbed after setup commands that affected boot')
                self.reboot()

    def reset(self, deps_new):
        '''Reset the testbed, if possible and necessary'''

        adtlog.debug('testbed reset: modified=%s, deps_installed=%s, deps_new=%s' %
                     (self.modified, self.deps_installed, deps_new))
        if 'revert' in self.caps and (
                self.modified or
                [d for d in self.deps_installed if d not in deps_new]):
            adtlog.debug('testbed reset')
            pl = self.command('revert', (), 1)
            self._opened(pl)
        self.modified = False

    def install_deps(self, deps_new, shell_on_failure=False, synth_deps=[]):
        '''Install dependencies into testbed'''
        adtlog.debug('install_deps: deps_new=%s' % deps_new)

        self.deps_installed = deps_new
        if not deps_new:
            return
        self.satisfy_dependencies_string(', '.join(deps_new), 'install-deps', shell_on_failure=shell_on_failure, synth_deps=synth_deps)

    def _provide_sudo(self) -> str:
        '''
        Enable the needs-sudo restriction, or return why we can't.
        '''

        if 'root-on-testbed' in self.caps and not self.user:
            return (
                'Cannot enable needs-sudo restriction: no ordinary user '
                'available'
            )

        # This is intentionally similar to how run_test() runs it, to make
        # sure that we can sudo here if and only if we could sudo in the test.
        if 'root-on-testbed' in self.caps:
            run_as_user = ['su', '-s', '/bin/bash', self.user, '-c']
        else:
            run_as_user = ['bash', '-c']

        # First check whether the test user is in the sudo group
        rc, out, err = self.execute(
            run_as_user + ['id -Gn'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        if rc == 0 and 'sudo' in out.split():
            adtlog.debug('User "%s" groups include sudo (%s)'
                         % (self.user, out.strip()))
            # Also check whether they need a password (by default yes,
            # but in e.g. Ubuntu cloud images they don't)
            rc, out, err = self.execute(
                run_as_user + ['sudo -n -v'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            adtlog.debug('sudo -n -v stdout: %s' % out.strip())
            adtlog.debug('sudo -n -v stderr: %s' % err.strip())
            adtlog.debug('sudo -n -v status: %d' % rc)
            already_has_sudo = (rc == 0)
        else:
            adtlog.debug('id -Gn stdout: %s' % out.strip())
            adtlog.debug('id -Gn stderr: %s' % err.strip())
            adtlog.debug('id -Gn status: %d' % rc)
            already_has_sudo = False

        if already_has_sudo:
            adtlog.debug('User "%s" can already sudo without password' %
                         self.user)
            return ''

        if 'root-on-testbed' not in self.caps:
            return 'Cannot enable needs-sudo restriction: not root'

        if 'revert-full-system' not in self.caps:
            return 'Cannot enable needs-sudo restriction: cannot revert'

        self.needs_reset()
        adtlog.info('Setting up user "%s" to sudo without password...' %
                    self.user)

        with open(
            os.path.join(PKGDATADIR, 'setup-commands', 'enable-sudo')
        ) as reader:
            script = reader.read()

        rc, _, _ = self.execute(
            ['sh', '-euc', script, 'enable-sudo', self.user],
        )

        if rc != 0:
            return (
                'Failed to enable needs-sudo restriction: exit '
                'status %d'
            ) % rc

        return ''

    def satisfy_restrictions(
        self,
        name: str,
        restrictions: Set[str],
    ) -> None:
        '''
        Try to satisfy restrictions that can be set up dynamically.
        Raise Unsupported if unable to do so.
        '''

        if 'needs-sudo' in restrictions:
            if not self._tried_provide_sudo:
                self._tried_provide_sudo = self._provide_sudo() or True

            if self._tried_provide_sudo is not True:
                raise Unsupported(name, self._tried_provide_sudo)

    def needs_reset(self):
        # show what caused a reset
        (fname, lineno, function, code) = traceback.extract_stack(limit=2)[0]
        adtlog.debug('needs_reset, previously=%s, requested by %s() line %i' %
                     (self.modified, function, lineno))
        self.modified = True

    def bomb(self, m, _type=adtlog.TestbedFailure):
        adtlog.debug('%s %s' % (_type.__name__, m))
        self.stop()
        raise _type(m)

    def badpkg(self, m):
        _type = adtlog.BadPackageError
        adtlog.debug('%s %s' % (_type.__name__, m))
        raise _type(m)

    def debug_fail(self):
        # Avoid recursion, and also avoid repeating ourselves if cleanup
        # fails after we already tried to debug a failure
        if self._tried_debug:
            return

        self._tried_debug = True

        # Many reasons for failure will make auxverb_debug_fail also fail.
        # Ignore that, to preserve the original exception
        try:
            self.command('auxverb_debug_fail')
        except Exception as e:
            adtlog.debug('auxverb_debug_fail failed: %s' % e)

    def send(self, string):
        try:
            adtlog.debug('sending command to testbed: ' + string)
            self.sp.stdin.write(string)
            self.sp.stdin.write('\n')
            self.sp.stdin.flush()
            self.lastsend = string
        except Exception as e:
            self.debug_fail()
            self.bomb('cannot send to testbed: %s' % e)

    def expect(self, keyword, nresults):
        line = self.sp.stdout.readline()
        if not line:
            self.debug_fail()
            self.bomb('unexpected eof from the testbed')
        if not line.endswith('\n'):
            self.bomb('unterminated line from the testbed')
        line = line.rstrip('\n')
        adtlog.debug('got reply from testbed: ' + line)
        ll = line.split()
        if not ll:
            self.bomb('unexpected whitespace-only line from the testbed')
        if ll[0] != keyword:
            self.debug_fail()

            if self.lastsend is None:
                self.bomb("got banner `%s', expected `%s...'" %
                          (line, keyword))
            else:
                self.bomb("sent `%s', got `%s', expected `%s...'" %
                          (self.lastsend, line, keyword))
        ll = ll[1:]
        if nresults is not None and len(ll) != nresults:
            self.bomb("sent `%s', got `%s' (%d result parameters),"
                      " expected %d result parameters" %
                      (self.lastsend, line, len(ll), nresults))
        return ll

    def command(self, cmd, args=(), nresults=0, unquote=True):
        # pass args=[None,...] or =(None,...) to avoid more url quoting
        if type(cmd) is str:
            cmd = [cmd]
        if len(args) and args[0] is None:
            args = args[1:]
        else:
            args = list(map(urllib.parse.quote, args))
        al = cmd + args
        self.send(' '.join(al))
        ll = self.expect('ok', nresults)
        if unquote:
            ll = list(map(urllib.parse.unquote, ll))
        return ll

    def execute(self, argv, xenv=[], stdout=None, stderr=None, kind='short'):
        '''Run command in testbed.

        The commands stdout/err will be piped directly to autopkgtest and its log
        files, unless redirection happens with the stdout/stderr arguments
        (passed to Popen).

        Return (exit code, stdout, stderr). stdout/err will be None when output
        is not redirected.
        '''
        env = list(xenv)  # copy
        if kind == 'install':
            env.append('DEBIAN_FRONTEND=noninteractive')
            env.append('APT_LISTBUGS_FRONTEND=none')
            env.append('APT_LISTCHANGES_FRONTEND=none')

        adtlog.debug('testbed command %s, kind %s, sout %s, serr %s, env %s' %
                     (argv, kind, stdout and 'pipe' or 'raw',
                      stderr and 'pipe' or 'raw', env))

        if env:
            argv = ['env'] + env + argv

        VirtSubproc.timeout_start(timeouts[kind])
        try:
            proc = subprocess.Popen(self.exec_cmd + argv,
                                    stdin=self.devnull,
                                    stdout=stdout, stderr=stderr)
            (out, err) = proc.communicate()
            if out is not None:
                out = out.decode()
            if err is not None:
                err = err.decode()
            VirtSubproc.timeout_stop()
        except VirtSubproc.Timeout:
            # This is a bit of a hack, but what can we do.. we can't kill/clean
            # up sudo processes, we can only hope that they clean up themselves
            # after we stop the testbed
            killtree(proc.pid)
            adtlog.debug('timed out on %s %s (kind: %s)' % (self.exec_cmd, argv, kind))
            if 'sudo' not in self.exec_cmd:
                proc.wait()
            msg = 'timed out on command "%s" (kind: %s)' % (' '.join(argv), kind)
            if kind == 'test':
                adtlog.error(msg)
                raise
            else:
                self.debug_fail()
                self.bomb(msg)

        adtlog.debug('testbed command exited with code %i' % proc.returncode)

        if proc.returncode in (254, 255):
            self.debug_fail()
            self.bomb('testbed auxverb failed with exit code %i' % proc.returncode)

        return (proc.returncode, out, err)

    def check_exec(self, argv, stdout=False, kind='short'):
        '''Run argv in testbed.

        If stdout is True, capture stdout and return it. Otherwise, don't
        redirect and return None.

        argv must succeed and not print any stderr.
        '''
        (code, out, err) = self.execute(argv,
                                        stdout=(stdout and subprocess.PIPE or None),
                                        stderr=subprocess.PIPE, kind=kind)
        if err:
            self.bomb('"%s" failed with stderr "%s"' % (' '.join(argv), err),
                      adtlog.AutopkgtestError)
        if code != 0:
            self.bomb('"%s" failed with status %i' % (' '.join(argv), code),
                      adtlog.AutopkgtestError)
        return out

    def is_real_package_installed(self, package):
        '''Check if a non-virtual package is installed in the testbed'''
        (rc, out, err) = self.execute(['dpkg-query', '--show', '-f', '${Status}', package],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if rc != 0:
            if 'no packages found' in err:
                return False
            self.badpkg('Failed to run dpkg-query: %s (exit code %d)' % (err, rc))
        if out == 'install ok installed':
            return True
        return False

    def is_virtual_package_installed(self, package):
        '''Check if a package is installed in the testbed'''
        (rc, out, err) = self.execute(['dpkg-query', '--show', '-f', '${Status} ${Provides}\n', '*'],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if rc != 0:
            self.badpkg('Failed to run dpkg-query: %s (exit code %d)' % (err, rc))
            return False
        if not out:
            return False
        for line in out.splitlines():
            prefix = 'install ok installed '
            if not line.startswith(prefix):
                continue
            line = line[len(prefix):]
            for p in line.split(','):
                (p, _, _) = p.lstrip().partition(' ')  # ' foo (== 1.0)' => 'foo'
                if p == package:
                    return True
        return False

    def _run_apt_install(self, what, prefix, ignorerc=False):
        '''actually run apt-get install'''

        # capture status-fd to stderr
        (rc, _, serr) = self.execute(['/bin/sh', '-ec', '%s apt-get install '
                                      '--assume-yes %s '
                                      '-o APT::Status-Fd=3 '
                                      '-o APT::Install-Recommends=false '
                                      '-o Dpkg::Options::=--force-confnew '
                                      '-o Debug::pkgProblemResolver=true 3>&2 2>&1' %
                                      (prefix, what)],
                                     kind='install', stderr=subprocess.PIPE)
        if not ignorerc and rc != 0:
            adtlog.debug('apt-get install %s failed; status-fd:\n%s' % (what, serr))
            # check if apt failed during package download, which might be a
            # transient error, so retry
            if 'dlstatus:' in serr and 'pmstatus:' not in serr:
                raise adtlog.AptDownloadError
            else:
                raise adtlog.AptPermanentError

    def install_apt(self, deps, shell_on_failure=False, synth_deps=[]):
        '''Install dependencies with apt-get into testbed

        This requires root privileges and a writable file system.
        '''
        # create a dummy deb with the deps
        pkgdir = tempfile.mkdtemp(prefix='autopkgtest-satdep.')
        debdir = os.path.join(pkgdir, 'DEBIAN')
        os.chmod(pkgdir, 0o755)
        os.mkdir(debdir)
        os.chmod(debdir, 0o755)
        with open(os.path.join(debdir, 'control'), 'w') as f:
            f.write('''Package: autopkgtest-satdep
Section: oldlibs
Priority: extra
Maintainer: autogenerated
Version: 0
Architecture: %s
Depends: %s
Description: satisfy autopkgtest test dependencies
''' % (self.dpkg_arch, deps))

        deb = TempPath(self, 'autopkgtest-satdep.deb')
        subprocess.check_call(['dpkg-deb', '-Zxz', '-b', pkgdir, deb.host],
                              stdout=subprocess.PIPE)
        shutil.rmtree(pkgdir)
        deb.copydown()

        # install it and its dependencies in the tb; our apt pinning is not
        # very clever wrt. resolving transitional dependencies in the pocket,
        # so we might need to retry without pinning
        download_fail_retries = 3
        while True:
            self.check_exec(['dpkg', '--unpack', deb.tb], stdout=subprocess.PIPE)

            # if '@' was provided as a test dependency (as seen in synth_deps),
            # we explicitly install the corresponding binary packages in case
            # the dependencies on those were satisfied by versioned Provides

            rc = 0
            try:
                self._run_apt_install('--fix-broken', ' '.join(self.eatmydata_prefix))
                need_explicit_install = []
                for dep in synth_deps:
                    if dep != list(dep):
                        # simple test dependency (no alternatives)
                        if self.is_real_package_installed(dep):
                            continue
                        if self.is_virtual_package_installed(dep):
                            need_explicit_install.append(dep)
                            continue
                        adtlog.warning('package %s is not installed though it should be' % dep)
                    else:
                        # figure out which test dependency alternative got installed
                        installed_virtual_packages = []
                        found_a_real_package = False
                        for pkg in dep:
                            if self.is_real_package_installed(pkg):
                                # no need to install anything from this set of alternatives
                                found_a_real_package = True
                                break
                            if self.is_virtual_package_installed(pkg):
                                installed_virtual_packages.append(pkg)
                        if found_a_real_package:
                            continue
                        if not installed_virtual_packages:
                            adtlog.warning('no alternative in %s is installed though one should be' % dep)
                            continue
                        if len(installed_virtual_packages) > 1:
                            adtlog.warning('more than one test dependency alternative in %s installed as a virtual package, installing the first one (%s) as the real package' % (dep, installed_virtual_packages[0]))
                        need_explicit_install.append(installed_virtual_packages[0])

                if need_explicit_install:
                    adtlog.debug('installing real packages of test dependencies: %s' % need_explicit_install)
                    self._run_apt_install(' '.join(need_explicit_install), ' '.join(self.eatmydata_prefix))

            # check if apt failed during package download, which might be a
            # transient error, so retry
            except adtlog.AptDownloadError:
                download_fail_retries -= 1
                if download_fail_retries > 0:
                    adtlog.warning('apt failed to download packages, retrying in 10s...')
                    time.sleep(10)
                    continue
                else:
                    self.bomb('apt repeatedly failed to download packages')

            except adtlog.AptPermanentError:
                rc = -1
                if shell_on_failure:
                    self.run_shell()
            else:
                # apt-get -f may succeed, but its solution might remove
                # autopkgtest-satdep, which is still a failure
                rc = self.execute(['dpkg', '--status', 'autopkgtest-satdep'],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)[0]

            if rc != 0:
                if self.apt_pin_for_releases and self.enable_apt_fallback:
                    release = self.apt_pin_for_releases.pop()
                    adtlog.warning('Test dependencies are unsatisfiable with using apt pinning. '
                                   'Retrying with using all packages from %s' % release)
                    self.check_exec(['/bin/sh', '-ec', 'rm /etc/apt/preferences.d/autopkgtest-' + release])
                    if not self.apt_pin_for_releases:
                        self.check_exec(['/bin/sh', '-ec', 'rm -f /etc/apt/preferences.d/autopkgtest-default-release'])
                    continue

                adtlog.warning('Test dependencies are unsatisfiable - calling '
                               'apt install on test deps directly for further '
                               'data about failing dependencies in test logs')
                self._run_apt_install('--simulate ' + deps.replace(',', ''),
                                      ' '.join(self.eatmydata_prefix),
                                      ignorerc=True)

                if shell_on_failure:
                    self.run_shell()
                if self.enable_apt_fallback:
                    self.badpkg('Test dependencies are unsatisfiable. A common reason is '
                                'that your testbed is out of date with respect to the '
                                'archive, and you need to use a current testbed or run '
                                'apt-get update or use -U.')
                else:
                    self.badpkg('Test dependencies are unsatisfiable. A common reason is '
                                'that the requested apt pinning prevented dependencies '
                                'from the non-default suite to be installed. In that '
                                'case you need to add those dependencies to the pinning '
                                'list.')
            break

        # remove autopkgtest-satdep to avoid confusing tests, but avoid marking our
        # test dependencies for auto-removal
        out = self.check_exec(['apt-get', '--simulate', '--quiet',
                               '-o', 'APT::Get::Show-User-Simulation-Note=False',
                               '--auto-remove',
                               'purge', 'autopkgtest-satdep'],
                              True)
        test_deps = []
        for line in out.splitlines():
            if not line.startswith('Purg '):
                continue
            pkg = line.split()[1]
            if pkg != 'autopkgtest-satdep':
                test_deps.append(pkg)
        if test_deps:
            adtlog.debug('Marking test dependencies as manually installed: %s' %
                         ' '.join(test_deps))
            # avoid overly long command lines
            batch = 0
            while batch < len(test_deps):
                self.check_exec(['apt-mark', 'manual', '-qq'] + test_deps[batch:batch + 20])
                batch += 20

        self.execute(['dpkg', '--purge', 'autopkgtest-satdep'])

    def satisfy_dependencies_string(self, deps, what,
                                    build_dep=False, shell_on_failure=False, synth_deps=[]):
        '''Install dependencies from a string into the testbed'''

        adtlog.debug('%s: satisfying %s' % (what, deps))

        # ignore ":native" tags, apt cannot parse them and deps_parse() does
        # not seem to have an option to get rid of them; we always test on the
        # native platform
        deps = deps.replace(':native', '')

        # resolve arch specific dependencies; don't use universal_newlines
        # here, it's broken for stdin on Python 3.2
        if build_dep:
            extra_args = ', reduce_profiles => $supports_profiles, build_dep => 1'
        else:
            extra_args = ''
        perl = subprocess.Popen(['perl', '-'], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE)
        code = '''use Dpkg::Deps;
                  $supports_profiles = ($Dpkg::Deps::VERSION gt '1.04' or 0);
                  $origdeps = '%s';
                  $origdeps =~ s/(^|,)[^<,]+<[^!,>]+>//g if (!$supports_profiles);
                  $dep = deps_parse($origdeps, reduce_arch => 1, host_arch => '%s' %s);
                  $out = $dep->output();
                  # fall back to ignoring build profiles
                  $out =~ s/ <![^ >]+>//g if (!$supports_profiles);
                  print $out, "\\n";
                  ''' % (deps, self.dpkg_arch, extra_args)
        deps = perl.communicate(code.encode('UTF-8'))[0].decode('UTF-8').strip()
        if perl.returncode != 0:
            self.bomb('failed to run perl for parsing dependencies')
        adtlog.debug('%s: architecture resolved: %s' % (what, deps))

        # check if we can use apt-get
        can_apt_get = False
        if 'root-on-testbed' in self.caps:
            rc = self.execute(['test', '-w', '/var/lib/dpkg/status'])[0]
            if rc == 0:
                can_apt_get = True
        adtlog.debug('can use apt-get on testbed: %s' % can_apt_get)

        if can_apt_get:
            self.install_apt(deps, shell_on_failure, synth_deps)
        else:
            has_dpkg_checkbuilddeps = self.execute(
                ['sh', '-ec', 'command -v dpkg-checkbuilddeps'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )[0] == 0
            if has_dpkg_checkbuilddeps:
                rc, _, err = self.execute(
                    ['dpkg-checkbuilddeps', '-d', deps, "/dev/null"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                if rc != 0:
                    missing = re.sub('dpkg-checkbuilddeps: error: Unmet build dependencies: ', '', err)
                    self.badpkg('test dependencies missing: %s' % missing)
            else:
                adtlog.warning('test dependencies (%s) are not fully satisfied, but continuing anyway since dpkg-checkbuilddeps it not available to determine which ones are missing.' % deps)

    def run_shell(self, cwd=None, extra_env=[]):
        '''Run shell in testbed for debugging tests'''

        adtlog.info(' - - - - - - - - - - running shell - - - - - - - - - -')
        self.command('shell', [cwd or '/'] + extra_env)

    def run_test(self, tree, test, extra_env=[], shell_on_failure=False,
                 shell=False, build_parallel=None):
        '''Run given test in testbed

        tree (a Path) is the source tree root.
        '''
        def _info(m):
            adtlog.info('test %s: %s' % (test.name, m))

        self.last_test_name = test.name

        if test.path and not os.path.exists(os.path.join(tree.host, test.path)):
            self.badpkg('%s does not exist' % test.path)

        # record installed package versions
        if self.output_dir and self.execute(
            ['sh', '-ec', 'command -v dpkg-query'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )[0] == 0:
            pkglist = TempPath(self, test.name + '-packages.all', autoclean=False)
            self.check_exec([
                'sh', '-ec', "dpkg-query --show -f '${Package}\\t${Version}\\n' > %s" % pkglist.tb])
            pkglist.copyup()

            # filter out packages from the base system
            with open(pkglist.host[:-4], 'w') as out:
                join = subprocess.Popen(['join', '-v2', '-t\t',
                                         os.path.join(self.output_dir, 'testbed-packages'), pkglist.host],
                                        stdout=out, env={})
                join.communicate()
                if join.returncode != 0:
                    self.badpkg('failed to call join for test specific package list, code %d' % join.returncode)
            os.unlink(pkglist.host)

        # ensure our tests are in the testbed
        tree.copydown(check_existing=True)

        # stdout/err files in testbed
        so = TempPath(self, test.name + '-stdout', autoclean=False)
        se = TempPath(self, test.name + '-stderr', autoclean=False)

        # create script to run test
        test_artifacts = '%s/%s-artifacts' % (self.scratch, test.name)
        autopkgtest_tmp = '%s/autopkgtest_tmp' % (self.scratch)
        assert self.nproc is not None
        # TODO: Move more of this into wrapper.sh
        script = 'set -e; ' \
                 'export USER=`id -nu`; ' \
                 '. /etc/profile >/dev/null 2>&1 || true; ' \
                 ' . ~/.profile >/dev/null 2>&1 || true; ' \
                 'buildtree="%(t)s"; ' \
                 'mkdir -p -m 1777 -- "%(a)s"; ' \
                 'export AUTOPKGTEST_ARTIFACTS="%(a)s"; ' \
                 'export ADT_ARTIFACTS="$AUTOPKGTEST_ARTIFACTS"; ' \
                 'mkdir -p -m 755 "%(tmp)s"; export AUTOPKGTEST_TMP="%(tmp)s"; ' \
                 'export ADTTMP="$AUTOPKGTEST_TMP"; ' \
                 'export DEBIAN_FRONTEND=noninteractive; ' \
                 'export LANG=C.UTF-8; ' \
                 '''export DEB_BUILD_OPTIONS=parallel=%(cpu)s; ''' \
                 'unset LANGUAGE LC_CTYPE LC_NUMERIC LC_TIME LC_COLLATE '\
                 '  LC_MONETARY LC_MESSAGES LC_PAPER LC_NAME LC_ADDRESS '\
                 '  LC_TELEPHONE LC_MEASUREMENT LC_IDENTIFICATION LC_ALL;' \
                 'cd "$buildtree"; '\
                 % {'t': tree.tb, 'a': test_artifacts, 'tmp': autopkgtest_tmp,
                    'cpu': build_parallel or self.nproc}

        if 'needs-root' in test.restrictions and self.user is not None:
            script += 'export AUTOPKGTEST_NORMAL_USER=%s; ' % self.user
            script += 'export ADT_NORMAL_USER=%s; ' % self.user

        for e in extra_env:
            script += 'export \'%s\'; ' % e

        if test.path:
            test_cmd = os.path.join(tree.tb, test.path)
            script += 'chmod +x %s; ' % test_cmd
        else:
            test_cmd = "bash -ec '%s'" % test.command.replace("'", "'\\''")

        script += ' '.join([
            'exec',
            '{}/wrapper.sh'.format(shlex.quote(self.scratch)),
            '--script-pid-file=/tmp/autopkgtest_script_pid',
            '--stderr={}'.format(shlex.quote(se.tb)),
            '--stdout={}'.format(shlex.quote(so.tb)),
            '--',
            test_cmd,
            ';',
        ])

        if 'needs-root' not in test.restrictions and self.user is not None:
            if 'root-on-testbed' not in self.caps:
                self.bomb('cannot change to user %s without root-on-testbed' % self.user,
                          adtlog.AutopkgtestError)
            # we don't want -l here which resets the environment from
            # self.execute(); so emulate the parts that we want
            # FIXME: move "run as user" as an argument of execute()/check_exec() and run with -l
            test_argv = ['su', '-s', '/bin/bash', self.su_user, '-c']

            if 'rw-build-tree' in test.restrictions:
                self.check_exec(['chown', '-R', self.user, tree.tb])
        else:
            # this ensures that we have a PAM/logind session for root tests as
            # well; with some interfaces like ttyS1 or lxc_attach we don't log
            # in to the testbed
            if 'root-on-testbed' in self.caps:
                test_argv = ['su', '-s', '/bin/bash', 'root', '-c']
            else:
                test_argv = ['bash', '-c']

        # run test script
        if test.command:
            _info(test.command)
        _info('[-----------------------')

        # tests may reboot, so we might need to run several times
        self.last_reboot_marker = ''
        timeout = False
        while True:
            if self.last_reboot_marker:
                script_prefix = 'export AUTOPKGTEST_REBOOT_MARK="%s"; export ADT_REBOOT_MARK="$AUTOPKGTEST_REBOOT_MARK"; ' % self.last_reboot_marker
            else:
                script_prefix = ''
            try:
                rc = self.execute(test_argv + [script_prefix + script], kind='test')[0]
            except VirtSubproc.Timeout:
                rc = 1
                timeout = True
                break

            # If the shell or su command that runs wrapper.sh exits with
            # status 128+SIGKILL, that probably means either wrapper.sh or
            # the test itself was terminated with SIGKILL, which might be
            # because the test asked for the testbed to be rebooted.
            if (
                rc in (-signal.SIGKILL, 128 + signal.SIGKILL) and
                'reboot' in self.caps
            ):
                adtlog.debug('test process SIGKILLed, checking for reboot marker')
                (code, reboot_marker, err) = self.execute(
                    ['cat', '/run/autopkgtest-reboot-mark'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if code == 0:
                    self.last_reboot_marker = reboot_marker.strip()
                    adtlog.info('test process requested reboot with marker %s' % self.last_reboot_marker)
                    self.reboot()
                    continue

                adtlog.debug('test process SIGKILLed, checking for prepare-reboot marker')
                (code, reboot_marker, err) = self.execute(
                    ['cat', '/run/autopkgtest-reboot-prepare-mark'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if code == 0:
                    self.last_reboot_marker = reboot_marker.strip()
                    adtlog.info('test process requested preparation for reboot with marker %s' % self.last_reboot_marker)
                    self.reboot(prepare_only=True)
                    continue

                adtlog.debug('no reboot marker, considering a failure')
            break

        # give the setup_trace() cats some time to catch up
        sys.stdout.flush()
        sys.stderr.flush()
        time.sleep(0.3)
        _info('-----------------------]')
        adtlog.debug('testbed executing test finished with exit status %i' % rc)

        # copy stdout/err files to host
        try:
            so.copyup()
            se.copyup()
            se_size = os.path.getsize(se.host)
        except adtlog.TestbedFailure:
            if timeout:
                # if the test timed out, it's likely that the test destroyed
                # the testbed, so ignore this and call it a failure
                adtlog.warning('Copying up test output timed out, ignoring')
                se_size = 0
                so.host = None
                se.host = None
            else:
                # smells like a tmpfail
                raise

        # avoid mixing up stdout (from report) and stderr (from logging) in output
        sys.stdout.flush()
        sys.stderr.flush()
        time.sleep(0.1)

        _info(' - - - - - - - - - - results - - - - - - - - - -')

        if timeout:
            test.failed('timed out')
        elif rc == 77 and 'skippable' in test.restrictions:
            test.set_skipped('exit status 77 and marked as skippable')
        elif rc != 0:
            if 'needs-internet' in test.restrictions and self.needs_internet == 'try':
                test.set_skipped("Failed, but test has needs-internet and that's not guaranteed")
            else:
                test.failed('non-zero exit status %d' % rc)
        elif se_size != 0 and 'allow-stderr' not in test.restrictions:
            with open(se.host, encoding='UTF-8', errors='replace') as f:
                stderr_top = f.readline().rstrip('\n \t\r')
            test.failed('stderr: %s' % stderr_top)
        else:
            test.passed()

        sys.stdout.flush()
        sys.stderr.flush()

        # skip the remaining processing if the testbed got broken
        if se.host is None:
            adtlog.debug('Skipping remaining log processing and testbed restore after timeout')
            return

        if os.path.getsize(so.host) == 0:
            # don't produce empty -stdout files in --output-dir
            so.autoclean = True

        if se_size != 0 and 'allow-stderr' not in test.restrictions:
            # give tee processes some time to catch up, to avoid mis-ordered logs
            time.sleep(0.2)
            _info(' - - - - - - - - - - stderr - - - - - - - - - -')
            with open(se.host, 'rb') as f:
                while True:
                    block = f.read1(1000000)
                    if not block:
                        break
                    sys.stderr.buffer.write(block)
            sys.stderr.buffer.flush()
        else:
            # don't produce empty -stderr files in --output-dir
            if se_size == 0:
                se.autoclean = True

        # copy artifacts to host, if we have --output-dir
        if self.output_dir:
            ap = Path(self, os.path.join(self.output_dir, 'artifacts'),
                      test_artifacts, is_dir=True)
            ap.copyup()
            # don't keep an empty artifacts dir around
            if not os.listdir(ap.host):
                os.rmdir(ap.host)

        if shell or (shell_on_failure and not test.result):
            self.run_shell(tree.tb, ['AUTOPKGTEST_ARTIFACTS="%s"' % test_artifacts,
                                     'AUTOPKGTEST_TMP="%s"' % autopkgtest_tmp])

        # clean up artifacts and AUTOPKGTEST_TMP dirs
        self.check_exec(['rm', '-rf', test_artifacts, autopkgtest_tmp])

    #
    # helper methods
    #

    def _create_apt_pinning_for_pocket(self, release):
        '''Create apt pinning for --apt-pocket=pocket[=pkglist] with
           Pin-Priority: 500 to undo the effect of NotAutomatic: yes'''

        script = 'mkdir -p /etc/apt/preferences.d; '
        script += 'printf "Package: *\\nPin: release a=%(release)s\\nPin-Priority: 500\\n" > /etc/apt/preferences.d/autopkgtest-%(release)s-baseline; ' % \
            {'release': release}
        self.check_exec(['sh', '-ec', script])

    def _create_apt_pinning_for_packages(self, release, pkglist, default_releases=[]):
        '''Create apt pinning for --apt-pocket=pocket=pkglist and
           --pin-packages=release=pkglist'''

        # sort pkglist into source and binary packages
        binpkgs = []
        srcpkgs = []
        for i in pkglist.split(','):
            i = i.strip()
            if i.startswith('src:'):
                srcpkgs.append(i[4:])
            else:
                binpkgs.append(i)

        script = 'mkdir -p /etc/apt/preferences.d; '
        script += 'PKGS="%s"; ' % ' '.join(binpkgs)

        # translate src:name entries into binaries of that source
        if srcpkgs:
            script += 'PKGS="$PKGS $(apt-cache showsrc %s | ' \
                '''awk '/^Package-List:/ { show=1; next } (/^ / && show==1) { print $1; next } { show=0 }' |''' \
                '''sort -u | tr '\\n' ' ')"; ''' % \
                ' '.join(srcpkgs)

        # filenames shouldn't contain slashes, so convert them when
        # they appear in the release name
        # apt also doesn't like unknown extensions, so replace dot too
        release_file_name = release.replace('/', '-').replace('.', '_')
        # prefer given packages from series, but make sure that other packages
        # are taken from default release as much as possible
        script += 'printf "Package: $PKGS\\nPin: release a=%(release)s\\nPin-Priority: 995\\n" > /etc/apt/preferences.d/autopkgtest-%(release_file_name)s; ' % \
            {'release': release, 'release_file_name': release_file_name}
        for default in default_releases:
            script += 'printf "\nPackage: *\\nPin: release a=%(default)s\\nPin-Priority: 990\\n" >> /etc/apt/preferences.d/autopkgtest-%(release_file_name)s; ' % \
                {'release_file_name': release_file_name, 'default': default}
        self.check_exec(['sh', '-ec', script])
        self._set_default_release()
        self.apt_pin_for_releases.append(release)

    def _get_default_release(self):
        '''Get the release name which occurs first in apt sources'''
        # Note: we ignore the current value of APT::Default-Release

        # This can be set via --apt-default-release.
        if self.default_release is None:

            script = 'SRCS=$(ls /etc/apt/sources.list /etc/apt/sources.list.d/*.list 2>/dev/null|| true); '
            script += r'''sed -rn '/^(deb|deb-src) +(\[.*\] *)?(http|https|file):/ { s/\[.*\] +//; s/^[^ ]+ +[^ ]* +([^ ]+) +.*$/\1/p }' $SRCS | head -n1'''
            self.default_release = self.check_exec(['sh', '-ec', script], stdout=True).strip()

        return self.default_release

    def _set_default_release(self):
        '''Set apt's default release to enable pinning to do its job.'''
        script = 'mkdir -p /etc/apt/preferences.d; '
        # Bug 894094: in the case pockets are used, the default release has the
        # same code name as the pocket being tested. Therefore force the use of
        # the archive name to distinguish the two. In general, the default
        # release can be specified by either the code name or the archive name.
        if self.add_apt_pockets:
            script += 'printf "Package: *\\nPin: release a=%(default)s\\nPin-Priority: 990\\n" > /etc/apt/preferences.d/autopkgtest-default-release' % \
                {'default': self._get_default_release()}
        else:
            script += 'printf "Package: *\\nPin: release %(default)s\\nPin-Priority: 990\\n" > /etc/apt/preferences.d/autopkgtest-default-release' % \
                {'default': self._get_default_release()}
        self.check_exec(['sh', '-ec', script])

    def _add_apt_source(self, source, filename='autopkgtest-add-apt-sources.list'):
        adtlog.debug('adding APT source: %s' % source)
        script = '''
        echo "$1" >> /etc/apt/sources.list.d/$2
        for retry in 1 2 3; do
            apt-get --no-list-cleanup \\
                    -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/$2 \\
                    -o Dir::Etc::sourceparts=/dev/null \\
                    update 2>&1 && break || \\
                    if [ $retry = 3 ] ; then
                        return 1
                    else
                        sleep 15
                    fi
        done
        '''
        self.check_exec(['sh', '-ec', script, 'sh', source, filename])


class Path:
    '''Represent a file/dir with a host and a testbed path'''

    def __init__(self, testbed, host, tb, is_dir=None):
        '''Create a Path object.

        The object itself is just a pair of file names, nothing more. They do
        not need to exist until you call copyup() or copydown() on them.

        testbed: the Testbed object which this refers to
        host: path of the file on the host
        tb: path of the file in testbed
        is_dir: whether path is a directory; None for "unspecified" if you only
                need copydown()
        '''
        self.testbed = testbed
        self.host = host
        self.tb = tb
        self.is_dir = is_dir

    def copydown(
        self,
        check_existing=False,
        mode='',
    ) -> None:
        '''Copy file from the host to the testbed

        If check_existing is True, don't copy if the testbed path already
        exists.
        '''
        if check_existing and self.testbed.execute(['test', '-e', self.tb])[0] == 0:
            adtlog.debug('copydown: tb path %s already exists' % self.tb)
            return

        # create directory on testbed
        self.testbed.check_exec(['mkdir', '-p', os.path.dirname(self.tb)])

        if os.path.isdir(self.host):
            # directories need explicit '/' appended for VirtSubproc
            self.testbed.command('copydown', (self.host + '/', self.tb + '/'))
        else:
            self.testbed.command('copydown', (self.host, self.tb))

        # we usually want our files be readable for the non-root user
        if mode:
            self.testbed.check_exec(['chmod', '-R', mode, '--', self.tb])
        elif self.testbed.user:
            rc = self.testbed.execute(['chown', '-R', self.testbed.user, '--', self.tb],
                                      stderr=subprocess.PIPE)[0]
            if rc != 0:
                # chowning doesn't work on all shared downtmps, try to chmod
                # instead
                self.testbed.check_exec(['chmod', '-R', 'go+rwX', '--', self.tb])

    def copyup(self, check_existing=False):
        '''Copy file from the testbed to the host

        If check_existing is True, don't copy if the host path already
        exists.
        '''
        if check_existing and os.path.exists(self.host):
            adtlog.debug('copyup: host path %s already exists' % self.host)
            return

        os.makedirs(os.path.dirname(self.host), exist_ok=True, mode=0o2755)
        assert self.is_dir is not None
        if self.is_dir:
            self.testbed.command('copyup', (self.tb + '/', self.host + '/'))
        else:
            self.testbed.command('copyup', (self.tb, self.host))


class TempPath(Path):
    '''Represent a file in the hosts'/testbed's temporary directories

    These are only guaranteed to exit within one testbed run.
    '''

    # private; used to make sure the names of autocleaned files don't collide
    _filename_prefix = 1

    def __init__(self, testbed, name, is_dir=False, autoclean=True):
        '''Create a temporary Path object.

        The object itself is just a pair of file names, nothing more. They do
        not need to exist until you call copyup() or copydown() on them.

        testbed: the Testbed object which this refers to
        name: name of the temporary file (without path); host and tb
              will then be derived from that
        is_dir: whether path is a directory; None for "unspecified" if you only
                need copydown()
        autoclean: If True (default), remove file when test finishes. Should
                be set to False for files which you want to keep in the
                --output-dir which are useful for reporting results, like test
                stdout/err, log files, and binaries.
        '''
        # if the testbed supports a shared downtmp, use that to avoid
        # unnecessary copying, unless we want to permanently keep the file
        if testbed.shared_downtmp and (not testbed.output_dir or autoclean):
            host = testbed.shared_downtmp
        else:
            host = testbed.output_dir
        self.autoclean = autoclean
        if autoclean:
            name = str(self._filename_prefix) + '-' + name
            TempPath._filename_prefix += 1

        Path.__init__(self, testbed, os.path.join(host, name),
                      os.path.join(testbed.scratch, name),
                      is_dir)

    def __del__(self):
        if self.autoclean:
            if os.path.exists(self.host):
                try:
                    os.unlink(self.host)
                except OSError as e:
                    if e.errno == errno.EPERM:
                        self.testbed.check_exec(['rm', '-rf', self.tb])
                    else:
                        raise

#
# Helper functions
#


def child_ps(pid):
    '''Get all child processes of pid'''

    try:
        out = subprocess.check_output(['ps', '-o', 'pid=', '--ppid', str(pid)])
        return [int(p) for p in out.split()]
    except subprocess.CalledProcessError:
        return []


def killtree(pid):
    '''Recursively kill pid and all of its children'''

    for child in child_ps(pid):
        killtree(child)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def locate_setup_command(name: str) -> str:
    # Shortcut for shipped scripts
    if '/' not in name:
        shipped = os.path.join(PKGDATADIR, 'setup-commands', name)

        if os.path.exists(shipped):
            return shipped

    return name
