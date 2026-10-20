#!/bin/sh
# Install the user-level xkb override that restores the real F13..F24.
# See KEYS.md. Nothing here needs root: everything goes into ~/.config/xkb.
#
#   sh tools/install_fkeys_xkb.sh          # install
#   sh tools/install_fkeys_xkb.sh --check  # only verify the result
#   sh tools/install_fkeys_xkb.sh --remove # remove the override
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
XKB="${XDG_CONFIG_HOME:-$HOME/.config}/xkb"
SYSDIR=/usr/share/X11/xkb

remove() {
    rm -f "$XKB/symbols/k815-fkeys" "$XKB/rules/evdev"
    echo "override removed from $XKB"
}

check() {
    if command -v xkbcli >/dev/null 2>&1; then
        xkbcli compile-keymap --rules evdev --layout us --model pc86 \
            --include "$XKB" --include-defaults 2>/dev/null \
          | grep -E 'key <FK1[3-9]>|key <FK2[0-4]>' || true
    else
        echo "xkbcli not found, skipping the check"
    fi
}

case "${1:-}" in
    --remove) remove; exit 0 ;;
    --check)  check;  exit 0 ;;
esac

mkdir -p "$XKB/symbols" "$XKB/rules"
cp "$HERE/xkb/symbols/k815-fkeys" "$XKB/symbols/k815-fkeys"

# copy the system rules and append our layer AFTER inet(evdev)
cp "$SYSDIR/rules/evdev" "$XKB/rules/evdev"
for f in "$SYSDIR"/rules/*.lst; do cp "$f" "$XKB/rules/"; done

python3 - "$XKB/rules/evdev" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p).read()
new, n = re.subn(r"(?m)^(\s*\*\s*=\s*)\+inet\(evdev\)\s*$",
                 r"\1+inet(evdev)+k815-fkeys", s)
if n == 0:
    sys.exit("could not find the line '* = +inet(evdev)' in rules/evdev")
open(p, "w").write(new)
print("rules/evdev: added the +k815-fkeys layer")
PY

echo "installed into $XKB"
echo "re-apply the layout (KDE: switch the layout away and back) or log in again"
echo "check:"
check
