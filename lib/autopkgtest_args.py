# autopkgtest_args is part of autopkgtest
# autopkgtest is a tool for testing Debian binary packages
#
# autopkgtest is Copyright (C) 2006-2016 Canonical Ltd.
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
import re
import argparse

import adtlog
import testdesc
import adt_testbed

__all__ = ['parse_args']

actions = None


def read_changes(parser, changes):
    '''Expand a Debian .changes file into contained paths'''

    try:
        files = testdesc.parse_rfc822(changes).__next__()['Files']
    except (StopIteration, KeyError):
        parser.error('%s is invalid and does not contain Files:'
                     % changes)
    changes_dir = os.path.dirname(changes)
    paths = []
    for f in files.split():
        if f.endswith('.deb') or f.endswith('.dsc'):
            paths.append(os.path.join(changes_dir, f))
    return paths


def process_package_arguments(parser, args):
    '''Check positional arguments and produce adt_run_args compatible actions list'''

    # TODO: This should be simplified now that the old adt_run_args CLI got dropped
    # Sort action list by deb << dsc, for a "do what
    # I mean" compatible adt_run_args action list

    global actions
    debsrc_action = None
    has_debs = False

    # expand .changes files
    packages = []
    for p in args.packages:
        if p.endswith('.changes'):
            packages += read_changes(parser, p)
        else:
            packages.append(p)

    def set_debsrc(p, kind, built_bin=None):
        nonlocal debsrc_action
        if debsrc_action:
            parser.error('You must specify only one source package to test')
        debsrc_action = (kind, p, built_bin)

    for p in packages:
        if p.endswith('.deb') and os.path.exists(p):
            actions.append(('binary', p, None))
            has_debs = True
        elif p.endswith('.dsc') and os.path.exists(p):
            set_debsrc(p, 'source')
        elif re.match('[0-9a-z][0-9a-z.+-]+$', p):
            set_debsrc(p, 'apt-source', False)
        elif os.path.isfile(os.path.join(p, 'debian', 'control')):
            if os.path.exists(os.path.join(p, 'debian', 'files')):
                set_debsrc(p, 'built-tree', False)
            else:
                set_debsrc(p, 'unbuilt-tree')
        elif os.path.isfile(os.path.join(p, 'debian', 'tests', 'control')):
            # degenerate Debian source tree with only debian/tests
            set_debsrc(p, 'built-tree', False)
        elif '://' in p:
            set_debsrc(p, 'git-source')
        else:
            parser.error('%s is not a valid test package' % p)

    # if no source is given, check if the current directory is a source tree
    if not debsrc_action and os.path.isfile('debian/control'):
        if os.path.exists('debian/files'):
            set_debsrc('.', 'built-tree', False)
        else:
            set_debsrc('.', 'unbuilt-tree')

    if not debsrc_action:
        parser.error('You must specify source package to test')

    if has_debs:
        args.built_binaries = False

    if debsrc_action:
        # some actions above disable built binaries, for the rest use the CLI option
        if debsrc_action[2] is None:
            debsrc_action = (debsrc_action[0], debsrc_action[1], args.built_binaries)

        actions.append(debsrc_action)

    adtlog.debug('actions: %s' % actions)
    adtlog.debug('build binaries: %s' % args.built_binaries)


class ArgumentParser(argparse.ArgumentParser):
    '''autopkgtest ArgumentParser

    It enables include files with '@' and trims whitespace from their lines.
    '''
    def __init__(self, **kwargs):
        super(ArgumentParser, self).__init__(fromfile_prefix_chars='@',
                                             **kwargs)

    def convert_arg_line_to_args(self, arg_line):
        return [arg_line.strip()]


class AppendCommaSeparatedArg(argparse.Action):
    def __call__(self, parser, args, value, option_string=None):
        result = getattr(args, self.dest, [])
        result.extend(value.split(','))
        setattr(args, self.dest, result)


def parse_args(arglist=None):
    '''Parse autopkgtest command line arguments.

    Return (options, actions, virt-server-args).
    '''
    global actions
    actions = []

    usage = '%(prog)s [options] [testbinary ...] testsrc -- virt-server [options]'
    description = '''Test installed binary packages using the tests in testsrc.

testsrc can be one of a:
 - Debian *.dsc source package
 - Debian *.changes file containing a .dsc source package (and possibly binaries to test)
 - Debian source package directory
 - apt source package name (through apt-get source)
 - Debian source package in git (url#branchname)

You can specify local *.deb packages to test.'''

    epilog = '''The -- argument separates the autopkgtest actions and options
from the virt-server which provides the testbed. See e. g. man autopkgtest-schroot
for details.'''

    parser = argparse.ArgumentParser(
        usage=usage, description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=epilog,
        add_help=False)

    # test specification
    g_test = parser.add_argument_group('arguments for specifying and modifying the test')
    g_test.add_argument('--override-control', metavar='PATH',
                        help='run tests from control file/manifest PATH instead '
                        'of the source package')
    g_test.add_argument('--test-name',
                        dest='only_tests', action='append', default=[],
                        help='run only given test name (repeatable). '
                        'This replaces --testname, which is deprecated.')
    # Don't display the deprecated argument name in the --help output.
    g_test.add_argument('--testname', dest='only_tests', action='append',
                        help=argparse.SUPPRESS)
    g_test.add_argument('--skip-test',
                        dest='skip_tests', action='append',
                        help='skip given test name (repeatable).')
    g_test.add_argument('-B', '--no-built-binaries', dest='built_binaries',
                        action='store_false', default=True,
                        help='do not build/use binaries from .dsc, git source, or unbuilt tree')
    g_test.add_argument('packages', nargs='*',
                        help='testsrc source package and testbinary packages as above')

    # logging
    g_log = parser.add_argument_group('logging options')
    g_log.add_argument('-o', '--output-dir',
                       help='Write test artifacts (stdout/err, log, debs, etc)'
                       ' to OUTPUT-DIR (must not exist or be empty)')
    g_log.add_argument('-l', '--log-file', dest='logfile',
                       help='Write the log LOGFILE, emptying it beforehand,'
                       ' instead of using OUTPUT-DIR/log')
    g_log.add_argument('--summary-file', dest='summary',
                       help='Write a summary report to SUMMARY, emptying it '
                       'beforehand')
    g_log.add_argument('-q', '--quiet', action='store_const', dest='verbosity',
                       const=0, default=1,
                       help='Suppress all messages from %(prog)s itself '
                       'except for the test results')

    # test bed setup
    g_setup = parser.add_argument_group('test bed setup options')
    g_setup.add_argument('--setup-commands', metavar='COMMANDS_OR_PATH',
                         action='append', default=[],
                         help='Run these commands after opening the testbed '
                         '(e. g. "apt-get update" or adding apt sources); '
                         'can be a string with the commands, or a file '
                         'containing the commands')
    # Ensure that this fails with something other than 100 in most error cases,
    # as apt-get update failures are usually transient; but if we find a
    # nonexisting apt source (404 Not Found) we *do* want 100, as otherwise
    # we'd get eternally looping tests.
    g_setup.add_argument('-U', '--apt-upgrade', dest='setup_commands',
                         action='append_const',
                         const='''(O=$(bash -o pipefail -ec 'apt-get update | tee /proc/self/fd/2') ||'''
                         '{ [ "${O%404*Not Found*}" = "$O" ] || exit 100; sleep 15; apt-get update; }'
                         ' || { sleep 60; apt-get update; } || false)'
                         ' && $(command -v eatmydata || true) apt-get dist-upgrade -y -o '
                         'Dpkg::Options::="--force-confnew"'
                         ' && $(command -v eatmydata || true) apt-get --purge autoremove -y',
                         help='Run apt update/dist-upgrade before the tests')
    g_setup.add_argument('--setup-commands-boot', metavar='COMMANDS_OR_PATH',
                         action='append', default=[],
                         help='Run these commands after --setup-commands, '
                         'and also every time the testbed is rebooted')
    g_setup.add_argument('--add-apt-source', action='append',
                         dest='add_apt_sources',
                         metavar='"deb http://MIRROR SUITE COMPONENT..."',
                         default=[],
                         help='Enable additional apt sources')
    g_setup.add_argument('--add-apt-release', action='append',
                         dest='add_apt_releases',
                         metavar='"RELEASE"',
                         default=[],
                         help='Add sources.list entries for RELEASE')
    g_setup.add_argument('--pin-packages', action='append',
                         dest='pin_packages',
                         metavar='RELEASE=pkgname,src:srcname,...',
                         default=[],
                         help='Enable testing with packages from a different release. '
                         'Sets up apt pinning to use only those packages from RELEASE; '
                         'src:srcname expands to all binaries of srcname.')
    g_setup.add_argument('--apt-pocket', action='append',
                         metavar='POCKETNAME[=pkgname,src:srcname,...]',
                         default=[],
                         help='Enable additional apt source for release-POCKETNAME. '
                         'If packages are given, set up apt pinning to use '
                         'only those packages from release-POCKETNAME; src:srcname '
                         ' expands to all binaries of srcname')
    g_setup.add_argument('--apt-default-release',
                         dest='apt_default_release',
                         metavar='SUITENAME_OR_CODENAME',
                         help='Define the default release for apt. For apt pinning to work '
                         'properly, the default release must be set to the release '
                         'that should provide the packages that are not pinned. For '
                         'Debian and Ubuntu, this is normally automatically detected '
                         'from the first entry in /etc/apt/sources.list.')
    g_setup.add_argument('--no-apt-fallback',
                         dest='enable_apt_fallback',
                         action='store_false',
                         default=True,
                         help='Disable the apt fallback which is used with --apt-pocket or '
                         '--pin-packages in case installation of dependencies fails due to '
                         'strict pinning')
    g_setup.add_argument('--copy', metavar='HOSTFILE:TESTBEDFILE',
                         action='append', default=[],
                         help='Copy file or dir from host into testbed after '
                         'opening')
    g_setup.add_argument('--env', metavar='VAR=value',
                         action='append', default=[],
                         help='Set arbitrary environment variable for builds and test')
    g_setup.add_argument('--ignore-restrictions', default=[],
                         metavar='RESTRICTION[,RESTRICTION...]',
                         action=AppendCommaSeparatedArg,
                         help='Run tests even if these restrictions would '
                         'normally prevent it')

    # privileges
    g_priv = parser.add_argument_group('user/privilege handling options')
    g_priv.add_argument('-u', '--user',
                        help='run tests as USER (needs root on testbed)')
    g_priv.add_argument('--gain-root', dest='gainroot',
                        help='Command to gain root during package build, '
                        'passed to dpkg-buildpackage -r')

    # debugging
    g_dbg = parser.add_argument_group('debugging options')
    g_dbg.add_argument('-d', '--debug', action='store_const', dest='verbosity',
                       const=2,
                       help='Show lots of internal autopkgtest debug messages')
    g_dbg.add_argument('-s', '--shell-fail', action='store_true',
                       help='Run a shell in the testbed after any failed '
                       'build or test')
    g_dbg.add_argument('--shell', action='store_true',
                       help='Run a shell in the testbed after every test')

    # timeouts
    g_time = parser.add_argument_group('timeout options')
    for k, v in adt_testbed.timeouts.items():
        g_time.add_argument(
            '--timeout-' + k, type=int, dest='timeout_' + k, metavar='T',
            help='set %s timeout to T seconds (default: %us)' %
            (k, v))
    g_time.add_argument(
        '--timeout-factor', type=float, metavar='FACTOR', default=1.0,
        help='multiply all default timeouts by FACTOR')

    # locale
    g_loc = parser.add_argument_group('locale options')
    g_loc.add_argument('--set-lang', metavar='LANGVAL',
                       help='set LANG on testbed to LANGVAL '
                       '(default: C.UTF-8')

    # misc
    g_misc = parser.add_argument_group('other options')
    g_misc.add_argument(
        '--no-auto-control', dest='auto_control', action='store_false',
        default=True,
        help='Disable automatic test generation with autodep8')
    g_misc.add_argument('--build-parallel', metavar='N',
                        help='Set "parallel=N" DEB_BUILD_OPTION for building '
                        'packages (default: number of available processors)')
    g_misc.add_argument(
        '--needs-internet', dest='needs_internet',
        choices=['run', 'try', 'skip'],
        default='run',
        help='Define how to handle the needs-internet restriction. With "try" '
        'tests with needs-internet restrictions will be run, but if they fail '
        'they will be treated as flaky tests. With "skip" these tests will be '
        'skipped immediately and will not be run. With "run" the restriction '
        'is basically ignored.')
    g_misc.add_argument(
        '-V', '--validate', action='store_true', default=False,
        help='validate the test control file and exit')
    g_misc.add_argument(
        '-h', '--help', action='help', default=argparse.SUPPRESS,
        help='show this help message and exit')

    # first, expand argument files
    file_parser = ArgumentParser(add_help=False)
    arglist = file_parser.parse_known_args(arglist)[1]

    # deprecation warning
    if '--testname' in arglist:
        adtlog.warning('--testname is deprecated; use --test-name')

    # split off virt-server args
    try:
        sep = arglist.index('--')
    except ValueError:
        # backwards compatibility: allow three dashes
        try:
            sep = arglist.index('---')
            adtlog.warning('Using --- to separate virt server arguments is deprecated; use -- instead')
        except ValueError:
            # still allow --help
            sep = None
            virt_args = None
    if sep is not None:
        virt_args = arglist[sep + 1:]
        arglist = arglist[:sep]

    # parse autopkgtest options
    args = parser.parse_args(arglist)
    adtlog.verbosity = args.verbosity
    adtlog.debug('autopkgtest options: %s' % args)
    adtlog.debug('virt-runner arguments: %s' % virt_args)

    if not virt_args:
        parser.error('You must specify -- <virt-server>...')

    # autopkgtest-virt-* prefix can be skipped
    if virt_args and '/' not in virt_args[0] and not virt_args[0].startswith('autopkgtest-virt-'):
        virt_args[0] = 'autopkgtest-virt-' + virt_args[0]

    process_package_arguments(parser, args)

    # verify --env validity
    for e in args.env:
        if '=' not in e:
            parser.error('--env must be KEY=value')

    if args.set_lang:
        args.env.append('LANG=' + args.set_lang)

    # set (possibly adjusted) timeout defaults
    for k in adt_testbed.timeouts:
        v = getattr(args, 'timeout_' + k)
        if v is None:
            adt_testbed.timeouts[k] = int(adt_testbed.timeouts[k] * args.timeout_factor)
        else:
            adt_testbed.timeouts[k] = v

    # this timeout is for the virt server, so pass it down via environment
    os.environ['AUTOPKGTEST_VIRT_COPY_TIMEOUT'] = str(adt_testbed.timeouts['copy'])

    # if we have --setup-commands and it points to a file, read its contents
    for i, c in enumerate(args.setup_commands):
        c = adt_testbed.locate_setup_command(c)
        if os.path.exists(c):
            with open(c, encoding='UTF-8') as f:
                args.setup_commands[i] = f.read().strip()

    for i, c in enumerate(args.setup_commands_boot):
        c = adt_testbed.locate_setup_command(c)
        if os.path.exists(c):
            with open(c, encoding='UTF-8') as f:
                args.setup_commands_boot[i] = f.read().strip()

    # parse --copy arguments
    copy_pairs = []
    for arg in args.copy:
        try:
            (host, tb) = arg.split(':', 1)
        except ValueError:
            parser.error('--copy argument must be HOSTPATH:TESTBEDPATH: %s'
                         % arg)
        if not os.path.exists(host):
            parser.error('--copy host path %s does not exist' % host)
        copy_pairs.append((host, tb))
    args.copy = copy_pairs

    return (args, actions, virt_args)
