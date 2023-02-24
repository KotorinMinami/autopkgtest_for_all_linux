Autopkgtest - Defining tests for Debian packages
================================================

This document describes how autopkgtest interprets and executes tests
found in Debian source packages.

Overview
--------

The source package provides a test metadata file
``debian/tests/control``. This enumerates the tests and specifies their
dependencies and requirements for the testbed. It contains zero or more
RFC822-style stanzas, along these lines:

::

    Tests: fred, bill, bongo
    Depends: pkg1, pkg2 [amd64] | pkg3 (>= 3)
    Restrictions: needs-root, breaks-testbed

This example defines three tests, called ``fred``, ``bill`` and
``bongo``. The tests will be performed by executing
``debian/tests/fred``, ``debian/tests/bill``, etc. Each test program
should, on success, exit with status 0 and print nothing to stderr; if a
test exits nonzero, or prints to stderr, it is considered to have
failed.

The cwd of each test is guaranteed to be the root of the source package,
which will have been unpacked but not built. *However* note that the
tests must test the *installed* version of the package, as opposed to
programs or any other file from the built tree. Tests may not modify the
source tree (and may not have write access to it).

If the file to be executed has no execute bits set, ``chmod a+x`` is
applied to it (this means that tests can be added in patches without the
need for additional chmod; contrast this with debian/rules).

During execution of the test, the environment variable
``$AUTOPKGTEST_TMP`` will point to a directory for the execution of this
particular test, which starts empty and will be deleted afterwards (so
there is no need for the test to clean up files left there).

Tests can expect that the ``$HOME`` environment variable to be set
to a directory that exists and is writeable by the user running the test.

If tests want to create artifacts which are useful to attach to test
results, such as additional log files or screenshots, they can put them
into the directory specified by the ``$AUTOPKGTEST_ARTIFACTS``
environment variable. When using the ``--output-dir`` option, they will
be copied into ``outputdir/artifacts/``.

Tests must declare all applicable Restrictions - see below.

The '#' character introduces a comment. Everything from '#' to the end
of line will be entirely ignored.

Examples
--------
Simplest possible control file that installs all of the source package's
binary packages and runs the standalone ``debian/tests/smoke`` test
script as user, without any further limitations:

::

    Tests: smoke


Control file for an inline test command that just calls a program
``foo-cli`` from package ``foo`` (which usually should be a binary of
the source package that contains this test control) as root, and makes
sure it exits with zero:

::

    Test-Command: foo-cli --list --verbose
    Depends: foo
    Restrictions: needs-root

Control fields
--------------

The fields which may appear in debian/tests/control stanzas are:

Tests: name-of-test [, name-of-another-test ...]
    This field names the tests which are defined by this stanza, and map
    to executables/scripts in the test directory. All of the other
    fields in the same stanza apply to all of the named tests. Either
    this field or ``Test-Command:`` must be present.

    Test names are separated by comma and/or whitespace and should
    contain only characters which are legal in package names. It is
    permitted, but not encouraged, to use upper-case characters as well.

Test-Command: shell command
    If your test only contains a shell command or two, or you want to
    re-use an existing upstream test executable and just need to wrap it
    with some command like ``dbus-launch`` or ``env``, you can use this
    field to specify the shell command directly. It will be run under
    ``bash -e``. This is mutually exclusive with the ``Tests:`` field.

    This is also useful for running the same script under different
    interpreters and/or with different dependencies, such as
    ``Test-Command: python debian/tests/mytest.py`` and
    ``Test-Command: python3 debian/tests/mytest.py``.

Restrictions: restriction-name [, another-restriction-name ...]
    Declares some restrictions or problems with the tests defined in
    this stanza. Depending on the test environment capabilities, user
    requests, and so on, restrictions can cause tests to be skipped or
    can cause the test to be run in a different manner. Tests which
    declare unknown restrictions will be skipped. See below for the
    defined restrictions.

    Restrictions are separated by commas and/or whitespace.

Features: feature-name [, another-feature-name ...]
    Declares some additional capabilities or good properties of the
    tests defined in this stanza. Any unknown features declared will be
    completely ignored. See below for the defined features.

    Features are separated by commas and/or whitespace.

Depends: dpkg dependency field syntax
    Declares that the specified packages must be installed for the test
    to go ahead. This supports all features of dpkg dependencies, including
    the architecture qualifiers (see
    https://www.debian.org/doc/debian-policy/ch-relationships.html),
    plus the following extensions:

    ``@`` stands for the package(s) generated by the source package
    containing the tests; each dependency (strictly, or-clause, which
    may contain ``|``\ s but not commas) containing ``@`` is replicated
    once for each such binary package, with the binary package name
    substituted for each ``@`` (but normally ``@`` should occur only
    once and without a version restriction).

    ``@builddeps@`` will be replaced by the package's
    ``Build-Depends:``, ``Build-Depends-Indep:``, ``Build-Depends-Arch:``, and
    ``build-essential``. This is useful if you have many build
    dependencies which are only necessary for running the test suite and
    you don't want to replicate them in the test ``Depends:``. However,
    please use this sparingly, as this can easily lead to missing binary
    package dependencies being overlooked if they get pulled in via
    build dependencies.

    ``@recommends@`` stands for all the packages listed in the
    ``Recommends:`` fields of all the binary packages mentioned in the
    ``debian/control`` file. Please note that variables are stripped,
    so if some required test dependencies aren't explicitly mentioned,
    they may not be installed.

    If no Depends field is present, ``Depends: @`` is assumed. Note that
    the source tree's Build-Dependencies are *not* necessarily
    installed, and if you specify any Depends, no binary packages from
    the source are installed unless explicitly requested.

Tests-Directory: path
    Replaces the path segment ``debian/tests`` in the filenames of the
    test programs with ``path``. I. e., the tests are run by executing
    ``built/source/tree/path/testname``. ``path`` must be a relative
    path and is interpreted starting from the root of the built source
    tree.

    This allows tests to live outside the debian/ metadata area, so that
    they can more palatably be shared with non-Debian distributions.

Classes: class-1 [, class-2 ...]
    Most package tests should work in a minimal environment and are
    usually not hardware specific. However, some packages like the
    kernel, X.org, or graphics drivers should be tested on particular
    hardware, and also run on a set of different platforms rather than
    just a single virtual testbeds.

    This field can specify a list of abstract class names such as
    "desktop" or "graphics-driver". Consumers of autopkgtest can then
    map these class names to particular machines/platforms/policies.
    Unknown class names should be ignored.

    This is purely an informational field for autopkgtest itself and
    will be ignored.

    Classes are separated by commas and/or whitespace.

Architecture: dpkg architecture field syntax
    When package tests are only supported on a limited set of
    architectures, or are known to not work on a particular (set of)
    architecture(s), this field can be used to define the supported
    architectures. The autopkgtest will be skipped when the
    architecture of the testbed doesn't match the content of this
    field. The format is the same as in (Build-)Depends, with the
    understanding that ``all`` is not allowed, and ``any`` means that
    the test will be run on every architecture, which is the default
    when not specifying this field at all.

Any unknown fields will cause the whole stanza to be skipped.

Defined restrictions
--------------------

allow-stderr
    Output to stderr is not considered a failure. This is useful for
    tests which write e. g. lots of logging to stderr.

breaks-testbed
    The test, when run, is liable to break the testbed system. This
    includes causing data loss, causing services that the machine is
    running to malfunction, or permanently disabling services; it does
    not include causing services on the machine to temporarily fail.

    When this restriction is present the test will usually be skipped
    unless the testbed's virtualisation arrangements are sufficiently
    powerful, or alternatively if the user explicitly requests.

build-needed
    The tests need to be run from a built source tree. The test runner
    will build the source tree (honouring the source package's build
    dependencies), before running the tests. However, the tests are
    *not* entitled to assume that the source package's build
    dependencies will be installed when the test is run.

    Please use this considerately, as for large builds it unnecessarily
    builds the entire project when you only need a tiny subset (like the
    tests/ subdirectory). It is often possible to run ``make -C tests``
    instead, or copy the test code to ``$AUTOPKGTEST_TMP`` and build it
    there with some custom commands. This cuts down the load on the
    Continuous Integration servers and also makes tests more robust as
    it prevents accidentally running them against the built source tree
    instead of the installed packages.

flaky
    The test is expected to fail intermittently, and is not suitable for
    gating continuous integration. This indicates a bug in either the
    package under test, a dependency or the test itself, but such bugs
    can be difficult to fix, and it is often difficult to know when the
    bug has been fixed without running the test for a while. If a
    ``flaky`` test succeeds, it will be treated like any other
    successful test, but if it fails it will be treated as though it
    had been skipped.

hint-testsuite-triggers
    This test exists purely as a hint to suggest when rerunning the
    tests is likely to be useful.  Specifically, it exists to
    influence the way dpkg-source generates the Testsuite-Triggers
    .dsc header from test metadata: the Depends for this test are
    to be added to Testsuite-Triggers.  (Just as they are for any other
    test.)

    The test with the hint-testsuite-triggers restriction should not
    actually be run.

    The packages listed as Depends for this test are usually indirect
    dependencies, updates to which are considered to pose a risk of
    regressions in other tests defined in this package.

    There is currently no way to specify this hint on a per-test
    basis; but in any case the debian.org machinery is not able to
    think about triggering individual tests.

isolation-container
    The test wants to start services or open network TCP ports. This
    commonly fails in a simple chroot/schroot, so tests need to be run
    in their own container (e. g. autopkgtest-virt-lxc) or their own
    machine/VM (e. g. autopkgtest-virt-qemu or autopkgtest-virt-null).
    When running the test in a virtualization server which does not
    provide this (like autopkgtest-schroot) it will be skipped.

    Tests may assume that this restriction implies that process 1 in the
    container's process namespace is a system service manager (init system)
    such as systemd or sysvinit + sysv-rc, and therefore system services
    are available via the ``service(8)``, ``invoke-rc.d(8)`` and
    ``update-rc.d(8))`` interfaces.

    Tests must not assume that a specific init system is in use: a
    dependency such as ``systemd-sysv`` or ``sysvinit-core`` does not work
    in practice, because switching the init system often cannot be done
    automatically. Tests that require a specific init system should use the
    ``skippable`` restriction, and skip the test if the required init system
    was not detected.

    Many implementations of the ``isolation-container`` restriction will
    also provide ``systemd-logind(8)`` or a compatible interface, but this
    is not guaranteed. Tests requiring a login session registered with
    logind should declare a dependency on ``default-logind | logind``
    or on a more specific implementation of ``logind``, and should use the
    ``skippable`` restriction to exit gracefully if its functionality is
    not available at runtime.

isolation-machine
    The test wants to interact with the kernel, reboot the machine, or
    other things which fail in a simple schroot and even a container.
    Those tests need to be run in their own machine/VM (e. g.
    autopkgtest-virt-qemu or autopkgtest-virt-null). When running the
    test in a virtualization server which does not provide this it will
    be skipped.

    This restriction also provides the same facilities as
    ``isolation-container``.

needs-internet
    The test needs unrestricted internet access, e.g. to download test data
    that's not shipped as a package, or to test a protocol implementation
    against a test server. Please also see the note about Network access later
    in this document.

needs-reboot
    The test wants to reboot the machine using
    ``/tmp/autopkgtest-reboot`` (see below).

needs-recommends (deprecated)
    Please use ``@recommends@`` in your test ``Depends:`` instead.

needs-root
    The test script must be run as root.

    While running tests with this restriction, some test runners will
    set the ``AUTOPKGTEST_NORMAL_USER`` environment variable to the name
    of an ordinary user account. If so, the test script may drop
    privileges from root to that user, for example via the ``runuser``
    command. Test scripts must not assume that this environment variable
    will always be set.

    For tests that declare both the ``needs-root`` and ``isolation-machine``
    restrictions, the test may assume that it has "global root" with full
    control over the kernel that is running the test, and not just root
    in a container (more formally, it has uid 0 and full capabilities in
    the initial user namespace as defined in ``user_namespaces(7)``).
    For example, it can expect that mounting block devices will succeed.

    For tests that declare the ``needs-root`` restriction but not the
    ``isolation-machine`` restriction, the test will be run as uid 0 in
    a user namespace with a reasonable range of system and user uids
    available, but will not necessarily have full control over the kernel,
    and in particular it is not guaranteed to have elevated capabilities
    in the initial user namespace as defined by ``user_namespaces(7)``.
    For example, it might be run in a namespace where uid 0 is mapped to
    an ordinary uid in the initial user namespace, or it might run in a
    Docker-style container where global uid 0 is used but its ability to
    carry out operations that affect the whole system is restricted by
    capabilities and system call filtering.  Tests requiring particular
    privileges should use the ``skippable`` restriction to check for
    required functionality at runtime.

needs-sudo
    The test script needs to be run as a non-root user who is a member of
    the ``sudo`` group, and has the ability to elevate privileges to root
    on-demand.

    This is useful for testing user components which should not normally
    be run as root, in test scenarios that require configuring a system
    service to support the test. For example, gvfs has a test-case which
    uses sudo for privileged configuration of a Samba server, so that
    the unprivileged gvfs service under test can communicate with that server.

    While running a test with this restriction, ``sudo(8)`` will be
    installed and configured to allow members of the ``sudo`` group to run
    any command without password authentication.

    Because the test user is a member of the ``sudo`` group, they will
    also gain the ability to take any other privileged actions that are
    controlled by membership in that group. In particular, several packages
    install ``polkit(8)`` policies allowing members of group ``sudo`` to
    take administrative actions with or without authentication.

    If the test requires access to additional privileged actions, it may
    use its access to ``sudo(8)`` to install additional configuration
    files, for example configuring ``polkit(8)`` or ``doas.conf(5)``
    to allow running ``pkexec(1)`` or ``doas(1)`` without authentication.

    Commands run via ``sudo(8)`` or another privilege-elevation tool could
    be run with either "global root" or root in a container, depending
    on the presence or absence of the ``isolation-machine`` restriction,
    in the same way described for ``needs-root``.

rw-build-tree
    The test(s) needs write access to the built source tree (so it may
    need to be copied first). Even with this restriction, the test is
    not allowed to make any change to the built source tree which (i)
    isn't cleaned up by debian/rules clean, (ii) affects the future
    results of any test, or (iii) affects binary packages produced by
    the build tree in the future.

skip-not-installable
    This restrictions may cause a test to miss a regression due to
    installability issues, so use with caution. If one only wants to
    skip certain architectures, use the ``Architecture`` field for
    that.

    This test might have test dependencies that can't be fulfilled in
    all suites or in derivatives. Therefore, when apt-get installs the
    test dependencies, it will fail. Don't treat this as a test
    failure, but instead treat it as if the test was skipped.

skippable
    The test might need to be skipped for reasons that cannot be
    described by an existing restriction such as isolation-machine or
    breaks-testbed, but must instead be detected at runtime. If the
    test exits with status 77 (a convention borrowed from Automake), it
    will be treated as though it had been skipped. If it exits with any
    other status, its success or failure will be derived from the exit
    status and stderr as usual. Test authors must be careful to ensure
    that ``skippable`` tests never exit with status 77 for reasons that
    should be treated as a failure.

superficial
    The test does not provide significant test coverage, so if it
    passes, that does not necessarily mean that the package under test
    is actually functional. If a ``superficial`` test fails, it will be
    treated like any other failing test, but if it succeeds, this is
    only a weak indication of success. Continuous integration systems
    should treat a package where all non-superficial tests are skipped as
    equivalent to a package where all tests are skipped.

    For example, a C library might have a superficial test that simply
    compiles, links and executes a "hello world" program against the
    library under test but does not attempt to make use of the library's
    functionality, while a Python or Perl library might have a
    superficial test that runs ``import foo`` or ``require Foo;`` but
    does not attempt to use the library beyond that.

Defined features
----------------

test-name
    Set an explicit test name for the log heading and the ``summary`` file
    for a ``Test-Command:`` inline test. When not given, these are
    enumerated like ``command1``. Syntax: test-name=my_test_name (no spaces
    allowed).


Source package header
---------------------

To allow test execution environments to discover packages which provide
tests, their source packages need to have a ``Testsuite:`` header
containing ``autopkgtest`` (or a value like ``autopkgtest-pkg-perl``,
see below).  Multiple values get comma separated, as usual in control
files.  This tag is added automatically by dpkg-source version 1.17.11
or later, so normally you don't need to worry about this field.

Automatic test control file for known package types
---------------------------------------------------

There are groups of similarly-structured packages for which the contents
of ``debian/tests/control`` would be mostly identical, such as Perl or
Ruby libraries. If ``debian/tests/control`` is absent, the ``autodep8``
tool can generate an automatic control file. If installed, ``autopkgtest``
will automatically use it; this can be disabled with the
``--no-auto-control`` option.

Those packages do not have to provide ``debian/tests/``, but they should
still include an appropriate source package header
(``Testsuite: autopkgtest-pkg-perl`` or similar) so that they can be
discovered in the archive.

Reboot during a test
--------------------

Some testbeds support rebooting; for those, the testbed will have a
``/tmp/autopkgtest-reboot`` command which tests can call to cause a
reboot.  **Do not** use ``reboot`` and similar commands directly without
at least checking for the presence of that script! They will cause
testbeds like ``null`` or ``schroot`` to reboot the entire host, and
even for ``lxc`` or ``qemu`` it will just cause the test to fail as there
is no state keeping to resume a test at the right position after reboot
without further preparation (see below).

The particular steps for a rebooting tests are:

- The test calls ``/tmp/autopkgtest-reboot my_mark`` with a "mark"
  identifier. ``autopkgtest-reboot`` will cause the test to terminate
  (with ``SIGKILL``).

- autopkgtest backs up the current state of the test source tree and
  any ``$AUTOPKGTEST_ARTIFACTS`` that were created so far, reboots the
  testbed, and restores the test source tree and artifacts.

- The test gets run again, this time with a new environment variable
  ``$AUTOPKGTEST_REBOOT_MARK`` containing the argument to
  ``autopkgtest-reboot``, e. g. ``my_mark``.

- The test needs to check ``$AUTOPKGTEST_REBOOT_MARK`` and jump to the
  appropriate point. A nonexisting variable means "start from the
  beginning".

This example test will reboot the testbed two times in between:

::

    #!/bin/sh -e
    case "$AUTOPKGTEST_REBOOT_MARK" in
      "") echo "test beginning"; /tmp/autopkgtest-reboot mark1 ;;
      mark1) echo "test in mark1"; /tmp/autopkgtest-reboot mark2 ;;
      mark2) echo "test in mark2" ;;
    esac
    echo "test end"

In some cases your test needs to do the reboot by itself, e. g. through
kexec, or a reboot command that is hardcoded in the piece of software
that you want to test. To support those, you need to call
``/tmp/autopkgtest-reboot-prepare my_mark`` at a point as close as
possible to the reboot instead; this will merely save the state but not
issue the actual reboot by itself. Note that all logs and artifacts from
the time between calling ``autopkgtest-reboot-prepare`` and rebooting
will be lost. Other than that, the usage is very similar to above.
Example:

::

    #!/bin/sh
    if [ "$AUTOPKGTEST_REBOOT_MARK" = phase1 ]; then
        echo "continuing test after reboot"
        ls -l /var/post-request-action
        echo "end of test"
    else
        echo "beginning test"
        /tmp/autopkgtest-reboot-prepare phase1
        touch /var/post-request-action
        reboot
    fi

Network access
--------------
autopkgtest needs access to the network at least for downloading test
dependencies and possibly dist-upgrading testbeds. In environments with
restricted internet access you need to set up an apt proxy and configure
the testbed to use it. (Note that the standard tools like
autopkgtest-build-lxc or mk-sbuild automatically use the apt proxy from
the host system.)

In general, tests should not access the internet themselves. If a test does use
the internet outside of the pre-configured apt domain, the test must be marked
with the needs-internet restriction. Using the internet usually makes tests
less reliable, so this should be kept to a minimum. But for many packages their
main purpose is to interact with remote web services and thus their testing
should actually cover those too, to ensure that the distribution package keeps
working with their corresponding web service.

Please note that for Debian, the ftp-master have ruled (in their
`REJECT-FAQ (Non-Main II) <https://ftp-master.debian.org/REJECT-FAQ.html>`_
that tests must not execute code they download. In particular, tests must not
use external repositories to depend on software (as opposed to data) that is
not in Debian. However, currently there is nothing preventing this.

Debian's production CI infrastructure allows unrestricted network access
on most workers. Tests with needs-internet can be skipped on some to avoid
flaky behavior. In Ubuntu's infrastructure access to sites other than
`*.ubuntu.com` and `*.launchpad.net` happens via a proxy (limited to DNS and
http/https).
