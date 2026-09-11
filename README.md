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
- `adb` on `PATH` (Android SDK [platform-tools](https://developer.android.com/tools/releases/platform-tools))
- `ffmpeg` on `PATH`
- `yt-dlp` on `PATH` only if you play URLs

ARM device agents (agent, proxy, preload) are **GitHub Release**
assets, not git files. `ghostdeck play` downloads them to
`~/.ghostdeck/bin/` when missing and checks hashes from the repo
manifest.

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
`~/.ghostdeck/bin`. Needs `clang`, `armv7-linux-gnueabihf-gcc`, and
`adb`/`ffmpeg` on PATH.

Put `adb` and `ffmpeg` on `PATH` before `play`. Example (Homebrew ffmpeg
plus Google platform-tools):

```bash
export PATH="$PATH:$HOME/Library/Android/sdk/platform-tools"
```

Simultaneous Studio keys need a **local** copy of official Studio with
hidshim (`~/Applications/Ulanzi Studio ADB.app`). ghostdeck never
uploads that app.

## Commands

| Command | Purpose |
| --- | --- |
| `ghostdeck detect` | Print serial, VID/PID, and USB mode (HID `2207:0019` or ADB `18d1:d002`). Fails if no deck is found. Serial is discovered at runtime; it is not baked into the source. |
| `ghostdeck play FILE\|URL` | ADB JPEG play. Optional IOHID attempt does not block play. |
| `ghostdeck studio` | Launch the **local** hidshim copy (`~/Applications/Ulanzi Studio ADB.app`). Official `/Applications/Ulanzi Studio.app` is not written. |
| `ghostdeck stop` | Stop playback, restore stock UI, delete `/tmp` agents. |
| `ghostdeck quit` | Tear down the IOHID keeper if any. |
| `ghostdeck status` | USB mode, shim copy, IOHID, playing. |

`ffmpeg` and `adb` missing: `play` fails. `yt-dlp` missing: URL sources
fail; local files still play.

Host state lives in `~/.ghostdeck/state.json`. Logs go to the terminal
only.

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
