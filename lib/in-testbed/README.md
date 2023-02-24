Scripts in this directory are either copied into the testbed and run
from there, or run directly as shell commands. They should generally only
use commands that are Essential: yes or have had their prerequisites checked.

For scripts that are used with autopkgtest-virt-chroot, the ChrootRunner
in tests/autopkgtest needs to copy all executables needed by these
scripts into its chroot. Scripts that are only used by containers with an
init system do not need this.
