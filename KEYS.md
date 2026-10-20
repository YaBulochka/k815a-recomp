# Reassigning keys and non-standard keys (F13–F24)

Verified on this machine: KDE Plasma 6.8.0, KWin 6.8.0, Wayland, xkbcommon 1.13.2.

## 1. What the keyboard can send at all

The keymap is 160 bytes, one USB HID keyboard usage code per key
(see `PROTOCOL.md`). A byte can hold any value `0x00..0xFF`, so in principle any
key with a usage code from keyboard page `0x07` can be assigned.

The function keys F13–F24 are ordinary keyboard-page usage codes:

| key | HID usage | key | HID usage |
|---------|-----------|---------|-----------|
| F13     | `0x68`    | F19     | `0x6E`    |
| F14     | `0x69`    | F20     | `0x6F`    |
| F15     | `0x6A`    | F21     | `0x70`    |
| F16     | `0x6B`    | F22     | `0x71`    |
| F17     | `0x6C`    | F23     | `0x72`    |
| F18     | `0x6D`    | F24     | `0x73`    |

On the Linux side the kernel (`hid-input.c`) turns these usage codes into the
evdev codes `KEY_F13..KEY_F24` = 183..194. So the keyboard can send them if the
firmware accepts an arbitrary usage code. Hardware verification comes when the
device is connected.

## 2. Can KDE bind F13–F24 as a hot key

Yes. Verified through the D-Bus service `org.kde.kglobalaccel`:

* `setShortcut` **accepts** F13 (`Qt::Key_F13 = 16777276`) and F24
  (`16777287`) and stores them (with flags = 4);
* a real F24 press generated through `uinput` (`KEY_F24 = 194`) triggers the
  `globalShortcutPressed` signal for the bound action.

So the chain "keyboard → kernel → KWin → KGlobalAccel" works for the F-keys.

## 3. Pitfall: the layout overrides F13–F24

The xkb rules for evdev **always** append `+inet(evdev)` to the layout:
`/usr/share/X11/xkb/rules/evdev` contains the line `* = +inet(evdev)`.
The file `symbols/inet` remaps the keys `<FK13>..<FK24>` to media keysyms, so by
default you get an XF86 key rather than F13.

The actual map on this machine (`xmodmap -pke`, evdev code = xkb code − 8):

| evdev | xkb keycode | default keysym | wanted keysym |
|-------|-------------|---------------------|---------------|
| 183   | 191         | `XF86Tools`         | F13           |
| 184   | 192         | `XF86Launch5`       | F14           |
| 185   | 193         | `XF86Launch6`       | F15           |
| 186   | 194         | `XF86Launch7`       | F16           |
| 187   | 195         | `XF86Launch8`       | F17           |
| 188   | 196         | `XF86Launch9`       | F18           |
| 189   | 197         | `F19`               | F19 (already fine)  |
| 190   | 198         | `XF86AudioMicMute`  | F20           |
| 191   | 199         | `XF86TouchpadToggle`| F21           |
| 192   | 200         | `XF86TouchpadOn`    | F22           |
| 193   | 201         | `F23` (+XF86Assistant) | F23 (already fine) |
| 194   | 202         | `F24` (+XF86TouchpadToggle) | F24 (already fine) |

Bottom line: out of the box only F19, F23 and F24 work. The rest need a layout
override, or you have to bind the shortcut to the XF86 keysym that actually
arrives.

## 4. Recipe: enable the real F13–F24

Verified by compiling with `xkbcli compile-keymap`: if you add your own symbols
**after** `inet(evdev)`, every `<FK13>..<FK24>` becomes F13..F24.

Ready-made files live in `tools/xkb/`; installation (no root):

```sh
# 1) symbols: a wrapper layout that remaps FK13..FK24
mkdir -p ~/.config/xkb/symbols
cp tools/xkb/symbols/k815-fkeys ~/.config/xkb/symbols/

# 2) rules: a copy of the system evdev plus our layer AFTER inet(evdev)
mkdir -p ~/.config/xkb/rules
cp /usr/share/X11/xkb/rules/*.lst /usr/share/X11/xkb/rules/evdev \
   ~/.config/xkb/rules/
#   in ~/.config/xkb/rules/evdev replace
#       *       =   +inet(evdev)
#   with  *       =   +inet(evdev)+k815-fkeys

# 3) apply: in KDE "System Settings → Input → Layouts" switch the
#    layout away and back, or log in again
```

Check without applying:

```sh
xkbcli compile-keymap --rules evdev --layout us --model pc86 \
    --include ~/.config/xkb --include-defaults \
  | grep -E 'key <FK1[3-9]>|key <FK2[0-4]>'
```

After that `xmodmap -pke | grep 191` should print `F13`, and KDE can bind
F13–F24 in the usual way.

## 5. Tool

`tools/k815ctl.py keymap --keymap <file>` accepts an 8×20 matrix
(`KBconfig.ini`, the form `col_row=value`, decimal, as in that file) or 160
bytes in hex/binary and writes it to the device. The values of F13–F24 are
`104..115` (`0x68..0x73`).

The matrix is the only source of the keymap when you write it through this
tool: whatever the vendor application stored for a key does not survive a full
upload. (Inside the vendor application the two sources merge — see
`PROTOCOL.md`, section 4.)
