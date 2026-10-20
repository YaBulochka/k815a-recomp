#!/bin/sh
# Install or remove the udev rule that lets the desktop user talk to the K815
# vendor HID interface (usage page 0xFF01) through /dev/hidrawN.
#
# This script needs root.  Run it yourself:
#
#   sudo sh tools/install_udev.sh            # install
#   sudo sh tools/install_udev.sh remove     # uninstall
#
# The rule itself ships in tools/udev/99-mkespn-k815.rules; read it for the PID
# candidates and the manual commands.
set -e

src="$(dirname "$0")/udev/99-mkespn-k815.rules"
dst="/etc/udev/rules.d/99-mkespn-k815.rules"

if [ "$(id -u)" -ne 0 ]; then
    echo "needs root: run 'sudo sh $0 $*'" >&2
    exit 1
fi

if [ "$1" = "remove" ]; then
    rm -f "$dst"
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=hidraw
    echo "removed $dst"
    exit 0
fi

if [ ! -f "$src" ]; then
    echo "missing $src" >&2
    exit 1
fi

install -m 0644 "$src" "$dst"
udevadm control --reload-rules
udevadm trigger --subsystem-match=hidraw
echo "installed $dst"
echo "replug the keyboard, then check:"
echo "  python3 tools/k815ctl.py list"
