"""Behavioral proof for the public-tree privacy gate: untracked state is skipped, real leaks are not.

Loads `tests/test_ghostdeck_offline.py` by path and repoints its module-level `ROOT` at a temp
tree, so both directions are exercised without dirtying the real repository.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OFFLINE = ROOT / "tests" / "test_ghostdeck_offline.py"
HOME = "/Users/" + "mose"
LEAK = f'home = "{HOME}"\n'

# Untracked-on-purpose state that must never fail the gate.
UNTRACKED_STATE = (
    ".git/objects/ab/cdef0123",
    ".gjc/_session-01a08e6c/runtime/runtime-state.json",
    ".venv/lib/python3.13/site-packages/ghostdeck.py",
    "__pycache__/cli.cpython-313.py",
    ".pytest_cache/CACHEDIR.TAG",
    "build/lib/pkg.egg-info/PKG-INFO",
)

# Tracked path classes that must still be walked and still fail on a real leak.
TRACKED_SAMPLES = (
    "device/d200-zkgui-proxy.c",
    "vendor/d200-color-play.py",
    "src/ghostdeck/cli.py",
    "tests/other_test.py",
    "manifest/0.1.0.json",
    "reference/hidshim.c",
    "README.md",
)


def _gate():
    spec = importlib.util.spec_from_file_location("ghostdeck_offline_gate", OFFLINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plant(root: Path, relatives: tuple[str, ...]) -> Path:
    for relative in relatives:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(LEAK, encoding="utf-8")
    return root


def test_gate_skips_untracked_state(tmp_path):
    gate = _gate()
    gate.ROOT = _plant(tmp_path / "untracked", UNTRACKED_STATE)
    gate.test_public_tree_has_no_lab_identity()


def test_gate_bites_on_every_tracked_path_class(tmp_path):
    gate = _gate()
    for index, relative in enumerate(TRACKED_SAMPLES):
        gate.ROOT = _plant(tmp_path / f"tracked{index}", (relative,))
        with pytest.raises(AssertionError):
            gate.test_public_tree_has_no_lab_identity()


def test_gate_passes_on_clean_tracked_tree(tmp_path):
    gate = _gate()
    root = tmp_path / "clean"
    (root / "device").mkdir(parents=True)
    (root / "device" / "d200-color-agent.c").write_text("int main(void) { return 0; }\n")
    (root / "src").mkdir()
    (root / "src" / "ghostdeck.py").write_text("__version__ = '0.1.0'\n")
    gate.ROOT = root
    gate.test_public_tree_has_no_lab_identity()
