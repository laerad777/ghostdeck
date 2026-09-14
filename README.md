# ghostdeck

[![ci](https://github.com/laerad777/ghostdeck/actions/workflows/ci.yml/badge.svg)](https://github.com/laerad777/ghostdeck/actions/workflows/ci.yml)

Play JPEG video on a Ulanzi D200 from macOS. While the deck is in ADB, Studio
keys go through a **local hidshim copy** started by `ghostdeck studio`.

Version **0.1.0**. MIT (`LICENSE`, `NOTICE`). Korean: [README.ko.md](README.ko.md).

This is a **checkout**, not a PyPI package. A wheel of `ghostdeck` cannot see
`vendor/`, `device/`, or `reference/`. Clone the repo and install editable.

Official Studio.app, vendor firmware, and kernel modules are not shipped.
The gadget is never set to `functions=hid,adb`. Device serials are discovered
at runtime, never committed.

## Requirements

- macOS (0.1.0)
- Python 3.11+
- `adb`, `ffmpeg`, `ffprobe` on `PATH` (`yt-dlp` only for URLs)
- `hidapi` and `pyusb` (the `device` extra)
- Official `/Applications/Ulanzi Studio.app` if you want Studio keys
- `~/.ghostdeck/bin/d200-color-agent` (ARM Linux binary; see below)

`detect`, `status`, and `play` exit `2` with an install hint if `hidapi` or
`pyusb` is missing. `build` and `quit` do not need them.

## Install

```bash
git clone https://github.com/laerad777/ghostdeck.git
cd ghostdeck
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[device]"
```

Put `adb` and `ffmpeg` on `PATH`. Homebrew ffmpeg plus Android
platform-tools is enough:

```bash
export PATH="$PATH:$HOME/Library/Android/sdk/platform-tools"
```

### Device agent

`d200-color-agent` is not in git and is not a one-command macOS build. Put a
prebuilt ARM Linux binary at `~/.ghostdeck/bin/d200-color-agent`, or point
`GHOSTDECK_AGENT_SOURCE` at one. `device/build-color-agent.sh` needs an ARMv7
Linux cross gcc **and** an ARM Linux `libturbojpeg.a` (Homebrew jpeg-turbo is
Mach-O and will be refused).

`ghostdeck build` compiles the zkgui proxy/preload and the hidshim Studio
copy. It needs Xcode CLT (`clang`, `codesign`, `ditto`, …) and the official
Studio app. It still fails at the agent step until that binary exists.

## Use

```bash
ghostdeck studio          # hidshim copy + bridge; run this first
ghostdeck play video.mp4  # JPEG play over ADB
ghostdeck stop            # stop the player; Studio stays if it is up
```

Do not open `~/Applications/Ulanzi Studio ADB.app` by hand. Without the
bridge that copy's shim sees no device.

## Commands

| Command | What it does |
| --- | --- |
| `ghostdeck studio` | Start the local hidshim copy and the bridge `play` talks to. Does not write official Studio. |
| `ghostdeck play FILE\|URL` | Play through the running bridge. Refuses if the bridge is down. |
| `ghostdeck stop` | Stop the player. If the bridge is down, restore stock UI and clear `/tmp/ghostdeck-*`. If the bridge is up, leave the deck in ADB so Studio keys keep working. |
| `ghostdeck detect` | Serial, VID/PID, USB mode. Fails if no deck. |
| `ghostdeck status` | USB mode, shim copy, playing. |
| `ghostdeck quit` | Stop the unused IOHID keeper, if any. |

`play` is a launcher: it returns after the player has survived a short grace
window. The player keeps looping until `stop`.

## Stop vs Studio

Three owners, one of them a `ghostdeck` command:

| Owner | Owns | Released by |
| --- | --- | --- |
| `ghostdeck stop` | the player; stock UI and `/tmp/ghostdeck-*` only when the bridge is **down** | `ghostdeck stop` |
| the bridge (`ghostdeck studio`) | staged `/tmp/d200-color-agent`, framebuffer black-out, ADB hold | quitting the bridge / hidshim copy |

There is no `ghostdeck` command that stops the bridge. If `stop` exits `0`
and the deck is still ADB with Studio open, that is expected. To return to
HID: quit the hidshim copy, then `ghostdeck stop`.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | success (`status` also uses `0` when no deck is attached) |
| `1` | no deck, or another failure |
| `2` | missing `hidapi` / `pyusb` |
| `3` | deck is attached as ADB but the transport will not run a command (`offline`) |

`detect` and `status` only read `adb devices`. They never restart the adb
server. Nothing is sent to a device that is not identified as the D200.

An `offline` deck is not a missing cable. Host-side reconnects do not recover
it; power-cycle or replug, then retry.

## Out of scope

- PyPI / a self-contained wheel
- Linux as a supported host in 0.1.0
- `functions=hid,adb`, firmware, kernel modules
- Redistributing Studio.app
- ARM binaries in git
- Hard-coded serials

## License

MIT. PRs need `Signed-off-by` (DCO). See [CONTRIBUTING.md](CONTRIBUTING.md).
