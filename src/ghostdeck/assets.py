from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ghostdeck import state as gdstate
from ghostdeck import tree

MANIFEST = tree.candidate_root() / "manifest" / "0.1.0.json"
ZERO = "0" * 64


def ensure_release_bins() -> None:
    if not MANIFEST.is_file():
        return
    data = json.loads(MANIFEST.read_text())
    assets = data.get("assets") or {}
    gdstate.BIN_DIR.mkdir(parents=True, exist_ok=True)
    for name, meta in assets.items():
        sha = (meta or {}).get("sha256") or ""
        if sha == ZERO or len(sha) != 64:
            continue
        dest = gdstate.BIN_DIR / name.replace(".gz", "")
        if dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == sha:
            continue
        # Real URL filled when Releases exist. Zero hashes mean skip.
        raise RuntimeError(f"release asset {name} has sha256 but no fetch URL in 0.1.0")
