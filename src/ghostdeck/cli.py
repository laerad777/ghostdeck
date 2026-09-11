from __future__ import annotations

import argparse
import sys

from ghostdeck import devicebuild
from ghostdeck import play as playmod
from ghostdeck import studio, usb, vhid


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
    except Exception as error:
        print(error, file=sys.stderr)
        return 1
    return 2


def _detect() -> int:
    found = usb.detect()
    if found is None or found.get("mode") in (None, "none"):
        print("no device", file=sys.stderr)
        return 1
    print(f"serial={found['serial']} vid={found['vid']:04x} pid={found['pid']:04x} mode={found['mode']}")
    return 0


def _status() -> int:
    found = usb.detect()
    mode = found["mode"] if found else "none"
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
