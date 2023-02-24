# testdesc is part of autopkgtest
# autopkgtest is a tool for testing Debian binary packages
#
# autopkgtest is Copyright (C) 2006-2014 Canonical Ltd.
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

import string
import re
import errno
import os.path
import subprocess
import tempfile
from typing import (Dict, FrozenSet, Iterable, List, Optional, Union)

import debian.deb822
import debian.debian_support
import debian.debfile

import adtlog

#
# Abstract test representation
#

known_restrictions = frozenset({
    'allow-stderr',
    'breaks-testbed',
    'build-needed',
    'flaky',
    'isolation-container',
    'isolation-machine',
    'needs-internet',
    'needs-reboot',
    'needs-recommends',
    'needs-root',
    'needs-sudo',
    'rw-build-tree',
    'skip-not-installable',
    'skippable',
    'superficial',
})


# Keys are restrictions
# Values are sets of either:
# - str: a capability that is required by the restriction
# - set of two or more str: at least one of these caps is required
#   by the restriction
RESTRICTIONS_REQUIRE_CAPS = {
    'breaks-testbed': frozenset({'revert-full-system'}),
    'needs-internet': frozenset({'has_internet'}),
    'needs-reboot': frozenset({'reboot'}),
    'needs-root': frozenset({'root-on-testbed'}),
    'isolation-container': frozenset({
        frozenset({'isolation-container', 'isolation-machine'}),
    }),
    'isolation-machine': frozenset({'isolation-machine'}),
}       # type: Dict[str, FrozenSet[Union[str, FrozenSet[str]]]]


# Keys are restrictions
# Values are sets of dependency strings
RESTRICTIONS_IMPLY_DEPS = {
    'needs-sudo': frozenset({'sudo'}),
}


class Unsupported(Exception):
    '''Test cannot be run in the testbed'''

    def __init__(self, testname, message):
        self.testname = testname
        self.message = message

    def __str__(self):
        return 'Unsupported test %s: %s' % (self.testname, self.message)

    def report(self):
        adtlog.report(self.testname, 'SKIP %s' % self.message)


class InvalidControl(Exception):
    '''Test has invalid control data'''

    def __init__(self, testname, message):
        self.testname = testname
        self.message = message

    def __str__(self):
        return 'InvalidControl test %s: %s' % (self.testname, self.message)

    def report(self):
        adtlog.report(self.testname, 'BROKEN %s' % self.message)


class Test:
    '''Test description.

    This is only a representation of the metadata, it does not have any
    actions.
    '''
    def __init__(
        self,
        name: str,
        path: Optional[str],
        command: Optional[str],
        restrictions: Iterable[str],
        features: Iterable[str],
        depends: List[str],
        synth_depends: List[str],
    ) -> None:
        '''Create new test description

        A test must have either "path" or "command", the respective other value
        must be None.

        @name: Test name
        @path: path to the test's executable, relative to source tree
        @command: shell command for the test code
        @restrictions, @features: sets of strings, as in README.package-tests
        @depends: string list of test dependencies (packages)
        @synth_depends: string list of synthesized test dependencies (packages)
        '''
        if '/' in name:
            raise Unsupported(name, 'test name may not contain / character')

        if not ((path is None) ^ (command is None)):
            raise InvalidControl(name, 'Test must have either path or command')

        self.name = name
        self.path = path
        self.command = command
        self.restrictions = set(restrictions)
        self.features = set(features)
        self.depends = list(depends)
        self.synth_depends = synth_depends

        for r in self.restrictions:
            for d in RESTRICTIONS_IMPLY_DEPS.get(r, frozenset()):
                if d not in self.depends:
                    self.depends.append(d)

        # None while test hasn't run yet; True: pass, False: fail
        self.result = None
        self.skipped = False
        adtlog.debug('Test defined: name %s path %s command "%s" '
                     'restrictions %s features %s depends %s ' %
                     (name, path, command, sorted(restrictions),
                      sorted(features), depends))

    def passed(self):
        '''Mark test as passed'''

        self.result = True
        if 'superficial' in self.restrictions:
            adtlog.report(self.name, 'PASS (superficial)')
        else:
            adtlog.report(self.name, 'PASS')

    def set_skipped(self, reason):
        '''Mark test as skipped'''
        # This isn't called skipped() to avoid clashing with the boolean
        # attribute.

        self.skipped = True
        self.result = True
        adtlog.report(self.name, 'SKIP ' + reason)

    def failed(self, reason):
        '''Mark test as failed'''

        self.result = False
        if 'flaky' in self.restrictions:
            adtlog.report(self.name, 'FLAKY ' + reason)
        else:
            adtlog.report(self.name, 'FAIL ' + reason)

    def check_testbed_compat(self, caps, ignore_restrictions=()):
        '''Check for restrictions incompatible with test bed capabilities.

        Raise Unsupported exception if there are any.
        '''
        effective = set(self.restrictions) - set(ignore_restrictions)
        provided = set(caps)

        for r in effective:
            if r not in known_restrictions:
                raise Unsupported(self.name, 'unknown restriction %s' % r)

        for r in effective:
            need_caps = RESTRICTIONS_REQUIRE_CAPS.get(r, frozenset())

            for c in need_caps:
                if isinstance(c, str):
                    if c not in caps:
                        raise Unsupported(
                            self.name,
                            ('Test restriction "%s" requires testbed '
                             'capability "%s"') % (r, c),
                        )
                else:
                    # must have at least one of the capabilities
                    assert isinstance(c, frozenset)
                    assert len(c) > 1

                    if not (c & provided):
                        cap_names = sorted('"%s"' % n for n in c)
                        needed = ', '.join(cap_names[:-1])
                        needed += ' and/or ' + cap_names[-1]

                        raise Unsupported(
                            self.name,
                            ('Test restriction "%s" requires testbed '
                             'capability %s') % (r, needed),
                        )


#
# Parsing for Debian source packages
#


def parse_rfc822(path):
    '''Parse Debian-style RFC822 file

    Yield dictionaries with the keys/values.
    '''
    try:
        f = open(path, encoding='UTF-8')
    except (IOError, OSError) as oe:
        if oe.errno != errno.ENOENT:
            raise
        return

    # filter out comments, python-debian doesn't do that
    # (http://bugs.debian.org/743174)
    lines = []
    for line in f:
        # completely ignore ^# as that breaks continuation lines
        if line.startswith('#'):
            continue
        # filter out comments which don't start on first column (Debian
        # #743174); entirely remove line if all that's left is whitespace, as
        # that again breaks continuation lines
        if '#' in line:
            line = line.split('#', 1)[0]
            if not line.strip():
                continue
        lines.append(line)
    f.close()

    for p in debian.deb822.Deb822.iter_paragraphs(lines):
        r = {}
        for field, value in p.items():
            # un-escape continuation lines
            v = ''.join(value.split('\n')).replace('  ', ' ')
            field = string.capwords(field)
            r[field] = v
        yield r


def _debian_check_unknown_fields(name, record):
    unknown_keys = set(record.keys()).difference(
        {'Tests', 'Test-command', 'Restrictions', 'Features',
         'Depends', 'Tests-directory', 'Classes', 'Architecture'})
    if unknown_keys:
        raise Unsupported(name, 'unknown field %s' % unknown_keys.pop())


def _debian_packages_from_source(srcdir):
    packages = []
    packages_no_arch = []

    for st in parse_rfc822(os.path.join(srcdir, 'debian/control')):
        if 'Package' not in st:
            # source stanza
            continue
        # filter out udebs and similar stuff which aren't "real" debs
        if st.get('Xc-package-type', 'deb') != 'deb' or \
                st.get('Package-type', 'deb') != 'deb':
            continue
        arch = st['Architecture']
        if arch in ('all', 'any'):
            packages.append(st['Package'])
        else:
            packages.append('%s [%s]' % (st['Package'], arch))
        packages_no_arch.append(st['Package'])

    return (packages, packages_no_arch)


def _filter_variables(deps):
    deplist = []
    for dep in [d.strip() for d in deps.split(',')]:
        if re.match(r'^\${', dep):
            continue
        dep = re.sub(r'\(.*\${\S*}.*\)', '', dep)
        deplist.append(dep)
    return ', '.join(deplist)


def _debian_recommends_from_source(srcdir, testbed_arch):
    deps = ''
    for st in parse_rfc822(os.path.join(srcdir, 'debian/control')):
        if 'Recommends' in st:
            deps += st['Recommends'] + ','

    # Because we parse the source d/control file instead of the binary
    # control file, we may run into variables. See bug 1008206 for
    # some discussion.
    deps = _filter_variables(deps)

    # resolve arch specific dependencies and build profiles
    perl = subprocess.Popen(['perl', '-'], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    code = '''use Dpkg::Deps;
              $dep = deps_parse('%s', reduce_restrictions => 1,
                                host_arch => '%s');
              $out = $dep->output();
              print $out, "\\n";
              ''' % (deps, testbed_arch)
    deps = perl.communicate(code.encode('UTF-8'))[0].decode('UTF-8').strip()
    if perl.returncode != 0:
        raise InvalidControl('source', 'Invalid Recommends')

    deps = [d.strip() for d in deps.split(',')]

    # The following check is a bit silly as there were apparently no
    # recommends, but still the test asked for it. Let's not fail on
    # that, but also let's not add the empty string.
    if deps == ['']:
        deps = []

    return deps


def _debian_build_deps_from_source(srcdir, testbed_arch):
    deps = ''
    for st in parse_rfc822(os.path.join(srcdir, 'debian/control')):
        if 'Build-depends' in st:
            deps += st['Build-depends']
        if 'Build-depends-indep' in st:
            deps += ', ' + st['Build-depends-indep']
        if 'Build-depends-arch' in st:
            deps += ', ' + st['Build-depends-arch']

    # resolve arch specific dependencies and build profiles
    perl = subprocess.Popen(['perl', '-'], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    code = '''use Dpkg::Deps;
              $supports_profiles = ($Dpkg::Deps::VERSION gt '1.04');
              $dep = deps_parse('%s', reduce_arch => 1,
                                reduce_profiles => $supports_profiles,
                                build_dep => 1, host_arch => '%s');
              $out = $dep->output();
              # fall back to ignoring build profiles
              $out =~ s/ <[^ >]+>//g if (!$supports_profiles);
              print $out, "\\n";
              ''' % (deps, testbed_arch)
    deps = perl.communicate(code.encode('UTF-8'))[0].decode('UTF-8').strip()
    if perl.returncode != 0:
        raise InvalidControl('source', 'Invalid build dependencies')

    deps = [d.strip() for d in deps.split(',')]

    # @builddeps@ should always imply build-essential
    deps.append('build-essential')
    return deps


dep_re = re.compile(
    r'(?P<package>[a-z0-9+-.]+)(?::[a-z0-9_-]+)?\s*'
    r'(\((?P<relation><<|<=|>=|=|>>)\s*(?P<version>[^\)]*)\))?'
    r'(\s*\[(?P<arch>[a-z0-9+-.! ]+)\])?$')


def _debian_check_dep(testname, dep):
    '''Check a single Debian dependency'''

    dep = dep.strip()
    m = dep_re.match(dep)
    if not m:
        raise InvalidControl(testname, "Test Depends field contains an "
                             "invalid dependency `%s'" % dep)
    if m.group("version"):
        try:
            debian.debian_support.NativeVersion(m.group('version'))
        except ValueError:
            raise InvalidControl(testname, "Test Depends field contains "
                                 "dependency `%s' with an "
                                 "invalid version" % dep)
        except AttributeError:
            # too old python-debian, skip the check
            pass

    return (m.group('package'), m.group('version'))


def _synthesize_deps(dep, testbed_arch):
    '''Ensure that apt can install synthesized Depends

    Test Depends may have architecture qualifiers we need to check,
    because apt command-line can't handle those and can't do the check.
    (Policy says the architecture qualifier should not be in the binary
    control file). We'll ignore the version here.
    '''

    dep = dep.strip()
    m = dep_re.match(dep)

    arch_matches = 'y'
    if m.group('arch') is not None:
        arch_matches = False
        perl = subprocess.Popen(['perl', '-'], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE)
        code = '''use Dpkg::Deps;
                  print deps_parse('foo [%s]')->arch_is_concerned('%s') ? 'y' : 'n';
                  ''' % (m.group('arch'), testbed_arch)
        arch_matches = perl.communicate(code.encode('UTF-8'))[0].decode('UTF-8').strip()
        if perl.returncode != 0:
            raise InvalidControl('source', 'Invalid (Test-)Depends architecture qualifiers')

    if arch_matches == 'y':
        return m.group('package')
    else:
        return None


def _parse_debian_depends(testname, dep_str, srcdir, testbed_arch):
    '''Parse Depends: line in a Debian package

    Split dependencies (comma separated), validate their syntax, and expand @,
    @builddeps@ and @recommends@. Return a list of dependencies.

    This may raise an InvalidControl exception if there are invalid
    dependencies.
    '''
    deps = []
    synthdeps = []
    (my_packages, my_packages_no_arch) = _debian_packages_from_source(srcdir)
    for alt_group_str in dep_str.split(','):
        alt_group_str = alt_group_str.strip()
        if not alt_group_str:
            # happens for empty depends or trailing commas
            continue
        adtlog.debug('processing dependency %s' % alt_group_str)
        if alt_group_str == '@':
            for d in my_packages:
                adtlog.debug('synthesised dependency %s' % d)
                deps.append(d)
                s = _synthesize_deps(d, testbed_arch)
                if s:
                    synthdeps.append(s)
        elif alt_group_str == '@builddeps@':
            for d in _debian_build_deps_from_source(srcdir, testbed_arch):
                adtlog.debug('synthesised dependency %s' % d)
                deps.append(d)
        elif alt_group_str == '@recommends@':
            for d in _debian_recommends_from_source(srcdir, testbed_arch):
                adtlog.debug('synthesised dependency %s' % d)
                deps.append(d)
        else:
            synthdep_alternatives = []
            for dep in alt_group_str.split('|'):
                (pkg, version) = _debian_check_dep(testname, dep)
                if pkg not in my_packages_no_arch:
                    synthdep_alternatives = []
                    break
                s = _synthesize_deps(dep, testbed_arch)
                if s:
                    synthdep_alternatives.append(s)
            if synthdep_alternatives:
                adtlog.debug('marked alternatives %s as a synthesised dependency' % synthdep_alternatives)
                if len(synthdep_alternatives) > 1:
                    synthdeps.append(synthdep_alternatives)
                else:
                    synthdeps.append(synthdep_alternatives[0])
            deps.append(alt_group_str)

    return (deps, synthdeps)


def _autodep8(srcdir):
    '''Generate control file with autodep8'''

    f = tempfile.NamedTemporaryFile(prefix='autodep8.')
    try:
        autodep8 = subprocess.Popen(['autodep8'], cwd=srcdir, stdout=f,
                                    stderr=subprocess.PIPE)
    except OSError as e:
        adtlog.debug('autodep8 not available (%s)' % e)
        return None

    err = autodep8.communicate()[1].decode()
    if autodep8.returncode == 0:
        f.flush()
        f.seek(0)
        ctrl = f.read().decode()
        adtlog.debug('autodep8 generated control: -----\n%s\n-------' % ctrl)
        return f

    f.close()
    adtlog.debug('autodep8 failed to generate control (exit status %i): %s' %
                 (autodep8.returncode, err))
    return None


def _matches_architecture(host_arch, arch_wildcard):
    try:
        subprocess.check_call(['perl', '-mDpkg::Arch', '-e',
                               'exit(!Dpkg::Arch::debarch_is(shift, shift))',
                               host_arch, arch_wildcard])
    except subprocess.CalledProcessError as e:
        # returns 1 if host_arch is not matching arch_wildcard; other
        # errors shouldn't be ignored
        if e.returncode != 1:
            raise
        return False
    return True


def _check_architecture(name, testbed_arch, architectures):
    '''Check if testbed_arch is supported by the architectures

    The architecture list comes in two variants, positive: only this
    arch is supported (arch may be a wildcard) and negative: this arch
    is not supported (arch may be a wildcard). If there is any
    positive arch, every arch not explicitly listed is skipped. Debian
    Policy 7.1 explains that for (Build-)Depends it's not allowed to
    mix positive and negative, so let's not do either. The list can
    also be empty. Empty and ["any"] are the same, "all" isn't
    allowed.
    '''

    if "all" in architectures:
        raise Unsupported(name, "Arch 'all' not allowed in Architecture field")

    if len(architectures) == 0 or architectures == ["any"]:
        return

    any_negative = False
    any_positive = False
    for arch in architectures:
        if arch[0] == "!":
            any_negative = True
            if _matches_architecture(testbed_arch, arch[1:]):
                raise Unsupported(name, "Test declares architecture as not " +
                                  "supported: %s" % testbed_arch)
        if arch[0] != "!":
            any_positive = True

    if any_positive:
        if any_negative:
            raise Unsupported(name, "It is not permitted for some archs to " +
                              "be prepended by an exclamation mark while " +
                              "others aren't")
        arch_matched = False
        for arch in architectures:
            if _matches_architecture(testbed_arch, arch):
                arch_matched = True

        if not arch_matched:
            raise Unsupported(name, "Test lists explicitly supported " +
                              "architectures, but the current architecture " +
                              "%s isn't listed." % testbed_arch)


def parse_debian_source(srcdir, testbed_caps, testbed_arch, control_path=None,
                        auto_control=True, ignore_restrictions=(),
                        only_tests=()):
    '''Parse test descriptions from a Debian DEP-8 source dir

    @ignore_restrictions: If we would skip the test due to these restrictions,
                          run it anyway

    You can specify an alternative path for the control file (default:
    srcdir/debian/tests/control).

    Return (list of Test objects, some_skipped). If this encounters any invalid
    restrictions, fields, or test restrictions which cannot be met by the given
    testbed capabilities, the test will be skipped (and reported so), and not
    be included in the result.

    This may raise an InvalidControl exception.
    '''
    some_skipped = False
    command_counter = 0
    tests = []
    if not control_path:
        control_path = os.path.join(srcdir, 'debian', 'tests', 'control')
        dtc_exists = os.path.exists(control_path)
        try_autodep8 = False

        if auto_control:
            if not dtc_exists:
                try_autodep8 = True
            else:
                dcontrol_path = os.path.join(srcdir, 'debian', 'control')
                for record in parse_rfc822(dcontrol_path):
                    testsuite = record.get('Testsuite', '')
                    if 'autopkgtest-pkg-' in testsuite:
                        try_autodep8 = True
                    # We only want to look at the source section
                    break

        if try_autodep8:
            control = _autodep8(srcdir)
            if control is not None:
                control_path = control.name
            elif not dtc_exists:
                return ([], False)
        elif not dtc_exists:
            adtlog.debug('auto_control is disabled, and no regular tests')
            return ([], False)

    for record in parse_rfc822(control_path):
        command = None
        try:
            restrictions = set(
                record.get('Restrictions', '').replace(',', ' ').split()
            )

            # needs-recommends is deprecated, but until removed, let's
            # replace it with @recommends@
            if 'needs-recommends' in restrictions:
                record['Depends'] = record.get('Depends', '@') + ',@recommends@'

            feature_test_name = None
            features = set()
            record_features = record.get('Features', '').replace(
                ',', ' ').split()
            for feature in record_features:
                details = feature.split('=', 1)
                if details[0] != 'test-name':
                    features.add(feature)
                    continue
                if len(details) != 2:
                    # No value, i.e. a bare 'test-name'
                    raise InvalidControl(
                        '*', 'test-name feature with no argument')
                if feature_test_name is not None:
                    raise InvalidControl(
                        '*', 'only one test-name feature allowed')
                feature_test_name = details[1]
                features.add(feature)
            architectures = record.get('Architecture', '').replace(
                ',', ' ').split()

            if 'Tests' in record:
                test_names = record['Tests'].replace(',', ' ').split()
                if len(test_names) == 0:
                    raise InvalidControl('*', '"Tests" field is empty')
                (depends, synth_depends) = _parse_debian_depends(
                    test_names[0],
                    record.get('Depends', '@'),
                    srcdir,
                    testbed_arch)
                if 'Test-command' in record:
                    raise InvalidControl('*', 'Only one of "Tests" or '
                                         '"Test-Command" may be given')
                if feature_test_name is not None:
                    raise InvalidControl(
                        '*', 'test-name feature incompatible with Tests')
                test_dir = record.get('Tests-directory', 'debian/tests')

                for n in test_names:
                    try:
                        _debian_check_unknown_fields(n, record)
                        _check_architecture(n, testbed_arch, architectures)

                        test = Test(n, os.path.join(test_dir, n), None,
                                    restrictions, features, depends, synth_depends)
                        test.check_testbed_compat(testbed_caps, ignore_restrictions)
                    except Unsupported as u:
                        if (not only_tests) or n in only_tests:
                            u.report()
                            some_skipped = True
                    else:
                        tests.append(test)
            elif 'Test-command' in record:
                command = record['Test-command']
                (depends, synth_depends) = _parse_debian_depends(
                    command,
                    record.get('Depends', '@'),
                    srcdir,
                    testbed_arch)
                if feature_test_name is None:
                    command_counter += 1
                    name = 'command%i' % command_counter
                else:
                    name = feature_test_name
                _debian_check_unknown_fields(name, record)
                _check_architecture(name, testbed_arch, architectures)
                test = Test(name, None, command, restrictions, features,
                            depends, synth_depends)
                test.check_testbed_compat(testbed_caps, ignore_restrictions)
                tests.append(test)
            else:
                raise InvalidControl('*', 'missing "Tests" or "Test-Command"'
                                     ' field')
        except Unsupported as u:
            if not only_tests:
                u.report()
                some_skipped = True

    return (tests, some_skipped)
