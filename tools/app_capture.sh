#!/bin/sh
# Run the real Windows app under Wine against the uhid emulator, press Apply,
# and capture the resulting upload session.
#
#   sh tools/app_capture.sh captures/NAME.jsonl [CONFIG_INI] [MACRO_DAT] [KB_INI] [CLICKS]
#
# CONFIG_INI / MACRO_DAT / KB_INI, if given, are copied over
# extracted/app/config/config.ini, ShortcutMacro.dat and KBconfig.ini before
# the app starts (so the app reads the modified configuration). All are
# restored afterwards.
#
# CLICKS is an optional list of clicks performed before Apply, separated by ";".
# Each click is X,Y and is relative to the main window.  Two prefixes change the
# target, which is needed for the key-assignment dialog: it is a separate
# top-level window that overlaps the main one.
#
#   X,Y    relative to the main window (as before)
#   +X,Y   click a key of the device picture and wait for the dialog to appear
#   @X,Y   relative to the key-assignment dialog (found by its size 605x686)
#   %X,Y   relative to the drop-down pop-up window named "MainMenuDlg"
#   !KEYSYM  send a key press to the dialog (for the "Keyboard keys" box)
#   #X,Y   absolute screen coordinates (for anything else)
#
# It lets an experiment reach a page or a drop-down entry that the app does not
# open by itself, for example:
#
#   "180,90;500,187;543,266"            # Light tab, mode list, 3rd item
#   "440,367;@61,261;@427,222;#958,476;@380,516"  # key 1: Media, item 2, OK
#
# Window-relative coordinates for the useful controls (tabs, effect list, Apply)
# are in RE-NOTES.md, section 5.  If K815_SHOT is set to a path, a PNG of the
# main window is taken after the extra clicks, which shows whether a click
# landed; the special value "root" captures the whole screen instead (needed
# when a drop-down pop-up is open, the main window is black then).
# If K815_KEEP is set to a directory, the configuration files are copied there
# right before they are restored, so that an experiment can diff what the
# application wrote.
#
# The app is killed with `wineserver -k`, the emulator with its pidfile, so no
# fragile process matching is involved.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
LOG="$1"
case "$LOG" in /*) ;; *) LOG="$ROOT/$LOG" ;; esac
CONFIG_INI="${2:-}"
case "$CONFIG_INI" in ""|/*) ;; *) CONFIG_INI="$ROOT/$CONFIG_INI" ;; esac
MACRO_DAT="${3:-}"
case "$MACRO_DAT" in ""|/*) ;; *) MACRO_DAT="$ROOT/$MACRO_DAT" ;; esac
KB_INI="${4:-}"
case "$KB_INI" in ""|/*) ;; *) KB_INI="$ROOT/$KB_INI" ;; esac
CLICKS="${5:-}"
SHOT="${K815_SHOT:-}"
case "$SHOT" in
    "")   ;;
    root) SHOT="$ROOT/root.png" ;;
    /*)   ;;
    *)    SHOT="$ROOT/$SHOT" ;;
esac
PREFIX=/tmp/opencode/wine-k815
APPDIR="$ROOT/extracted/app"
CFG="$APPDIR/config/config.ini"
DAT="$APPDIR/config/ShortcutMacro.dat"
KB="$APPDIR/config/KBconfig.ini"
PIDFILE=/tmp/opencode/k815emu.pid
export WINEPREFIX="$PREFIX" WINEDLLOVERRIDES="mscoree,mshtml="

stop_emulator() {
    if [ -f "$PIDFILE" ]; then
        pid=$(cat "$PIDFILE")
        kill "$pid" 2>/dev/null || true
        rm -f "$PIDFILE"
    fi
}

restore() {
    if [ -n "${K815_KEEP:-}" ]; then
        mkdir -p "$K815_KEEP"
        for f in "$CFG" "$DAT" "$KB"; do
            [ -f "$f" ] && cp "$f" "$K815_KEEP/"
        done
        echo "== kept modified config in $K815_KEEP"
    fi
    if [ -f "$CFG.k815bak" ]; then mv -f "$CFG.k815bak" "$CFG" 2>/dev/null || true; fi
    if [ -f "$DAT.k815bak" ]; then mv -f "$DAT.k815bak" "$DAT" 2>/dev/null || true; fi
    if [ -f "$KB.k815bak" ]; then mv -f "$KB.k815bak" "$KB" 2>/dev/null || true; fi
}

# Window id of the key-assignment dialog: the top-level window named like the
# main one, but 605x686.  The dialog needs a moment to appear after the click,
# so it is polled for up to about 10 s.
dialog_wid() {
    n=${1:-20}
    i=0
    while [ $i -lt "$n" ]; do
        # The click loop sets IFS=';', so the window list is indexed with sed
        # instead of being split by the shell.
        c=0
        while [ $c -lt 8 ]; do
            w=$(xdotool search --name "MKESPN" 2>/dev/null | sed -n "$((c + 1))p")
            [ -n "$w" ] || break
            WIDTH=""
            HEIGHT=""
            eval "$(xdotool getwindowgeometry --shell "$w" 2>/dev/null)" || true
            if [ -n "${K815_DEBUG:-}" ]; then echo "  dialog_wid: $w W=$WIDTH H=$HEIGHT" >&2; fi
            if [ "$WIDTH" = 605 ] && [ "$HEIGHT" = 686 ]; then
                echo "$w"
                return 0
            fi
            c=$((c + 1))
        done
        i=$((i + 1))
        sleep 0.5
    done
    return 1
}

echo "== stopping previous app/emulator"
wineserver -k 2>/dev/null || true
stop_emulator
sleep 1

trap restore EXIT INT TERM

if [ -n "$CONFIG_INI" ]; then
    if [ ! -f "$CONFIG_INI" ]; then
        echo "!! config not found: $CONFIG_INI" >&2
        exit 1
    fi
    cp "$CFG" "$CFG.k815bak"
    cp "$CONFIG_INI" "$CFG"
    echo "== using config: $CONFIG_INI"
fi

if [ -n "$MACRO_DAT" ]; then
    if [ ! -f "$MACRO_DAT" ]; then
        echo "!! macro dat not found: $MACRO_DAT" >&2
        exit 1
    fi
    cp "$DAT" "$DAT.k815bak"
    cp "$MACRO_DAT" "$DAT"
    echo "== using macros: $MACRO_DAT"
fi

if [ -n "$KB_INI" ]; then
    if [ ! -f "$KB_INI" ]; then
        echo "!! KBconfig not found: $KB_INI" >&2
        exit 1
    fi
    cp "$KB" "$KB.k815bak"
    cp "$KB_INI" "$KB"
    echo "== using KBconfig: $KB_INI"
fi

echo "== starting emulator -> $LOG"
setsid nohup python3 "$ROOT/tools/k815_emulator.py" --log "$LOG" \
    --pidfile "$PIDFILE" >/tmp/opencode/emu.out 2>&1 &
sleep 2

echo "== starting app"
cd "$APPDIR"
setsid nohup wine "Gaming Keyboard.exe" >/tmp/opencode/app.out 2>&1 &

echo "== waiting for window"
wid=""
i=0
while [ $i -lt 60 ]; do
    wid=$(xdotool search --name "MKESPN K815" 2>/dev/null | head -1 || true)
    [ -n "$wid" ] && break
    i=$((i + 1))
    sleep 1
done
if [ -z "$wid" ]; then
    echo "!! window not found; see /tmp/opencode/app.out" >&2
    exit 1
fi
echo "   window id $wid"
sleep "${K815_WAIT:-3}"

# Window id of a drop-down pop-up (the list that opens below a combobox).
# Polled briefly, the pop-up appears a moment after the arrow is clicked.
popup_wid() {
    i=0
    while [ $i -lt 10 ]; do
        w=$(xdotool search --name "MainMenuDlg" 2>/dev/null | head -1)
        [ -n "$w" ] && { echo "$w"; return 0; }
        i=$((i + 1))
        sleep 0.5
    done
    return 1
}

echo "== pressing Apply"
xdotool windowactivate "$wid" 2>/dev/null || true
if [ -n "$CLICKS" ]; then
    echo "== extra clicks: $CLICKS"
    old_ifs=$IFS
    IFS=';'
    for c in $CLICKS; do
        target=""
        case "$c" in
            @*) c=${c#@}; target="dlg" ;;
            +*) c=${c#+}; target="open" ;;
            %*) c=${c#%}; target="pop" ;;
            '!'*) c=${c#'!'}; target="key" ;;
            '#'*) c=${c#'#'}; target="abs" ;;
        esac
        cx=$(printf '%s' "$c" | cut -d, -f1)
        cy=$(printf '%s' "$c" | cut -d, -f2)
        case "$target" in
            key)
                dw=$(dialog_wid) || { echo "!! dialog not found for key $c" >&2; exit 1; }
                echo "   key $c (dialog $dw)"
                xdotool key --window "$dw" "$c"
                ;;
            open)
                # Click a key of the device picture until the assignment dialog
                # shows up (the app ignores the first clicks while it is busy).
                echo "   open key at $cx,$cy"
                ok=""
                j=0
                while [ "$j" -lt 6 ]; do
                    xdotool mousemove --window "$wid" "$cx" "$cy"
                    sleep 0.4
                    xdotool click 1
                    if dw=$(dialog_wid 2); then ok=1; break; fi
                    j=$((j + 1))
                done
                if [ -z "$ok" ]; then
                    echo "!! dialog did not open for $cx,$cy" >&2
                    xwininfo -root -tree 2>/dev/null | sed -n '1,15p' >&2
                    exit 1
                fi
                echo "   dialog $dw"
                ;;
            dlg)
                dw=$(dialog_wid) || {
                    echo "!! dialog not found for $c" >&2
                    xwininfo -root -tree 2>/dev/null | sed -n '1,15p' >&2
                    exit 1
                }
                echo "   click $cx,$cy (dialog $dw)"
                xdotool mousemove --window "$dw" "$cx" "$cy"
                sleep 0.3
                xdotool click 1
                ;;
            pop)
                pw=$(popup_wid) || true
                if [ -z "$pw" ]; then echo "!! popup not found for $c" >&2; exit 1; fi
                echo "   click $cx,$cy (popup $pw)"
                xdotool mousemove --window "$pw" "$cx" "$cy"
                sleep 0.3
                xdotool click 1
                ;;
            abs)
                echo "   click $cx,$cy (screen)"
                xdotool mousemove "$cx" "$cy"
                sleep 0.3
                xdotool click 1
                ;;
            *)
                echo "   click $cx,$cy"
                xdotool mousemove --window "$wid" "$cx" "$cy"
                sleep 0.3
                xdotool click 1
                ;;
        esac
        sleep 1
    done
    IFS=$old_ifs
fi
if [ -n "$SHOT" ]; then
    # K815_SHOT=root captures the whole screen, which is what you want when the
    # extra clicks opened the drop-down pop-up (the main window goes black while
    # the assignment dialog is up).
    if [ "$K815_SHOT" = root ]; then
        import -window root "$SHOT" 2>/dev/null || true
        echo "   screenshot: $SHOT (full screen)"
    else
        import -window "$wid" "$SHOT" 2>/dev/null || true
        echo "   screenshot: $SHOT"
    fi
fi
xdotool mousemove --window "$wid" 687 731 click 1
sleep 6

echo "== stopping"
wineserver -k 2>/dev/null || true
stop_emulator
sleep 1

n=$(grep -c '"kind": "SET"' "$LOG" 2>/dev/null || echo 0)
echo "== done: $n SET reports in $LOG"
