#!/usr/bin/env python3
"""End-to-end test: a real Chromium over WebHID -> /dev/hidraw -> the uhid emulator.

What is checked. The webhid/ page opens in a real Chromium (not headless, on an
Xvfb display), connects to our MKESPN K815 emulator over WebHID and repeats the
usual configuration scenario: key 3 is assigned F13 (0x68), the Alt+F4 switch is
turned off, encoders A and B are assigned Mute and AC Home. Then the emulator log
is compared byte for byte with what tools/k815_proto.py computes for the same
layout.

This exercises the whole chain: a click in the interface -> frame assembly in JS
-> Chromium WebHID -> the kernel -> uhid -> the emulator log. The frame count,
their order, and the fact that the report id (0x07) is not swallowed are checked
separately.

Chromium always shows the device chooser, so the test walks the dialog once
(a click through xdotool) and afterwards replaces only
navigator.hid.requestDevice with navigator.hid.getDevices(). The device objects
and the whole transport remain real.

Run:
    python3 tools/e2e_webhid.py

Options:
    --http-port 8794   port of the local http server
    --cdp-port  9350   Chromium debugging port
    --display   :99    Xvfb display
    --keep             do not shut the processes down after the run
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import websocket

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import k815_proto as P          # noqa: E402
import k815ctl                  # noqa: E402
import fixtures                 # noqa: E402

CAPTURE = fixtures.hardware_capture()
KBCONFIG = fixtures.kbconfig()
APP_CONFIG = fixtures.app_config()

KEYMAP_A_BASE, KEYMAP_B_BASE = 0x20, 0x49
# what we do in the interface: select key 3 and assign it F13
EDIT_SVG, EDIT_IDX, EDIT_CODE, EDIT_TEXT = "key-3", 24, 0x68, "0x68"
# switches: the e0 template byte is 0xC0 (BALTF4+KEYSMODE); clear BALTF4
EDIT_SW_BYTE = 0x80
# encoders: function number -> Consumer-page usage (see PROTOCOL.md)
EDIT_ENC = {"enc0": (12, 0x00E2), "enc1": (16, 0x0223)}
ENC_ADDR = {"enc0": 0x016E, "enc1": 0x017E}
ENC_SECOND_COPY = 6


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def port_busy(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.n = 0

    def call(self, method, params=None, timeout=30):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(method)

    def evaluate(self, expr, await_promise=False, timeout=30):
        res = self.call("Runtime.evaluate", {
            "expression": expr, "awaitPromise": await_promise,
            "returnByValue": True, "userGesture": True}, timeout=timeout)
        r = res.get("result", {})
        if res.get("exceptionDetails"):
            raise RuntimeError(f"JS: {res['exceptionDetails'].get('text')} {r.get('description','')}")
        return r.get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


class Proc:
    """A process with its own log, started through setsid."""

    def __init__(self, cmd, log, env=None):
        self.log = log
        fh = open(log, "wb")
        full = dict(os.environ)
        if env:
            full.update(env)
        self.p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, env=full, start_new_session=True)

    def kill(self):
        try:
            os.killpg(self.p.pid, signal.SIGTERM)
        except Exception:
            pass
        for _ in range(20):
            if self.p.poll() is not None:
                break
            time.sleep(0.1)
        else:
            try:
                os.killpg(self.p.pid, signal.SIGKILL)
            except Exception:
                pass


def xdotool(display, *args):
    subprocess.run(["xdotool", *args], env={"DISPLAY": display, "PATH": "/usr/bin:/bin"},
                   check=False, capture_output=True)


def screenshot(display, path):
    subprocess.run(["import", "-window", "root", path],
                   env={"DISPLAY": display, "PATH": "/usr/bin:/bin"}, check=False)


# --------------------------------------------------------------------------- #
# the reference in Python
# --------------------------------------------------------------------------- #
def expected_frames():
    km = bytearray(k815ctl.load_keymap(KBCONFIG))
    km[EDIT_IDX] = EDIT_CODE
    prog, _ = P.program_from_capture(CAPTURE)
    led = P.read_led(prog)                     # defaults, as in the UI
    prog.patch("A", KEYMAP_A_BASE, bytes(km))
    prog.patch_b(P.KEYMAP_BASE_B, bytes(km))
    P.apply_led(prog, **led)
    prog.patch_b(0x0001, bytes([EDIT_SW_BYTE]))
    for name, (num, usage) in EDIT_ENC.items():
        addr = prog.b_addr(ENC_ADDR[name])
        prog.patch("B", addr, bytes([usage & 0xFF, usage >> 8]))
        second = 0 if num == 0 else usage
        prog.patch("B", addr + ENC_SECOND_COPY, bytes([second & 0xFF, second >> 8]))
    return [f.hex() for f in P.emit(prog)]


def template_frames():
    """The unmodified template: needed to make sure the edit is visible."""
    prog, _ = P.program_from_capture(CAPTURE)
    return [f.hex() for f in P.emit(prog)]


# --------------------------------------------------------------------------- #
# the main scenario
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--http-port", type=int, default=8794)
    ap.add_argument("--cdp-port", type=int, default=9350)
    ap.add_argument("--display", default=":99")
    ap.add_argument("--profile", default="/tmp/opencode/chrome-x11")
    ap.add_argument("--emu-log", default=os.path.join(ROOT, "captures/e2e_webhid.jsonl"))
    ap.add_argument("--capture", default=fixtures.hardware_capture())
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    global CAPTURE
    CAPTURE = args.capture

    # A physical K815 and the uhid emulator share VID/PID 30fa:2350.  Chromium's
    # chooser cannot reliably select the emulator's 0xFF01 collection while the
    # real device is plugged in, so skip instead of writing to hardware.
    try:
        devs = k815ctl.enumerate_devices(0x30FA, 0x2350)
    except SystemExit:
        devs = []
    physical = [d for d in devs
                if "MKESPN" not in (d.get("product_string") or "")]
    if physical:
        names = ", ".join(sorted({d.get("product_string") or "?" for d in physical}))
        print(f"SKIP: physical K815 connected ({names}); "
              "unplug it to run the emulator e2e")
        return 0

    display = args.display
    dnum = display.lstrip(":")
    procs: list[Proc] = []
    cdp = None
    ok = False

    try:
        # the session template must match the sources
        subprocess.run([sys.executable, os.path.join(ROOT, "tools/build_webhid.py"),
                        "--capture", CAPTURE],
                       check=True, cwd=ROOT, stdout=subprocess.DEVNULL)

        # 1. display
        if not os.path.exists(f"/tmp/.X11-unix/X{dnum}"):
            procs.append(Proc(["Xvfb", display, "-screen", "0", "1440x900x24",
                               "-nolisten", "tcp"], "/tmp/opencode/e2e_xvfb.log"))
            time.sleep(2)
            print(f"Xvfb {display} started")

        # 2. http
        if port_busy(args.http_port):
            print(f"http server already listening on :{args.http_port}")
        else:
            procs.append(Proc([sys.executable, "-m", "http.server", str(args.http_port),
                               "--bind", "127.0.0.1", "--directory", ROOT],
                              "/tmp/opencode/e2e_http.log"))
            time.sleep(1)
            print(f"http server started on :{args.http_port}")

        # 3. emulator
        if os.path.exists(args.emu_log):
            os.remove(args.emu_log)
        emu_base = P.program_from_capture(CAPTURE)[0].base
        procs.append(Proc([sys.executable, os.path.join(ROOT, "tools/k815_emulator.py"),
                           "--log", args.emu_log, "--seed-ini", APP_CONFIG,
                           "--dev-value", hex(emu_base),
                           "--pidfile", "/tmp/opencode/e2e_emulator.pid"],
                          "/tmp/opencode/e2e_emulator.log"))
        time.sleep(3)
        if not os.path.exists(args.emu_log):
            raise RuntimeError("the emulator did not create its log, see /tmp/opencode/e2e_emulator.log")
        print(f"emulator started, log {os.path.relpath(args.emu_log, ROOT)}")

        # 4. browser
        url = f"http://127.0.0.1:{args.http_port}/webhid/"
        env = {"DISPLAY": display}
        os.environ.pop("WAYLAND_DISPLAY", None)
        env["WAYLAND_DISPLAY"] = ""
        procs.append(Proc([
            "chromium", "--no-sandbox", "--disable-gpu", "--ozone-platform=x11",
            "--no-first-run", "--no-default-browser-check",
            "--disable-features=Crashpad", "--disable-crash-reporter",
            f"--remote-debugging-port={args.cdp_port}", "--remote-allow-origins=*",
            f"--user-data-dir={args.profile}", "--window-size=1280,860",
            "--window-position=0,0", url], "/tmp/opencode/e2e_chromium.log", env=env))

        page = None
        for _ in range(60):
            time.sleep(0.5)
            try:
                for t in http_json(f"http://127.0.0.1:{args.cdp_port}/json/list"):
                    if t.get("type") == "page" and "webhid" in t.get("url", ""):
                        page = t
                        break
            except Exception:
                continue
            if page:
                break
        if not page:
            raise RuntimeError("the page did not open")
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.call("Runtime.enable")
        print("page opened")

        # 5. device permission
        devs = cdp.evaluate("navigator.hid.getDevices().then(d=>d.map(x=>x.productName))", True)
        if not devs:
            print("no permission yet, walking the chooser dialog through xdotool")
            if not onboard(cdp, display):
                screenshot(display, "/tmp/opencode/e2e_fail_onboard.png")
                raise RuntimeError("could not get permission for 0xFF01")
            devs = cdp.evaluate("navigator.hid.getDevices().then(d=>d.map(x=>x.productName))", True)
        print(f"permitted devices: {devs}")

        # the dialog is not needed any more: replace requestDevice only
        cdp.evaluate("""
          Object.defineProperty(navigator.hid, 'requestDevice', {
            configurable: true, writable: true,
            value: async () => await navigator.hid.getDevices(),
          });
          'patched'
        """)

        # compute the reference up front and make sure the test checks something at all
        want = expected_frames()
        base = template_frames()
        if want == base:
            raise RuntimeError("the edit changes no byte, the test checks nothing")
        print(f"reference: {len(want)} frames, "
              f"{sum(1 for a, b in zip(want, base) if a != b)} of them differ from the template")

        # 6. connect
        cdp.evaluate("document.getElementById('conn').click()")
        connected = False
        for _ in range(40):
            time.sleep(0.25)
            connected = not cdp.evaluate("document.getElementById('btnApply').disabled")
            if connected:
                break
        if not connected:
            screenshot(display, "/tmp/opencode/e2e_fail_connect.png")
            raise RuntimeError("the Apply button did not become active")
        print("connected:", cdp.evaluate("document.getElementById('connVid').textContent"))

        # 7. edit: key 3 -> F13
        # an SVG group has no click() method, so we send an event
        cdp.evaluate(f"""
          document.getElementById('{EDIT_SVG}')
            .dispatchEvent(new MouseEvent('click', {{bubbles: true}}))
        """)
        time.sleep(0.2)
        clicked = cdp.evaluate(f"""
          (() => {{
            const rows = [...document.querySelectorAll('#usageList .usage-row')];
            const row = rows.find(r => r.querySelector('.u-code').textContent === '{EDIT_TEXT}');
            if (!row) return 'no row {EDIT_TEXT} among ' + rows.length;
            row.click();
            return document.getElementById('codeInput').value;
          }})()
        """)
        if clicked != EDIT_TEXT:
            raise RuntimeError(f"could not assign {EDIT_TEXT}: {clicked}")
        print(f"key 3 assigned: {clicked}")

        # 7b. switches and encoder: exactly the same edits as in the reference
        state = cdp.evaluate("""
          (() => {
            const fire = (el) => el.dispatchEvent(new Event('change', {bubbles: true}));
            const sw = document.getElementById('swAltF4');
            sw.checked = false; fire(sw);
            const e0 = document.getElementById('encFn0');
            e0.value = '12'; fire(e0);
            const e1 = document.getElementById('encFn1');
            e1.value = '16'; fire(e1);
            return { sw: document.getElementById('swByte').textContent,
                     e0: e0.value, e1: e1.value };
          })()
        """)
        if state["sw"] != "0x80" or state["e0"] != "12" or state["e1"] != "16":
            raise RuntimeError(f"switches and encoder did not toggle: {state}")
        print(f"switch byte -> {state['sw']}, encoder A -> {state['e0']}, "
              f"B -> {state['e1']}")

        # 8. apply
        start = count_lines(args.emu_log)
        print("pressing Apply")
        # Apply asks for a confirmation until the first successful Dump in the
        # session; this test has no interactive dialog.
        cdp.evaluate("window.confirm = () => true; 'ok'")
        t0 = time.time()
        cdp.evaluate("document.getElementById('btnApply').click()")

        count = 0
        while time.time() - t0 < 60:
            time.sleep(0.5)
            count = len(read_sets(args.emu_log, start))
            if count >= 663:
                break
        time.sleep(0.5)
        got = read_sets(args.emu_log, start)

        # 9. compare. The write is followed by read-back verification, whose
        # 07 18 05 commands are SET reports too: compare only the first session.
        print(f"frames received: {len(got)}, expected: {len(want)}, in {time.time()-t0:.1f} s")
        ok = got[:len(want)] == want
        if ok:
            print("the frame stream matched byte for byte")
            # wait until the read-back verification has finished and the page is
            # idle again, otherwise Read is still blocked
            for _ in range(120):
                time.sleep(0.5)
                if not cdp.evaluate("document.getElementById('btnApply').disabled"):
                    break
            # the read path was broken by the same bug, so check it too
            cdp.evaluate("document.getElementById('btnRead').click()")
            time.sleep(2.5)
            text = cdp.evaluate("document.getElementById('log').textContent") or ""
            if "info #1:" in text and "read failed" not in text:
                print("reading device information: ok")
            else:
                ok = False
                print("reading device information failed:")
                print("\n".join(text.splitlines()[-4:]))
            if "verified" in text and "after write" in text:
                print("read-back verification: ok")
            else:
                ok = False
                print("read-back verification did not report success:")
                print("\n".join(text.splitlines()[-6:]))
        else:
            report_diff(want, got)
            screenshot(display, "/tmp/opencode/e2e_fail_frames.png")

    except Exception as e:
        print(f"ERROR: {e}")
    finally:
        if cdp:
            cdp.close()
        if args.keep:
            print("processes left running (--keep)")
        else:
            for p in reversed(procs):
                p.kill()

    print("E2E WEBHID " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def count_lines(path):
    try:
        with open(path) as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def read_sets(path, start_line, dev="0xFF01"):
    out = []
    try:
        with open(path) as f:
            for i, line in enumerate(f):
                if i < start_line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("kind") == "SET" and r.get("dev") == dev:
                    out.append((i, r["raw"]))
    except OSError:
        return []
    return [raw for _, raw in out]


def report_diff(want, got):
    n = min(len(want), len(got))
    for i in range(n):
        if want[i] != got[i]:
            print(f"first mismatch at frame {i}:")
            print(f"  expected {want[i]}")
            print(f"  got      {got[i]}")
            return
    if len(want) != len(got):
        print(f"length mismatch: expected {len(want)}, got {len(got)}")


def onboard(cdp, display):
    """Walk the chooser dialog once and grant both vendor interfaces.

    The chooser lists the K815 twice (0xFF01 and 0xFF00); the page prefers the
    0xFF01 write collection, so both rows are tried until it appears.
    """
    cdp.evaluate("navigator.hid.requestDevice({filters:[{vendorId:0x30FA}]}).catch(()=>{})")
    time.sleep(3)
    for attempt, row_y in enumerate((175, 213), 1):
        xdotool(display, "mousemove", "230", str(row_y), "click", "1")
        time.sleep(0.5)
        xdotool(display, "mousemove", "505", "510", "click", "1")
        time.sleep(1.5)
        names = cdp.evaluate("navigator.hid.getDevices().then(d=>d.map(x=>x.productName))", True) or []
        if any("0xFF01" in n for n in names):
            return True
        print(f"  attempt {attempt}: granted {names}, retrying")
        cdp.evaluate("navigator.hid.requestDevice({filters:[{vendorId:0x30FA}]}).catch(()=>{})")
        time.sleep(2.5)
    return False


if __name__ == "__main__":
    sys.exit(main())
