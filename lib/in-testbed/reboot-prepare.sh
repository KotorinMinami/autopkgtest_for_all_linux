#!/bin/sh
# Copyright © 2014-2016 Canonical Ltd.
# Copyright © 2022 Simon McVittie
# SPDX-License-Identifier: GPL-2.0-or-later

# Usage: /tmp/autopkgtest-reboot-prepare MARK
#
# Record MARK as the marker to be passed to test scripts after rebooting,
# forcibly terminate the wrapper script that is running the actual test
# (leaving the actual test running!), and wait for the virtualization
# backend to signal that it has done everything necessary to prepare for
# the reboot.

set -e
[ -n "$1" ] || { echo "Usage: $0 <mark>" >&2; exit 1; }
echo "$1" > /run/autopkgtest-reboot-prepare-mark

# Unlike /tmp/autopkgtest-reboot, in this one we kill the wrapper script
# around the actual test, leaving the actual test running.
test_script_pid=$(cat /tmp/autopkgtest_script_pid)
kill -KILL "$test_script_pid"

# The virtualization backend removes the flag file when it has saved state
# and is ready for us to return control to the test script, which will
# trigger the actual reboot
while [ -e /run/autopkgtest-reboot-prepare-mark ]; do
    sleep 0.5
done
