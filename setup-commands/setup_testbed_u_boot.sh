#!/bin/sh

if [ -e /dev/ttyS1 ] || [ -e /dev/hvc1 ]; then
    mkdir -p "/etc/init.d"
    cat <<EOF > "/etc/init.d/autopkgtest"
#!/bin/sh
### BEGIN INIT INFO
# Provides:          autopkgtest
# Required-Start:    \$all
# Required-Stop:
# Default-Start:     2 3 4 5
# Default-Stop:
### END INIT INFO

if [ "\$1" = start ]; then
    for device in ttyS1 hvc1; do
        if [ -e "/dev/\$device" ]; then
            echo "Starting root shell on \$device for autopkgtest"
            (setsid sh <"/dev/\$device" >"/dev/\$device" 2>&1) &
        fi
    done
fi
EOF

    chmod 755 "/etc/init.d/autopkgtest"
    update-rc.d autopkgtest defaults
    cat <<EOF > "/etc/systemd/system/autopkgtest@.service"
[Unit]
Description=autopkgtest root shell on %I
ConditionPathExists=/dev/%I

[Service]
ExecStart=/bin/sh
StandardInput=tty-fail
StandardOutput=tty
StandardError=tty
TTYPath=/dev/%I
SendSIGHUP=yes
# ignore I/O errors on unusable tty
SuccessExitStatus=0 208 SIGHUP SIGINT SIGTERM SIGPIPE

[Install]
WantedBy=multi-user.target
EOF
    # Mask the unit generated for /etc/init.d/autopkgtest
    ln -sf /dev/null "/etc/systemd/system/autopkgtest.service"

    mkdir -p "/etc/systemd/system/multi-user.target.wants"
    for device in ttyS1 hvc1; do
        ln -sf ../autopkgtest@.service "/etc/systemd/system/multi-user.target.wants/autopkgtest@${device}.service"
    done
fi

if [ -e "/etc/init/tty2.conf" ] && ! [ -e "/etc/init/ttyS0.conf" ]; then
    sed 's/tty2/ttyS0/g; s! *exec.*$!exec /sbin/getty -L ttyS0 115200 vt102!' \
        "/etc/init/tty2.conf" > "/etc/init/ttyS0.conf"
fi

grep -v '^\s*$' /etc/apt/sources.list > temp
while read line
do 
    if [ "${line:0:1}" != "#" ] && [ "${line:0:7}" != "deb-src" ]; then
        echo "deb-src${line:3}" >> /etc/apt/sources.list
    fi
done < temp
rm -rf temp

echo "Acquire::Languages \"none\";" > "$root"/etc/apt/apt.conf.d/90nolanguages
echo 'force-unsafe-io' > "$root"/etc/dpkg/dpkg.cfg.d/autopkgtest

echo 'Acquire::Retries "10";' > "$root"/etc/apt/apt.conf.d/90retry

apt-get update

if [ ! -e "/usr/bin/gpg" ]; then
    # first try gpg (newer package for just /usr/bin/gpg)
    if ! apt-cache show gpg >/dev/null 2>&1; then
        apt-get install -y gpg </dev/null
    else
        # but if that isn't there then try the older gnupg2 package
        apt-get install -y gnupg2 </dev/null
    fi
fi

if ! systemd-detect-virt --quiet --container; then
    apt-get install -y rng-tools </dev/null
fi

if [ "${UPGRADE:-true}" != "false" ]; then
    apt-get update && apt-get upgrade -y
    apt-get -o Dpkg::Options::="--force-confold" -y dist-upgrade </dev/null
    apt-get -o Dpkg::Options::="--force-confold" -y --purge autoremove </dev/null
fi

if ! sh -c 'type python3 >/dev/null 2>&1 || type python >/dev/null 2>&1'; then
    apt-get install -y --no-install-recommends python3-minimal < /dev/null
fi

apt-get clean

# avoid cron interference with apt-get update
echo 'APT::Periodic::Enable "0";' > "$root/etc/apt/apt.conf.d/02periodic"

# always include phased updates, so that the output is what we expect.
echo 'APT::Get::Always-Include-Phased-Updates "true";' > "$root/etc/apt/apt.conf.d/90always-include-phased-updates"
