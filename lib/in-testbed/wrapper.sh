#!/bin/sh
# Copyright 2006-2016 Canonical Ltd.
# Copyright 2022 Simon McVittie

# wrapper.sh [OPTIONS] -- COMMAND [ARG...]
#
# Options:
# --stdout=OUTPATH              Copy stdout to OUTPATH
# --stderr=ERRPATH              Copy stderr to ERRPATH
# --script-pid-file=PATH        Write this script's pid to PATH
#
# Run COMMAND [ARG...], sending its stdout through a pipe to this script's
# stdout and OUTPATH, and its stderr through a pipe to this script's stderr
# and ERRPATH. If COMMAND leaks background processes that hold the pipes
# open, terminate those processes. Exit with the exit status of COMMAND.

set -eu

debug () {
    :
}

log () {
    echo "$0: $*" >&2
}

errpath=
outpath=
script_pid_file=

cleanup () {
    if [ -n "$script_pid_file" ]; then
        rm -f "$script_pid_file"
    fi
}

trap cleanup EXIT INT QUIT PIPE

getopt_temp="debug"
getopt_temp="$getopt_temp,script-pid-file:"
getopt_temp="$getopt_temp,stderr:"
getopt_temp="$getopt_temp,stdout:"
getopt_temp="$(getopt -o '' --long "$getopt_temp" -n "$0" -- "$@")"
eval "set -- $getopt_temp"
unset getopt_temp

while [ "$#" -gt 0 ]; do
    case "$1" in
        (--debug)
            debug () {
                log "$*"
            }
            shift
            ;;

        (--script-pid-file)
            script_pid_file="$2"
            shift 2
            ;;

        (--stderr)
            errpath="$2"
            shift 2
            ;;

        (--stdout)
            outpath="$2"
            shift 2
            ;;

        (--)
            shift
            break
            ;;

        (-*)
            log "Unknown option: $1"
            exit 255
            ;;

        (*)
            break
            ;;
    esac
done

if [ "$#" -lt 1 ]; then
    log "A command is required" >&2
    exit 255
fi

tmp="$(mktemp -d)"
mkfifo "$tmp/err"
mkfifo "$tmp/out"

if [ -n "$outpath" ]; then
    touch "$outpath"
    tee -a -- "$outpath" < "$tmp/out" &
else
    cat < "$tmp/out" &
fi
out_pid="$!"

if [ -n "$errpath" ]; then
    touch "$errpath"
    tee -a -- "$errpath" < "$tmp/err" >&2 &
else
    cat < "$tmp/err" >&2 &
fi
err_pid="$!"

if [ -n "$script_pid_file" ]; then
    rm -f "$script_pid_file"
    set -C
    echo "$$" > "$script_pid_file"
    set +C
fi

exit_status=0
# We have to use exec in a subshell instead of running the test in the
# obvious way, to avoid this shell printing a message like "Terminated"
# or "Killed" to $tmp/err if it gets killed by a signal, which autopkgtest
# would interpret as failure when not using allow-stderr.
( exec >> "$tmp/out" 2>> "$tmp/err"; exec "$@" ) || exit_status="$?"

# The naive implementation here would be to iterate through /proc/[0-9]*/fd/*
# calling readlink on each one. However, that starts a readlink executable
# per fd (hooray shell scripting), which in practice is surprisingly slow,
# particularly on weak hardware (see 4115f7f5 "adt-virt-ssh: Speed up
# adt-ssh-wrapper"). So instead we use find(1) to amortize the process
# startup cost.

# If our temp directory contains \[]*? (unlikely), escape them
tmp_escaped="$(
    echo "$tmp" | sed -E -e 's/\\/\\\\/g' -e 's/\[/\\[/g' -e 's/]/\\]/g' -e 's/([*?])/\\\1/g'
)"

kill="$(
    cd /proc
    find [0-9]*/fd \
        -lname "$tmp_escaped"/out -o -lname "$tmp_escaped"/err \
        2>/dev/null |
    sed -e 's#/fd/.*##' |
    sort -u |
    grep -v -F -e "$out_pid" -e "$err_pid" |
    tr '\n' ' '
)"

if [ -n "$kill" ]; then
    log "Killing leaked background processes: $kill"
    # Intentionally word-splitting
    # shellcheck disable=SC2086
    ps $kill >&2 || :
    # shellcheck disable=SC2086
    kill -CONT $kill >&2 || :
    # shellcheck disable=SC2086
    kill -9 $kill >&2 || :
fi

wait "$out_pid" "$err_pid" || :

rm -fr "$tmp"
debug "Exit status: $exit_status"
exit "$exit_status"
