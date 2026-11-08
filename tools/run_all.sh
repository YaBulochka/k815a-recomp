#!/bin/sh
# One entry point for the offline test suite of the K815 tools.
#
#   sh tools/run_all.sh          syntax checks + the two offline selftests
#   sh tools/run_all.sh --full   the above plus the Chromium WebHID end-to-end
#                                test (needs Xvfb :99, chromium, xdotool)
#
# Nothing here talks to a real keyboard: the selftests replay captured
# sessions, and the end-to-end test drives the uhid emulator.  A stage that is
# missing its tools is skipped, not failed.  The exit status is non-zero when
# any stage fails.
set -u

HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$HERE" || exit 1

FULL=0
case "${1:-}" in
    --full)       FULL=1 ;;
    ""|--quick)   FULL=0 ;;
    -h|--help)    sed -n '2,10p' "$0"; exit 0 ;;
    *)            echo "usage: sh tools/run_all.sh [--full]" >&2; exit 2 ;;
esac

pass=0
fail=0

run() {
    label=$1
    shift
    printf '\n== %s ==\n' "$label"
    if "$@"; then
        pass=$((pass + 1))
    else
        printf '!! %s: FAILED\n' "$label" >&2
        fail=$((fail + 1))
    fi
}

skip() {
    printf '\n== %s ==\n' "$1"
    printf -- '-- skipped: %s\n' "$2"
}

have() { command -v "$1" >/dev/null 2>&1; }

check_shell() {
    for f in tools/*.sh; do sh -n "$f" || return 1; done
}

check_js() {
    for f in webhid/*.js; do node --check "$f" || return 1; done
}

# --- static checks ---------------------------------------------------------- #
run "python syntax (tools/*.py)" python3 -m py_compile tools/*.py tests/*.py
run "shell syntax (tools/*.sh)"  check_shell
if have node; then
    run "js syntax (webhid/*.js)" check_js
else
    skip "js syntax (webhid/*.js)" "node not found"
fi

# --- offline selftests ------------------------------------------------------ #
run "model and CLI (tools/selftest_cli.py)" python3 tools/selftest_cli.py
run "macro protocol (tools/selftest_macro.py)" python3 tools/selftest_macro.py
if have node; then
    run "WebHID driver, JS vs Python (tools/selftest_webhid.py)" \
        python3 tools/selftest_webhid.py
else
    skip "WebHID driver, JS vs Python" "node not found"
fi

# --- optional end-to-end run ------------------------------------------------ #
if [ "$FULL" = 1 ]; then
    missing=""
    have chromium || have chromium-browser || missing="chromium"
    have xdotool || missing="${missing:+$missing, }xdotool"
    have Xvfb || missing="${missing:+$missing, }Xvfb"
    if [ -z "$missing" ]; then
        run "end-to-end WebHID (Chromium -> hidraw -> uhid)" \
            python3 tools/e2e_webhid.py
    else
        skip "end-to-end WebHID" "missing $missing"
    fi
fi

printf '\n%d stage(s) passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
