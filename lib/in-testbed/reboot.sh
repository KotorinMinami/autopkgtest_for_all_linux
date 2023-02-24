#!/bin/sh
# Copyright © 2014-2016 Canonical Ltd.
# Copyright © 2022 Simon McVittie
# SPDX-License-Identifier: GPL-2.0-or-later

# Usage: /tmp/autopkgtest-reboot MARK
#
# Record MARK as the marker to be passed to test scripts after rebooting,
# then forcibly terminate the actual test (but not the wrapper script
# around it, which will continue to report back status and logs).

set -e
[ -n "$1" ] || { echo "Usage: $0 <mark>" >&2; exit 1; }
echo "$1" > /run/autopkgtest-reboot-mark

test_script_pid=$(cat /tmp/autopkgtest_script_pid)

# The actual test is a direct child of $test_script_pid. We find it by
# walking up through the ancestors of this process until we find one
# whose parent pid is $test_script_pid.

p="$PPID"
while true; do
    read -r _ _ _ pp _ < "/proc/$p/stat"
    [ "$pp" -ne "$test_script_pid" ] || break
    p="$pp"
done
kill -KILL "$p"
