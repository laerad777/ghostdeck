"""Where the runtime assets are, and the one honest message when they are not there.

The runtime half of this product is not inside the Python package: `vendor/` holds the player and the
bridge, `device/` holds the C sources and the cross-build recipe, `reference/` holds the hidshim
source and `manifest/` the release manifest. A built wheel contains only `ghostdeck/*.py`, so an
installed copy has none of them.

Every module used to guess with `Path(__file__).resolve().parents[2]`. That is right in a checkout
(`src/ghostdeck/x.py` -> repo root) and wrong-but-plausible installed: the same expression resolves to
the directory *containing* the install location, so the CLI read real-looking paths that had nothing
to do with the checkout and reported four different wrong things -- e.g. an installed `ghostdeck
build` said `missing hidshim source: /private/tmp/reference/hidshim.c`, and `play` said `device
sources missing under /private/tmp/device`. None of those messages names the actual problem, and none
of them was reachable from `--help`, so the failure looked like a broken deck rather than a broken
install (C-158).

This module is the single answer to "which tree?", and it answers with a *validated* root: all four
directories must be present. `candidate_root()` never raises, so module-level constants stay
importable and can be printed for a diagnosis; `root()` validates and raises `TreeNotFound`, which is
the one message the user gets.

0.1.0 is checkout-only and `pyproject.toml` says so. This is not a short-term patch: making the wheel
self-contained means moving the runtime half into the package (`ghostdeck._vendor`) and loading it
through `importlib.resources`, which is a real change to how the bridge and the player are launched.
Until that exists, claiming installability would be the lie this module exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

# The directories every runtime path hangs off. All four are present in a checkout and absent from a
# wheel, which is exactly what makes them the validation set.
REQUIRED = ("vendor", "device", "reference", "manifest")


class TreeNotFound(RuntimeError):
    """The runtime assets are not next to the installed package (a non-checkout install)."""


def candidate_root() -> Path:
    """The directory that would hold the asset directories, whether or not it does.

    Deliberately non-raising: the module-level `VENDOR`/`DEVICE`/... constants below are built from
    this so that importing a module never depends on the checkout, and so a diagnostic can print the
    path that was actually consulted. `root()` is the validating form.
    """
    # `.../src/ghostdeck/tree.py` -> `.../src` -> the checkout root; in a wheel, `.../site-packages/
    # ghostdeck/tree.py` -> `.../site-packages` -> the install's library directory.
    return Path(__file__).resolve().parents[1].parent


def root() -> Path:
    """The checkout root, validated. Raises `TreeNotFound` with one actionable line."""
    candidate = candidate_root()
    missing = [name for name in REQUIRED if not (candidate / name).is_dir()]
    if not missing:
        return candidate
    raise TreeNotFound(
        "ghostdeck 0.1.0 runs from a source checkout: its runtime assets "
        f"({', '.join(REQUIRED)}) are not shipped in the installed package, and {candidate} has no "
        f"{', '.join(missing)}. Clone the repository and run ghostdeck from there, or use "
        "`pip install -e .` from the checkout."
    )


def available() -> bool:
    """True when this is a real checkout. Never raises; for callers that only want to warn."""
    try:
        root()
    except TreeNotFound:
        return False
    return True
