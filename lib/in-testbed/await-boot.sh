#!/bin/sh
# Copyright © 2006-2018 Canonical Ltd.
# Copyright © 2020 Paul Gevers
# Copyright © 2020 Lars Kruse
# Copyright © 2022 Simon McVittie
# SPDX-License-Identifier: GPL-2.0-or-later

set -eu

if [ -d /run/systemd/system ]; then
    systemctl start network-online.target
else
    while pgrep -f '/etc/init[.]d/rc' > /dev/null; do
        sleep 1
    done
fi
