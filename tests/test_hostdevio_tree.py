"""C-158: 0.1.0 is checkout-only, and it has to say so instead of guessing paths.

`pyproject.toml` declared a console script and `[tool.setuptools.packages.find] where = ["src"]`, so a
built wheel contains `ghostdeck/*.py` and nothing else -- while every runtime asset lives outside the
package (`vendor/`, `device/`, `reference/`, `manifest/`). Every module resolved those with
`Path(__file__).resolve().parents[2]`, which is correct in a checkout and *wrong but plausible* when
installed: the expression lands on the directory containing the install location, so the CLI read
real-looking paths unrelated to the checkout and reported four different irrelevant errors.

An installed `ghostdeck build` used to print `missing hidshim source: /private/tmp/reference/hidshim.c`
and `ghostdeck play <file>` used to print `device sources missing under /private/tmp/device`, neither of
which names the actual problem.

So the tests below are about the *shape* of the failure, not about a path string:

* an installed copy must refuse with exactly one line naming the source checkout, and must not produce
  any of the old per-file messages;
* the resolver must not trust a directory that merely looks right (a planted `vendor/`);
* the recovery commands (`stop`, `quit`) and the read-only diagnostics (`detect`, `status`) must keep
  working without a checkout, because that is what a user needs when `play` cannot run;
* every runtime asset path must sit under the one root the resolver validates, so a new asset cannot
  be added outside the checked set.

Every subprocess runs with a temp HOME (BRIEF rule 7) and imports the copied package through
PYTHONPATH, so the real checkout is never consulted. No device is contacted and no network is used --
the wheel itself is built by the acceptance command, not here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

# `ghostdeck.tree` is imported inside the tests that need it rather than at module scope, so that the
# subprocess tests below still collect against a revision that has no resolver. They are the ones that
# must fail *on their assertions* (with the old per-file error messages) rather than on an import,
# which is what makes them a real regression pin for C-158.


def _installed_copy(tmp_path: Path) -> Path:
    """A wheel-like install: the package files only, with no runtime assets anywhere near them.

    This is what `pip install ghostdeck` produces. `candidate_root()` for this layout is
    `tmp_path`, which holds none of `vendor/`, `device/`, `reference/`, `manifest/`.
    """
    site = tmp_path / "site"
    site.mkdir()
    shutil.copytree(SRC / "ghostdeck", site / "ghostdeck")
    return site


def _run_installed(tmp_path: Path, site: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the CLI as the installed copy, with a temp HOME and no other ghostdeck on the path."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "PYTHONPATH": str(site),
    }
    return subprocess.run(
        [sys.executable, "-m", "ghostdeck", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )


@pytest.mark.parametrize("command", [("build",), ("studio",), ("play", "/tmp/does-not-matter.mov")])
def test_an_installed_copy_refuses_in_one_line_naming_the_source_tree(tmp_path, command):
    """The headline: one line that names the real problem, for every command that needs the tree.

    The old behaviour is asserted against by name, because "the message changed" is not the point --
    the point is that the user is no longer told about a hidshim source or a device directory at a
    path that has nothing to do with the checkout.
    """
    site = _installed_copy(tmp_path)
    result = _run_installed(tmp_path, site, *command)
    combined = result.stdout + result.stderr
    lines = [line for line in result.stderr.splitlines() if line.strip()]

    assert result.returncode == 2, f"an environment problem must not read as a hardware one: {combined}"
    assert len(lines) == 1, f"expected exactly one clear line, got {lines}"
    message = lines[0]
    assert "source checkout" in message, message
    assert "pip install -e" in message, message
    # And none of the four unrelated failures the guessing produced.
    for old in (
        "missing hidshim source",
        "device sources missing under",
        "vendor player missing",
        "bridge missing",
        "Traceback",
    ):
        assert old not in combined, f"the installed copy still reports {old!r}: {combined}"


def test_the_recovery_and_diagnostic_commands_still_work_without_a_checkout(tmp_path):
    """`stop`/`quit` restore the deck; refusing them for a missing checkout would remove the only
    command that undoes a hijacked deck. `detect`/`status` are host-side and must stay informative."""
    site = _installed_copy(tmp_path)
    for command in (("stop",), ("quit",), ("detect",), ("status",)):
        result = _run_installed(tmp_path, site, *command)
        combined = result.stdout + result.stderr
        assert "source checkout" not in combined, f"`{command[0]}` was gated on the tree: {combined}"
        assert "Traceback" not in combined, combined


def test_a_planted_partial_tree_is_not_trusted(tmp_path, monkeypatch):
    """The safety property behind the fix: a directory that merely *looks* like the tree is not it.

    `parents[2]` pointed at a real location outside the package, so anything that put a
    `vendor/d200-color-play.py` and `vendor/d200-local-bridge.py` there would have been executed by
    the CLI. The resolver requires all four asset directories, so a partial plant is refused rather
    than used.
    """
    from ghostdeck import tree
    site = _installed_copy(tmp_path)
    planted = site.parent / "vendor"
    planted.mkdir()
    (planted / "d200-color-play.py").write_text("print('planted')\n", encoding="utf-8")

    monkeypatch.setattr(tree, "__file__", str(site / "ghostdeck" / "tree.py"))
    # The plant is exactly where the old expression looked, and it is convincing at a glance.
    assert tree.candidate_root() == site.parent
    assert (tree.candidate_root() / "vendor" / "d200-color-play.py").is_file()

    assert tree.available() is False
    with pytest.raises(tree.TreeNotFound) as excinfo:
        tree.root()
    assert "device" in str(excinfo.value), "the missing directories must be named"


def test_a_complete_tree_is_accepted_and_every_asset_dir_is_checked(tmp_path, monkeypatch):
    """The other direction: a real tree passes, and the check covers every directory assets live in.

    Without this, the resolver could be 'fixed' into always refusing and every installed-copy test
    above would still pass.
    """
    from ghostdeck import tree
    root = tmp_path / "checkout"
    (root / "src" / "ghostdeck").mkdir(parents=True)  # mirrors <checkout>/src/ghostdeck
    for name in tree.REQUIRED:
        (root / name).mkdir()
    monkeypatch.setattr(tree, "__file__", str(root / "src" / "ghostdeck" / "tree.py"))

    assert tree.root() == root
    assert tree.available() is True
    # Drop any one directory and it must refuse: no member of REQUIRED is decorative.
    for name in tree.REQUIRED:
        shutil.rmtree(root / name)
        with pytest.raises(tree.TreeNotFound):
            tree.root()
        (root / name).mkdir()


def test_every_runtime_asset_is_under_the_root_the_resolver_validates():
    """A new asset outside these directories would silently reintroduce the guessing.

    This is the 'walk the declared locations' half of the task: the constants the CLI uses must all
    live under the one root, and each must be in a directory `tree.REQUIRED` actually validates.
    """
    from ghostdeck import tree
    from ghostdeck import assets, devicebuild, play, studio

    root = tree.root()
    under_root = {
        "play.VENDOR_PLAY": play.VENDOR_PLAY,
        "play.VENDOR_DIR": play.VENDOR_DIR,
        "studio.VENDOR": studio.VENDOR,
        "studio.BRIDGE": studio.BRIDGE,
        "studio.HIDSHIM_SRC": studio.HIDSHIM_SRC,
        "devicebuild.DEVICE": devicebuild.DEVICE,
        "devicebuild.VENDOR": devicebuild.VENDOR,
        "devicebuild.AGENT_RECIPE": devicebuild.AGENT_RECIPE,
        "assets.MANIFEST": assets.MANIFEST,
    }
    assert studio.ROOT == devicebuild.ROOT == root

    for name, path in under_root.items():
        relative = path.relative_to(root)
        assert relative.parts, name
        assert relative.parts[0] in tree.REQUIRED, (
            f"{name} lives under {relative.parts[0]!r}, which the resolver does not validate: {path}"
        )


def test_the_packaging_does_not_claim_to_ship_the_runtime_assets():
    """`pyproject.toml` must state checkout-only rather than implying a wheel is enough.

    The declaration is prose, so it is pinned the only honest way: the file must say so, and it must
    not claim to ship the trees a wheel cannot contain.
    """
    from ghostdeck import tree
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "CHECKOUT-ONLY" in text, "pyproject must state that 0.1.0 is checkout-only"
    assert "pip install -e" in text, "the supported install must be named"
    lowered = text.lower()
    for directory in tree.REQUIRED:
        assert directory + "/" in text, f"the runtime asset directory {directory}/ is not named"
    # No declaration that would make a wheel carry them, which is the false claim being removed.
    assert "include-package-data = true" not in lowered
    assert "[tool.setuptools.data-files]" not in text
