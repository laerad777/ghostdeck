#!/usr/bin/env python3
"""Check real DCO trailers on every non-merge commit in a PR's base..head range."""
from __future__ import annotations

import re
import subprocess
import sys


SIGNOFF = re.compile(r"Signed-off-by:\s+[^<>\s][^<>]*\s+<[^<>\s@]+@[^<>\s@]+>", re.IGNORECASE)


def git(*args: str, input: str | None = None) -> str:
    return subprocess.run(
        ["git", *args], input=input, text=True, capture_output=True, check=True
    ).stdout


def check(base: str, head: str) -> int:
    # Resolve first so invalid/missing refs or option-like input cannot become an
    # empty successful check. Never hide rev-list's exit status in process substitution.
    base_sha = git("rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").strip()
    head_sha = git("rev-parse", "--verify", "--end-of-options", f"{head}^{{commit}}").strip()
    commits = git("rev-list", "--no-merges", f"{base_sha}..{head_sha}").splitlines()
    missing = []
    for sha in commits:
        message = git("log", "-1", "--format=%B", sha)
        # Git parses the final trailer block, ignoring lookalikes in the subject/body.
        trailers = git("interpret-trailers", "--parse", input=message)
        if not any(SIGNOFF.fullmatch(line) for line in trailers.splitlines()):
            missing.append(sha)
            print(f"missing valid Signed-off-by trailer: {sha}", file=sys.stderr)
    if missing:
        return 1
    print(f"DCO: checked {len(commits)} non-merge commit(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: check_dco.py BASE HEAD", file=sys.stderr)
        return 2
    try:
        return check(*args)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"DCO: cannot verify commit range: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
