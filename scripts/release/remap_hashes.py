#!/usr/bin/env python3
"""Rewrite old commit ids to new ones in tracked text files, using git-filter-repo's commit-map.

usage: remap_hashes.py <commit-map> <checkout-dir>
Full 40-hex ids are replaced exactly; abbreviated ids (7-39 hex, bounded by non-hex characters) are replaced
when they are a unique prefix of exactly one old commit, keeping the same length.
"""

import re
import subprocess
import sys
from pathlib import Path

cmap_path, root = Path(sys.argv[1]), Path(sys.argv[2])
old_to_new: dict[str, str] = {}
for line in cmap_path.read_text().splitlines()[1:]:
    old, new = line.split()
    if set(new) != {"0"}:
        old_to_new[old] = new
olds = sorted(old_to_new)


def lookup(abbrev: str) -> str | None:
    import bisect

    i = bisect.bisect_left(olds, abbrev)
    hits = []
    while i < len(olds) and olds[i].startswith(abbrev):
        hits.append(olds[i])
        i += 1
        if len(hits) > 1:
            return None
    return old_to_new[hits[0]][: len(abbrev)] if hits else None


pat = re.compile(r"(?<![0-9a-f])[0-9a-f]{7,40}(?![0-9a-f])")
files = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout.split(b"\0")
changed = 0
for f in files:
    if not f:
        continue
    p = root / f.decode()
    try:
        text = p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
        continue
    count = 0

    def sub(m: re.Match) -> str:
        global count
        new = lookup(m.group(0))
        if new is None or new == m.group(0):
            return m.group(0)
        count += 1
        return new

    out = pat.sub(sub, text)
    if count:
        p.write_text(out, encoding="utf-8")
        changed += 1
        print(f"{count:4d}  {f.decode()}")
print(f"files changed: {changed}")
