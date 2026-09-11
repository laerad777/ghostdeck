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
# A deck that is attached but whose adb transport cannot run a command is a third condition: not an
# unusable environment and not an absent deck. The wedged deck reports `offline`. Its own code keeps
# the three apart for a script - "replug the deck" is not "install hidapi" and not "no deck".
_OFFLINE_EXIT = 3
# Shared by `detect` and `status`: both must name what the user can actually do about a wedged
# transport, because nothing on the host can reset it.
_RECOVERY_HINT = "power-cycle or replug the deck (ghostdeck cannot recover it from the host)"


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


def _transport_problem(mode: str) -> tuple[str, str]:
    """``(state, detail)`` for an attached-but-unusable deck transport, else ``("", "")``.

    Only consulted for an ADB-mode deck: ADB is the only transport a command travels over, and an
    HID-mode deck is not a defect (it is the deck's normal resting mode). `restart=False` keeps these
    reporting commands read-only - a diagnostic must not reset the adb server it is reporting on
    (A-134) - so this answers "is the transport usable right now?", never "can I make it usable?".

    `detail` is a clause each caller can put in front of the same explanation. The second branch fires
    on the real host whenever the USB backend is unusable (the venv has neither hidapi nor pyusb),
    which is exactly the case where the old wording claimed nothing was attached while `adb` was
    listing it - and it keeps the honest hedge, because such a device cannot be proven to be the deck
    and nothing is ever sent to it (A-137).
    """
    if mode != "adb":
        return "", ""
    serial, state, blocked = playmod.deck_transport(restart=False)
    if serial and state and state != playmod.TRANSPORT_READY:
        return state, f"the deck ({serial}) is attached but its adb transport is {state}"
    if serial is None and blocked:
        first_serial, first_state = blocked[0]
        others = len(blocked) - 1
        listed = f"{first_serial} and {others} other device{'s' if others != 1 else ''}" if others else first_serial
        return first_state, (
            f"{listed} is attached but its adb transport is {first_state}"
            f", and it was not identified as the D200"
        )
    return "", ""


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
    state, detail = _transport_problem(found["mode"])
    if state:
        # T16: `mode=adb` is not a success when the deck cannot execute a single command. A plain
        # success here is how a wedged deck looked healthy, so the state is surfaced and the exit is
        # distinct from both "no deck" (1) and "unusable environment" (2).
        print(f"mode={found['mode']} ({state}): {detail}; {_RECOVERY_HINT}", file=sys.stderr)
        return _OFFLINE_EXIT
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
    state, detail = "", ""
    if not dependency:
        state, detail = _transport_problem(mode)
    if state:
        # `usb=adb` alone read as healthy. The mode is real, so it is kept and annotated.
        mode = f"{mode} ({state})"
        print(f"{detail}; {_RECOVERY_HINT}", file=sys.stderr)
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
    if dependency:
        return _ENV_EXIT
    return _OFFLINE_EXIT if state else 0


if __name__ == "__main__":
    raise SystemExit(main())
