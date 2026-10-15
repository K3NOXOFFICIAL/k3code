#!/usr/bin/env python3
"""Third-party notices for the k3code release assets, and a check that a wheel carries k3code's license files.

    third_party_notices.py generate [--own ROOT] [--npm-lock LOCK [--node-modules DIR]]
                                    [--go-modules JSON [--goroot DIR]] -o OUT
    third_party_notices.py check-wheel [--repo ROOT] (WHEEL | --build)

`generate` writes a plain-text notice file for what the given parts ship (issue #165):

  --own ROOT         k3code's NOTICE and every license text in ROOT/LICENSES/ (the code k3code vendors)
  --npm-lock LOCK    every production package of an npm package-lock.json (lockfileVersion 2 or 3): name, version,
                     the license package-lock.json records, and the package's license files from DIR (default: the
                     node_modules beside LOCK) when it is installed. Dev packages and workspace links (the TUI's own
                     packages) are left out. A package without a license fails the run.
  --go-modules JSON  `go list -deps -json=Module ./cmd/k3` output, one run per platform concatenated: every module
                     linked into the binary except the main one, with the license files from its directory in the
                     module cache. A module without a directory or without a license file fails the run.
  --goroot DIR       the Go standard library and runtime, which every Go binary contains (DIR/LICENSE)

`check-wheel` exits 1 unless WHEEL holds k3code/LICENSE, k3code/NOTICE and k3code/LICENSES/; with --repo, the same
files byte for byte as ROOT's LICENSE, NOTICE and LICENSES/. --build (with --repo) first builds the wheel of ROOT/core
with `uv build --wheel` into a temporary directory, as release.sh does.

Standard library only: release.sh and check.sh run it with any python3.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

#: The files that carry a package's or module's license and notices, at the top of its directory.
LICENSE_FILE = re.compile(r"(?i)^(licen[cs]e|copying|notice|patents|unlicense)([.\-_].*)?$")
RULE = "-" * 78
#: What every k3code wheel carries under k3code/ (core/hatch_build.py puts them there).
WHEEL_FILES = ("LICENSE", "NOTICE")


class NoticeError(Exception):
    """A part that cannot be described: its notice would be missing or wrong."""


@dataclass
class Component:
    name: str
    version: str
    license: str  # as the package metadata states it, or what the text looks like (Go modules: no metadata)
    texts: list[tuple[str, str]] = field(default_factory=list)  # (file name, text)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip("\n")


def license_texts(directory: Path) -> list[tuple[str, str]]:
    """The license, notice and patent files at the top of ``directory``, sorted by name."""
    if not directory.is_dir():
        return []
    return [(p.name, _read(p)) for p in sorted(directory.iterdir()) if p.is_file() and LICENSE_FILE.match(p.name)]


def guess_license(texts: list[tuple[str, str]]) -> str:
    """A short name for a license text, for the summary only: the full text is what the notice ships."""
    body = " ".join(t for name, t in texts if not name.upper().startswith(("NOTICE", "PATENTS")))
    flat = " ".join(body.split())
    if "Apache License" in flat and "Version 2.0" in flat:
        return "Apache-2.0"
    if "Mozilla Public License" in flat and "2.0" in flat:
        return "MPL-2.0"
    if "Permission is hereby granted, free of charge" in flat:
        return "MIT"
    if "Permission to use, copy, modify, and/or distribute" in flat:
        return "ISC"
    if "Redistribution and use in source and binary forms" in flat:
        return "BSD-3-Clause" if "Neither the name" in flat or "names of its contributors" in flat else "BSD-2-Clause"
    return "see the license text"


def _npm_license(meta: dict) -> str:
    raw = meta.get("license")
    if isinstance(raw, dict):  # the old {"type": "MIT", "url": ...} form
        raw = raw.get("type")
    if not raw and isinstance(meta.get("licenses"), list):  # the older "licenses": [{"type": ...}] form
        raw = " OR ".join(str(x.get("type")) for x in meta["licenses"] if isinstance(x, dict) and x.get("type"))
    return str(raw).strip() if raw else ""


def npm_components(lock: Path, node_modules: Path | None = None) -> list[Component]:
    """The production packages of an npm lockfile (v2/v3 ``packages`` map), one per name and version."""
    data = json.loads(lock.read_text(encoding="utf-8"))
    packages = data.get("packages")
    if not isinstance(packages, dict):
        raise NoticeError(f"{lock}: no 'packages' map (npm lockfileVersion 2 or 3 is needed)")
    base = lock.parent
    found: dict[tuple[str, str], Component] = {}
    missing: list[str] = []
    for key, meta in sorted(packages.items()):
        # "" is the project itself and "packages/x" a workspace: k3code's own code, covered by --own
        if not isinstance(meta, dict) or not (key.startswith("node_modules/") or "/node_modules/" in key):
            continue
        if meta.get("dev") or meta.get("link"):
            continue  # a build or test tool, or a link to a workspace package
        name = str(meta.get("name") or key.rsplit("node_modules/", 1)[1])
        version = str(meta.get("version") or "")
        lic = _npm_license(meta)
        if not lic:
            missing.append(f"{name} {version}".strip())
            continue
        directory = base / key
        if node_modules is not None and key.startswith("node_modules/"):
            directory = node_modules / key.removeprefix("node_modules/")
        comp = found.setdefault((name, version), Component(name, version, lic))
        if not comp.texts:
            comp.texts = license_texts(directory)
    if missing:
        raise NoticeError(f"{lock}: no license recorded for {', '.join(missing)}")
    return sorted(found.values(), key=lambda c: (c.name.lower(), c.version))


def _json_stream(text: str) -> list[dict]:
    """Concatenated JSON objects, as `go list -json` prints them (and several runs appended to one file)."""
    decoder = json.JSONDecoder()
    out: list[dict] = []
    i = 0
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return out
        obj, i = decoder.raw_decode(text, i)
        if isinstance(obj, dict):
            out.append(obj)


def go_components(modules_json: Path) -> list[Component]:
    """The modules of `go list -deps -json=Module` (or `go list -m -json`) output, except the main module."""
    found: dict[tuple[str, str], Component] = {}
    problems: list[str] = []
    for obj in _json_stream(modules_json.read_text(encoding="utf-8")):
        mod = obj.get("Module", obj) if "Module" in obj or "Path" in obj else None
        if not isinstance(mod, dict) or not mod.get("Path") or mod.get("Main"):
            continue  # a standard-library package (no module) or k3 itself
        path, version = str(mod["Path"]), str(mod.get("Version") or "")
        if (path, version) in found:
            continue
        real = mod.get("Replace") if isinstance(mod.get("Replace"), dict) else mod
        directory = real.get("Dir")
        if not directory:
            problems.append(f"{path} {version}: no module directory (run `go mod download` in panes/)")
            continue
        texts = license_texts(Path(directory))
        if not any(not n.upper().startswith(("NOTICE", "PATENTS")) for n, _ in texts):
            problems.append(f"{path} {version}: no license file in {directory}")
            continue
        name = path if real is mod else f"{path} (replaced by {real.get('Path')} {real.get('Version') or ''})".strip()
        found[(path, version)] = Component(name, version, guess_license(texts), texts)
    if problems:
        raise NoticeError("Go modules without a license to ship:\n  " + "\n  ".join(problems))
    return sorted(found.values(), key=lambda c: (c.name.lower(), c.version))


def go_runtime(goroot: Path) -> Component:
    lic = goroot / "LICENSE"
    if not lic.is_file():
        raise NoticeError(f"{goroot}: no LICENSE (is it a Go installation? `go env GOROOT` in panes/ names it)")
    version_file = goroot / "VERSION"
    version = _read(version_file).splitlines()[0] if version_file.is_file() else ""
    texts = license_texts(goroot)
    return Component("Go standard library and runtime", version, guess_license(texts), texts)


def _section(title: str) -> list[str]:
    return ["", "=" * 78, title, "=" * 78, ""]


def _components(comps: list[Component]) -> list[str]:
    width = max((len(c.name) for c in comps), default=0)
    vwidth = max((len(c.version or "-") for c in comps), default=0)
    lines = [f"  {c.name.ljust(width)}  {(c.version or '-').ljust(vwidth)}  {c.license}" for c in comps]
    for c in comps:
        lines += ["", RULE, f"{c.name} {c.version}".strip(), f"License: {c.license}", RULE]
        if not c.texts:
            lines += ["", "(The installed package ships no license file; the license above is the one it declares.)"]
        for name, text in c.texts:
            lines += ["", f"--- {name}", "", text]
    return lines


def render(
    own: Path | None,
    npm: list[Component] | None,
    npm_label: str,
    go: list[Component] | None,
    runtime: Component | None,
) -> str:
    lines = [
        "k3code: third-party notices",
        "",
        "k3code is released under the MIT License. The release files contain code from the projects below; each one's",
        "license and notices follow. Generated by scripts/release/third_party_notices.py.",
    ]
    if own is not None:
        lines += _section("k3code and the code it vendors (NOTICE, LICENSES/)")
        lines += [_read(own / "NOTICE")]
        for p in sorted((own / "LICENSES").iterdir()):
            if p.is_file():
                lines += ["", RULE, f"LICENSES/{p.name}", RULE, "", _read(p)]
    if npm is not None:
        lines += _section(f"npm packages in the TUI bundle (production dependencies in {npm_label})")
        lines += _components(npm)
    if go is not None or runtime is not None:
        lines += _section("Go modules in the k3 binaries (linux and darwin, amd64 and arm64)")
        lines += _components(([runtime] if runtime else []) + (go or []))
    return "\n".join(lines).rstrip() + "\n"


def check_wheel(wheel: Path, repo: Path | None) -> list[str]:
    """What k3code's license files are missing from (or differ in) ``wheel``; empty when it is complete."""
    try:
        with zipfile.ZipFile(wheel) as z:
            members = {n: z.read(n) for n in z.namelist() if n.startswith("k3code/") and not n.endswith("/")}
    except (OSError, zipfile.BadZipFile) as e:
        return [f"{wheel} is not a readable wheel: {e}"]
    problems: list[str] = []
    expected: dict[str, bytes | None] = {f"k3code/{n}": None for n in WHEEL_FILES}
    if repo is not None:
        expected = {f"k3code/{n}": (repo / n).read_bytes() for n in WHEEL_FILES}
        for p in sorted((repo / "LICENSES").iterdir()):
            if p.is_file():
                expected[f"k3code/LICENSES/{p.name}"] = p.read_bytes()
    elif not any(n.startswith("k3code/LICENSES/") for n in members):
        problems.append("k3code/LICENSES/ is missing")
    for name, want in expected.items():
        if name not in members:
            problems.append(f"{name} is missing")
        elif want is not None and members[name] != want:
            problems.append(f"{name} differs from the repository's")
    return problems


def build_wheel(repo: Path, out: Path) -> Path:
    """The wheel of ``repo/core``, built the way release.sh builds it."""
    uv = shutil.which("uv")
    if uv is None:
        raise NoticeError("uv is not installed: it builds the wheel (https://docs.astral.sh/uv/)")
    r = subprocess.run(
        [uv, "build", "--quiet", "--wheel", "--out-dir", str(out)],
        cwd=repo / "core",
        capture_output=True,
        text=True,
        check=False,
    )
    wheels = sorted(out.glob("*.whl"))
    if r.returncode != 0 or len(wheels) != 1:
        raise NoticeError(f"uv build --wheel in {repo / 'core'} failed (exit {r.returncode}):\n{r.stderr.strip()}")
    return wheels[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    gen = sub.add_parser("generate", help="write a third-party notice file")
    gen.add_argument("--own", type=Path, help="the repository root: k3code's NOTICE and LICENSES/")
    gen.add_argument("--npm-lock", type=Path, help="an npm package-lock.json")
    gen.add_argument("--node-modules", type=Path, help="where its packages are installed (default: beside the lock)")
    gen.add_argument("--go-modules", type=Path, help="`go list -deps -json=Module` output")
    gen.add_argument("--goroot", type=Path, help="the GOROOT the binaries were built with")
    gen.add_argument("-o", "--output", type=Path, required=True)
    chk = sub.add_parser("check-wheel", help="fail unless a wheel carries k3code's license files")
    chk.add_argument("--repo", type=Path, help="compare with this repository's LICENSE, NOTICE and LICENSES/")
    chk.add_argument("--build", action="store_true", help="build the wheel of --repo's core/ first (needs uv)")
    chk.add_argument("wheel", type=Path, nargs="?")
    args = ap.parse_args(argv)

    if args.cmd == "check-wheel":
        if (args.wheel is None) == (not args.build) or (args.build and args.repo is None):
            ap.error("check-wheel needs a WHEEL, or --build with --repo")
        with tempfile.TemporaryDirectory(prefix="k3code-wheel-") as tmp:
            try:
                wheel = build_wheel(args.repo, Path(tmp)) if args.build else args.wheel
            except NoticeError as e:
                print(f"check-wheel: {e}", file=sys.stderr)
                return 1
            problems = check_wheel(wheel, args.repo)
            for p in problems:
                print(f"check-wheel: {wheel.name}: {p}", file=sys.stderr)
            if not problems:
                print(f"check-wheel: {wheel.name} ships LICENSE, NOTICE and LICENSES/")
        return 1 if problems else 0

    if not (args.own or args.npm_lock or args.go_modules or args.goroot):
        ap.error("generate needs at least one of --own, --npm-lock, --go-modules, --goroot")
    try:
        npm = npm_components(args.npm_lock, args.node_modules) if args.npm_lock else None
        go = go_components(args.go_modules) if args.go_modules else None
        runtime = go_runtime(args.goroot) if args.goroot else None
        label = f"{args.npm_lock.resolve().parent.name}/{args.npm_lock.name}" if args.npm_lock else ""
        text = render(args.own, npm, label, go, runtime)
    except (NoticeError, OSError, ValueError) as e:
        print(f"third_party_notices: {e}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    counts = [f"{len(x)} {what}" for x, what in ((npm, "npm packages"), (go, "Go modules")) if x is not None]
    print(f"third_party_notices: wrote {args.output}" + (f" ({', '.join(counts)})" if counts else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
