from __future__ import annotations

import argparse
import sys

from ghostdeck import devicebuild
from ghostdeck import play as playmod
from ghostdeck import studio, usb, vhid

# A missing optional backend is not a hardware verdict, so it gets its own exit code (A-102).
# Before this, `detect` printed "no device" and exited 1 for both "no deck is attached" and
# "your Python environment cannot see any device at all", which is how a missing package got
# reported as a missing deck. 2 is also argparse's usage-error code, so it reads the same way to a
# script: the invocation/environment is wrong, not the hardware.
_ENV_EXIT = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ghostdeck", description="D200 JPEG play + hidshim Studio copy")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("detect")
    sub.add_parser("status")
    sub.add_parser("stop")
    sub.add_parser("quit")
    sub.add_parser("studio")
    sub.add_parser("build")
    p_play = sub.add_parser("play")
    p_play.add_argument("source")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "detect":
            return _detect()
        if args.cmd == "status":
            return _status()
        if args.cmd == "play":
            playmod.start_play(args.source)
            return 0
        if args.cmd == "stop":
            playmod.stop()
            return 0
        if args.cmd == "quit":
            vhid.quit()
            return 0
        if args.cmd == "studio":
            studio.launch()
            return 0
        if args.cmd == "build":
            studio.ensure_copy()
            devicebuild.ensure()
            return 0
    except usb.MissingDependency as error:
        # The backend, not the deck (A-102). No command can reach a hardware conclusion here, so
        # every one of them reports the environment and exits with the environment code.
        print(error, file=sys.stderr)
        return _ENV_EXIT
    except Exception as error:
        print(error, file=sys.stderr)
        return 1
    return 2


def _detect() -> int:
    found = usb.detect()
    dependency = (found or {}).get("dependency")
    if dependency:
        # "no device" here would blame the deck for a missing package (A-102).
        print(dependency, file=sys.stderr)
        return _ENV_EXIT
    if found is None or found.get("mode") in (None, "none"):
        print("no device", file=sys.stderr)
        return 1
    print(f"serial={found['serial']} vid={found['vid']:04x} pid={found['pid']:04x} mode={found['mode']}")
    return 0


def _status() -> int:
    found = usb.detect()
    dependency = (found or {}).get("dependency")
    # `usb=unknown` rather than `usb=none`: with an unusable backend there is no device verdict to
    # report, and the rest of the line (vhid, shim, copy, playing) is host-side and still true, so
    # the diagnostic keeps its value while the false conclusion and the false success code go.
    mode = found["mode"] if found else "none"
    if dependency:
        print(dependency, file=sys.stderr)
        mode = "unknown"
    record = vhid.status()
    print(
        f"usb={mode} vhid={'up' if record.get('status') == 'up' else 'down'} "
        f"iohid={'yes' if record.get('iohid') else 'no'} "
        f"visible={'yes' if record.get('visible') else 'no'} "
        f"shim={'up' if studio.running() else 'down'} "
        f"copy={'yes' if studio.copy_exists() else 'no'} "
        f"release_gate={record.get('release_gate', 'blocked')} "
        f"playing={'yes' if playmod.playing() else 'no'}"
    )
    return _ENV_EXIT if dependency else 0


if __name__ == "__main__":
    raise SystemExit(main())
