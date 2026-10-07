"""Dry-run 3-way diff of vendored subtrees against upstream. Read-only to the repo; temp dirs only."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UPSTREAM_URLS = {
    "tuios": "https://github.com/Gaurav-Gosain/tuios",
    "hermes-agent": "https://github.com/NousResearch/hermes-agent",
}
SKIP_PARTS = {"node_modules", "dist", "__pycache__", ".git"}


def git(*args: str, cwd: Path | str, check: bool = True, timeout: int = 600) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()[-300:]}")
    return p.stdout


def blobs(repo: Path | str, rev: str, path: str) -> dict[str, str]:
    """{relative path: blob sha} for a tree (or single file) at rev; {} when absent."""
    spec = f"{rev}:{path}" if path not in (".", "") else f"{rev}^{{tree}}"
    kind = git("cat-file", "-t", spec, cwd=repo, check=False).strip()
    if kind == "blob":
        return {"": git("rev-parse", spec, cwd=repo).strip()}
    if kind != "tree":
        return {}
    out: dict[str, str] = {}
    for ln in git("ls-tree", "-r", spec, cwd=repo).splitlines():
        meta, name = ln.split("\t", 1)
        if not SKIP_PARTS & set(name.split("/")):
            out[name] = meta.split()[2]
    return out


def blob_bytes(repo: Path | str, sha: str | None) -> bytes:
    if sha is None:
        return b""
    return subprocess.run(["git", "cat-file", "blob", sha], cwd=repo, capture_output=True, check=False).stdout


def textual_conflicts(files: list[str], trio: tuple[Path | str, Path | str, Path | str],
                      shas: dict[str, tuple[str | None, str | None, str | None]]) -> list[str]:
    """Files among `files` where `git merge-file` cannot merge base/ours/theirs cleanly."""
    bad: list[str] = []
    with tempfile.TemporaryDirectory(prefix="k3-mf-") as t:
        for f in files:
            b, o, th = shas[f]
            paths = []
            for n, (repo, sha) in enumerate(zip(trio, (b, o, th), strict=True)):
                q = Path(t) / f"{n}"
                q.write_bytes(blob_bytes(repo, sha))
                paths.append(str(q))
            r = subprocess.run(["git", "merge-file", "-p", "--quiet", paths[1], paths[0], paths[2]],
                               capture_output=True, check=False)
            if r.returncode != 0:  # >0 = number of conflicts, <0 = error
                bad.append(f)
    return bad


FROZEN_DEFAULT = ("hermes-agent:tui",)


def three_way(base: dict[str, str], ours: dict[str, str], theirs: dict[str, str]) -> dict[str, list[str]]:
    res: dict[str, list[str]] = {"conflicts": [], "theirs_only": [], "ours_only": [], "identical": []}
    for f in sorted(set(base) | set(ours) | set(theirs)):
        b, o, t = base.get(f), ours.get(f), theirs.get(f)
        if o == t:
            res["identical"].append(f)
        elif o == b:
            res["theirs_only"].append(f)  # only upstream changed: clean merge
        elif t == b:
            res["ours_only"].append(f)  # only we changed: clean merge
        else:
            res["conflicts"].append(f)  # both changed differently
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--threshold", type=int, default=10)
    # Frozen forks are reported but not merged, so they do not count against the threshold (docs/UPSTREAM.md).
    ap.add_argument("--frozen", action="append", default=None,
                    help="subtree to report as a frozen fork (default: hermes-agent:tui)")
    a = ap.parse_args()
    frozen = set(a.frozen if a.frozen is not None else FROZEN_DEFAULT)
    vendor = tomllib.loads((REPO / "VENDOR.toml").read_text())
    entries = [(e["project"], e["upstream_path"], e["local_path"], e["commit"], "tree") for e in vendor.get("tree", [])
               if e["project"] in UPSTREAM_URLS]
    entries += [(e["project"], e["upstream_path"], e["local_path"], e["commit"], "file") for e in vendor.get("file", [])
                if e["project"] == "hermes-agent" and not e.get("port") and not e.get("port-to-python")
                and "note" not in e]
    ups = tomllib.loads((REPO / "panes" / "UPSTREAM.toml").read_text())
    entries = [e for e in entries if e[0] != "tuios"] + [("tuios", ".", "panes", ups["commit"], "tree")]
    subtrees: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="k3-sync-") as tmp:
        for project, url in UPSTREAM_URLS.items():
            d = Path(tmp) / project
            try:
                git("init", "--bare", "-q", str(d), cwd=tmp)
                git("fetch", "-q", "--filter=blob:none", url, "HEAD", cwd=d, timeout=900)
                git("update-ref", "refs/heads/upstream", "FETCH_HEAD", cwd=d)
                for c in sorted({e[3] for e in entries if e[0] == project}):
                    if git("cat-file", "-t", c, cwd=d, check=False).strip() != "commit":
                        git("fetch", "-q", "--filter=blob:none", url, c, cwd=d, timeout=900)
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                print(f"PENDING: upstream unreachable: {project}: {e}")
                return 3
            head = git("rev-parse", "upstream", cwd=d).strip()
            for p, up_path, local, base_c, kind in entries:
                if p != project:
                    continue
                base = blobs(d, base_c, up_path)
                theirs = blobs(d, "upstream", up_path)
                ours = blobs(REPO, "HEAD", local)
                if project == "hermes-agent" and local == "tui":  # tui/shared is its own vendored tree
                    ours = {k: v for k, v in ours.items() if not k.startswith("shared/")}
                r = three_way(base, ours, theirs)
                both = r["conflicts"]
                shas = {f: (base.get(f), ours.get(f), theirs.get(f)) for f in both}
                r["conflicts"] = textual_conflicts(both, (d, REPO, d), shas)
                subtrees[f"{project}:{local}"] = {
                    "project": project, "upstream_path": up_path, "base": base_c[:10], "upstream_head": head[:10],
                    "conflicts": len(r["conflicts"]), "theirs_only": len(r["theirs_only"]),
                    "ours_only": len(r["ours_only"]), "both_changed": len(both), "base_missing": not base, "examples": r["conflicts"][:5]}
    agg: dict[str, int] = {}
    upstream_changed: dict[str, int] = {}
    for k, v in subtrees.items():
        name = k if k.split(":")[1] in ("tui", "tui/shared", "panes") else "hermes-agent:vendored files"
        agg[name] = agg.get(name, 0) + v["conflicts"]
        upstream_changed[name] = upstream_changed.get(name, 0) + v["theirs_only"] + v["conflicts"]
    worst = max((n for k, n in agg.items() if k not in frozen), default=0)
    if a.json:
        print(json.dumps({"subtrees": subtrees, "aggregate": agg, "threshold": a.threshold}, indent=1))
    else:
        for k, v in subtrees.items():
            print(f"{k:45s} base {v['base']} -> upstream {v['upstream_head']}: conflicts={v['conflicts']} "
                  f"upstream-only={v['theirs_only']} ours-only={v['ours_only']}"
                  + (" (base path missing upstream)" if v["base_missing"] else ""))
        for k, n in agg.items():
            if k in frozen:
                print(f"SUBTREE {k}: {n} conflicting file(s); FROZEN FORK, not merged: {upstream_changed[k]} files "
                      f"changed upstream since the base, to be cherry-picked by hand (docs/UPSTREAM.md)")
            else:
                print(f"SUBTREE {k}: {n} conflicting file(s) (target <{a.threshold}) {'OK' if n < a.threshold else 'OVER'}")
    return 0 if worst < a.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
