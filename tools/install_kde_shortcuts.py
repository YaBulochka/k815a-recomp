#!/usr/bin/env python3
"""Register the KDE global shortcuts used with the K815.

No root: this talks to the user's org.kde.kglobalaccel service over D-Bus.

    python3 tools/install_kde_shortcuts.py
    python3 tools/install_kde_shortcuts.py --systemsettings F13 --mic F20

The actions must already exist in KGlobalAccel.  `systemsettings.desktop` is
shipped by Plasma; the microphone action is the user's command-shortcut
desktop file (`X-KDE-GlobalAccel-CommandShortcut=true`), for example
`~/.local/share/applications/net.local.global-mute.sh.desktop`.
"""
from __future__ import annotations

import argparse
import sys

try:
    import dbus
except ImportError:
    sys.exit("python-dbus is required (package python-dbus)")

QT_FKEY = {f"F{i}": 16777264 + (i - 1) for i in range(1, 25)}


def action_id(component, action, name):
    # KGlobalAccel expects [component, action, component label, action label].
    return [component, action, name, name]


def set_shortcut(kg, action, key_name):
    if key_name not in QT_FKEY:
        raise SystemExit(f"unsupported key {key_name!r}; use F1..F24")
    aid = dbus.Array(action_id(*action), signature="s")
    keys = dbus.Array([QT_FKEY[key_name]], signature="i")
    got = kg.setShortcut(aid, keys, dbus.UInt32(4))
    if not got:
        raise SystemExit(f"KGlobalAccel rejected {action[0]} {action[1]}")
    print(f"{action[0]} {action[1]} -> {key_name} "
          f"(Qt {QT_FKEY[key_name]})")
    print(f"  read back: {list(kg.shortcut(aid))}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--systemsettings", default="F13", metavar="FKEY",
                    help="key for the System Settings launch (default F13)")
    ap.add_argument("--mic", default="F20", metavar="FKEY",
                    help="key for the microphone mute command (default F20)")
    ap.add_argument("--no-systemsettings", action="store_true")
    ap.add_argument("--no-mic", action="store_true")
    args = ap.parse_args()

    bus = dbus.SessionBus()
    kg = dbus.Interface(bus.get_object("org.kde.kglobalaccel", "/kglobalaccel"),
                        "org.kde.KGlobalAccel")

    if not args.no_systemsettings:
        set_shortcut(kg, ("systemsettings.desktop", "_launch",
                          "System Settings"),
                     args.systemsettings)
    if not args.no_mic:
        set_shortcut(kg, ("net.local.global-mute.sh.desktop", "_launch",
                          "Microphone Mute"),
                     args.mic)
    print("done; the shortcuts are stored in ~/.config/kglobalshortcutsrc")
    return 0


if __name__ == "__main__":
    sys.exit(main())
