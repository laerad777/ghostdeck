# ghostdeck

Play JPEG video on a Ulanzi D200-class deck from macOS, and optionally
present a userspace virtual HID so official Ulanzi Studio can send keys
while the physical deck is in ADB.

Version **0.1.0**. License: MIT (see `LICENSE` and `NOTICE`).
Korean: [README.ko.md](README.ko.md).

ghostdeck does **not** redistribute Studio.app, vendor firmware, or
kernel modules. It never sets gadget `functions=hid,adb`.

## Requirements

- macOS (0.1.0 target; Linux is documentation only)
- Python 3.11+
- git

`ghostdeck play`, `stop`, `detect`, and `status` run these from `PATH`:

- `adb` (Android SDK [platform-tools](https://developer.android.com/tools/releases/platform-tools))
- `ffmpeg`
- `ffprobe` (ships with ffmpeg; `play` probes the source with it first)
- `yt-dlp` only if you play URLs

`ghostdeck build` compiles the device helpers and the hidshim Studio copy, so it needs:

- `armv7-linux-gnueabihf-gcc`, an ARM Linux cross toolchain (also needed the first time `play` builds the device binaries)
- Xcode command line tools: `clang`, `xcrun`, `install_name_tool`, `codesign`, `ditto`
- the official `/Applications/Ulanzi Studio.app` installed

ARM device binaries (`d200-zkgui-proxy`, `libd200-zkgui-preload.so`,
`d200-color-agent`) are not git files; there is no download step.
`ghostdeck build` compiles the proxy and preload from `device/*.c` with
`armv7-linux-gnueabihf-gcc` into `~/.ghostdeck/bin/`. `d200-color-agent`
is not compiled for you.

To build it, run `device/build-color-agent.sh`. That script enforces two
prerequisites, and a stock macOS machine does not meet the second:

1. an ARMv7 Linux cross toolchain providing `armv7-linux-gnueabihf-gcc`
   (Homebrew: `brew install armv7-unknown-linux-gnueabihf` — the formula
   name differs from the binary name);
2. an **ARM Linux** static `libturbojpeg.a` and its `turbojpeg.h`.

Homebrew's `jpeg-turbo` does **not** satisfy the second one: it installs a
Mach-O arm64 archive, which cannot be linked into an ARM Linux binary. The
script detects this and exits 1, printing:

> build-color-agent.sh: .../libturbojpeg.a contains no ELF object members
> (first inspected member was 'Mach-O 64-bit object arm64'). A macOS/Homebrew
> or Windows libturbojpeg cannot be linked into an ARM Linux binary. ...

The only path 0.1.0 offers is a real ARM Linux libturbojpeg, such as
Debian/Ubuntu's `libturbojpeg0-dev`, pointed at explicitly:

```bash
TURBOJPEG_INC=/usr/arm-linux-gnueabihf/include \
TURBOJPEG_LIB=/usr/arm-linux-gnueabihf/lib \
  device/build-color-agent.sh
```

0.1.0 has **no turnkey way to produce the agent on macOS**, and no download:
provision a prebuilt `d200-color-agent` in `~/.ghostdeck/bin/` out of band.
Until that file exists, `ghostdeck build` fails at the agent step.

## Install

This directory is the public repository root. Do not publish the parent lab tree.

```bash
git init
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
ghostdeck build
```

`ghostdeck build` compiles hidshim into a local Studio copy (from
`/Applications/Ulanzi Studio.app`) and ARM device helpers into
`~/.ghostdeck/bin`. It needs the ARM cross toolchain and the Xcode
command line tools, not `adb` or `ffmpeg`. It also needs
`~/.ghostdeck/bin/d200-color-agent` to exist already; see above.

Put `adb` and `ffmpeg` on `PATH` before `play`. Example (Homebrew ffmpeg
plus Google platform-tools):

```bash
export PATH="$PATH:$HOME/Library/Android/sdk/platform-tools"
```

Simultaneous Studio keys need a **local** copy of official Studio with
hidshim (`~/Applications/Ulanzi Studio ADB.app`); that copy is built
locally. The official `/Applications/Ulanzi Studio.app` is never
written or shipped.

## Commands

| Command | Purpose |
| --- | --- |
| `ghostdeck detect` | Print serial, VID/PID, and USB mode (HID `2207:0019` or ADB `18d1:d002`). Fails if no deck is found. Serial is discovered at runtime; it is not baked into the source. || `ghostdeck play FILE\|URL` | ADB JPEG play. Optional IOHID attempt does not block play. |
| `ghostdeck studio` | Launch the **local** hidshim copy (`~/Applications/Ulanzi Studio ADB.app`). Official `/Applications/Ulanzi Studio.app` is not written. |
| `ghostdeck stop` | Stop playback, restore stock UI, clear `/tmp/ghostdeck-*` on the deck. |
| `ghostdeck quit` | Tear down the IOHID keeper if any. |
| `ghostdeck status` | USB mode, shim copy, IOHID, playing. |

`ffmpeg`, `ffprobe`, and `adb` missing: `play` fails. `yt-dlp` missing:
URL sources fail; local files still play.

Host state lives in `~/.ghostdeck/state.json`. `ghostdeck studio` appends
the hidshim bridge's stdout and stderr to `/tmp/d200-local-bridge.log`;
other logs go to the terminal.

## If the deck is attached but stops answering

A deck can end up enumerated on USB as ADB while its `adbd` does not
answer. `adb devices -l` then lists it in a state that is not `device`,
usually `offline`:

```
<serial>      offline usb:18092032X transport_id:1
```

Nothing on the host recovers this. On the deck this project was developed
against, a 160-second wait, three `adb kill-server`/`start-server` cycles,
`adb reconnect`, and a USB reset were all tried, and none of them worked.
**Power-cycle the deck, or replug its USB cable, and then re-run the
command.** An attached deck that is merely `offline` is not the same thing
as a deck that is absent, and it is not a cabling problem.

`ghostdeck` distinguishes the three states instead of reporting `no device`:

| State | `detect` | `status` | `stop` |
| --- | --- | --- | --- |
| usable | exit `0`, `mode=adb` | exit `0`, `usb=adb` | restores the deck |
| attached, adb transport not answering | exit `3`, `mode=adb (offline)` | exit `3`, `usb=adb (offline)` | refuses, names the serial and the state, sends nothing to the deck |
| absent | exit `1`, `no device` | exit `0`, `usb=none` | exit `1`, names the deck as absent |

Exit codes: `0` success, `1` no deck or another failure, `2` the Python
environment is unusable (a missing `hidapi` or `pyusb`), `3` the deck is
attached but its adb transport cannot run a command.

`detect` and `status` are reporting commands and only read `adb devices`:
they never restart the adb server, and no command is ever sent to a device
that could not be identified as the deck.

## Plugins

Drop scripts in `~/.ghostdeck/plugins`. The 0.1.0 folder is documented
and created as needed; the call protocol is **not** frozen. These are
not Ulanzi Studio store plugins.

## Simultaneous (hidshim)

Physical USB cannot enumerate HID and ADB at once. `play` puts the real
deck on ADB. Official Studio does not see a fake USB device.

Simultaneous keys use **hidshim** in a **local copy**:
`~/Applications/Ulanzi Studio ADB.app`. That copy's `libhidapi.0.dylib`
is our shim (`2207:0019` / `ulanzi` inside the process, unix socket
`/tmp/d200-adb-bridge.sock`). `ghostdeck studio` opens that copy.

Official `/Applications/Ulanzi Studio.app` is never written or shipped.
IOHIDUserDevice is a failed Apple-entitlement spike, not the product path.
Hardware deck buttons during ADB play are best-effort.

## What this project will not do

- Set `functions=hid,adb` or flash firmware
- Redistribute Studio.app
- Commit ARM binaries to git
- Hard-code a device serial
